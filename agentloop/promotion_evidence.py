"""Conservative offline eligibility using native ablation and calibration data."""

from __future__ import annotations

from datetime import timedelta
from math import isclose

from agentloop.ablation_protocol import timestamp
from agentloop.ablations import build_ablation_report
from agentloop.budgets import BudgetLimits, budget_policy
from agentloop.calibration import _digest
from agentloop.harness_evidence import validate_evidence
from agentloop.promotion_types import BudgetProposal, utc_now
from agentloop.study_statistics import task_weighted_summary


def assess_promotion(proposal, *, now=None):
    if type(proposal) is not BudgetProposal:
        raise ValueError("promotion assessment requires BudgetProposal")
    moment = timestamp(now or utc_now())
    data = proposal.to_dict()
    spec = data["protocol"]["specification"]
    thresholds = data["thresholds"]
    reasons, diagnostics = [], {}
    earliest = moment - timedelta(days=thresholds["max_age_days"])
    if not earliest <= timestamp(spec["frozen_at"]) <= moment or moment >= timestamp(
        data["valid_until"]
    ):
        reasons.append("evidence_expired_or_future")
    if spec["synthetic"]:
        reasons.append("synthetic_evidence")
    if spec["bootstrap"]["confidence"] < 0.95 or spec["bootstrap"]["samples"] < 200:
        reasons.append("uncertainty_settings_insufficient")
    capability = data["capability"]
    if (
        not capability["passed"]
        or capability["check_count"] == 0
        or capability["adapter"] != "python"
        or capability["source_version"] != spec["versions"]["source"]
        or capability["policy_config_hash"] != data["policy_config_hash"]
        or not earliest <= timestamp(capability["tested_at"]) <= moment
    ):
        reasons.append("capability_evidence_ineligible")
    policy = budget_policy(BudgetLimits(**data["candidate"]))
    expected = {policy.policy_id: policy.version + ":" + policy.config_hash}
    matching = {
        condition["mode"]: name
        for name, condition in spec["conditions"].items()
        if condition["policies"] == expected
    }
    if set(matching) != {"shadow", "enforce"}:
        reasons.append("policy_configuration_mismatch")
    if (
        spec["quality_gate"]["min_score"] < thresholds["min_quality_score"]
        or spec["quality_gate"]["max_regression"] > thresholds["max_quality_regression"]
    ):
        reasons.append("quality_threshold_mismatch")
    try:
        report = build_ablation_report(data["protocol"], data["observations"])
    except ValueError:
        reasons.append("invalid_ablation_evidence")
        report = None
    if report is not None:
        diagnostics["planned_observations"] = report["planned_observation_count"]
        diagnostics["observed_count"] = report["observed_count"]
        diagnostics["status_counts"] = report["status_counts"]
        if (
            report["unplanned_observation_count"]
            or any(row["issues"] for row in report["observations"])
            or report["planned_observation_count"] != report["observed_count"]
        ):
            reasons.append("incomplete_or_mismatched_observations")
        if any(
            timestamp(row["observation"]["started_at"]) > moment for row in report["observations"]
        ):
            reasons.append("future_observations")
        _coverage(data, report, matching, reasons, diagnostics, moment)
        relevant = [
            item
            for item in report["comparisons"]
            if item["split"] == "held_out"
            and (
                (
                    item["kind"] == "total_policy_effect"
                    and item["candidate"] == matching.get("enforce")
                )
                or (
                    item["kind"] == "policy_shadow_overhead"
                    and item["candidate"] == matching.get("shadow")
                )
            )
        ]
        diagnostics["held_out_comparisons"] = relevant
        if not relevant:
            reasons.append("missing_held_out_comparison")
        for item in relevant:
            latency = item["descriptive_deltas"]["latency_ms"]
            if (
                latency["planned_task_count"] < thresholds["min_held_out_tasks"]
                or item["planned_pair_count"] < thresholds["min_held_out_pairs"]
            ):
                reasons.append("held_out_sample_too_small")
            counts = item["quality_gate_counts"]
            if counts["accepted"] != item["planned_pair_count"]:
                reasons.append("held_out_quality_ineligible")
            if (
                latency["interval"]["status"] != "computed"
                or latency["pair_summary"]["count"] != item["planned_pair_count"]
            ):
                reasons.append("latency_evidence_missing")
            elif (
                item["kind"] == "total_policy_effect"
                and latency["interval"]["upper"] > -thresholds["min_latency_improvement_ms"]
            ):
                reasons.append("performance_threshold_failed")
            elif (
                item["kind"] == "policy_shadow_overhead"
                and latency["interval"]["upper"] > thresholds["max_shadow_overhead_ms"]
            ):
                reasons.append("shadow_overhead_exceeded")
            cost = item["descriptive_deltas"]["provider_cost_usd"]
            # One price basis is required; mixed or inferred operating cost does
            # not authorize a deployment. Calculated cost remains qualified.
            if (
                cost["pair_summary"]["count"] != item["planned_pair_count"]
                or cost["interval"]["status"] != "computed"
            ):
                reasons.append("cost_evidence_missing")
            elif (
                item["kind"] == "total_policy_effect"
                and cost["interval"]["upper"] > thresholds["max_cost_regression_usd"]
            ):
                reasons.append("cost_regression")
    _calibration(data, spec, moment, earliest, reasons, diagnostics)
    return {
        "schema_version": "1.0",
        "proposal_hash": proposal.proposal_hash,
        "eligible": not reasons,
        "reasons": sorted(set(reasons)),
        "diagnostics": diagnostics,
        "assessed_at": moment.isoformat(),
        "enforcement_authorized": False,
        "interpretation": "Eligibility is conditional evidence for operator review; shadow coverage is not counterfactual benefit and no deployment is activated.",
    }


def _coverage(data, report, matching, reasons, diagnostics, moment):
    seen, covered, planned = set(), 0, 0
    modes = {value: key for key, value in matching.items()}
    for wrapper in report["observations"]:
        row = wrapper["observation"]
        if row["condition"] not in modes:
            continue
        planned += 1
        try:
            identity = row["trace_run_id"]
            if identity is None or identity in seen:
                raise ValueError()
            seen.add(identity)
            envelope = validate_evidence(data["harness_evidence"][identity], trace_id=identity)
            records = [
                item
                for item in envelope["decisions"].values()
                if item["policy_config_hash"] == data["policy_config_hash"]
                and item["mode"] == modes[row["condition"]]
            ]
            started = timestamp(row["started_at"])
            duration = row["metrics"]["latency_ms"]
            if duration is None or any(
                not started
                <= timestamp(item["timing"]["started_at"])
                <= min(moment, started + timedelta(milliseconds=duration + 1000))
                for item in records
            ):
                raise ValueError()
            before = {item["call_id"] for item in records if item["phase"] == "before"}
            after = {item["call_id"] for item in records if item["phase"] == "after"}
            if (
                not before
                or before != after
                or any(item["outcome"] == "failed" for item in records)
                or envelope["capture_errors"]
            ):
                raise ValueError()
            for boundary, metric in (("model", "model_calls"), ("tool", "tool_calls")):
                observed = sum(
                    item["phase"] == "after" and item["boundary"] == boundary and item["dispatched"]
                    for item in records
                )
                if row["metrics"][metric] != observed:
                    raise ValueError()
            costs, usage_ids = [], set()
            for item in records:
                if (
                    item["phase"] != "after"
                    or item["boundary"] != "model"
                    or not item["dispatched"]
                ):
                    continue
                usage = item["budget_snapshot"]["last_usage"]
                if (
                    not usage
                    or not usage["complete"]
                    or not usage["exclusive"]
                    or usage["cost_provenance"] != "provider_reported"
                    or usage["cost_usd"] is None
                ):
                    raise ValueError()
                usage_id = usage["usage_id"]
                if usage_id is None or usage_id not in usage_ids:
                    costs.append(usage["cost_usd"])
                usage_ids.add(usage_id)
            if row["metrics"]["cost_usd"] is None or not isclose(
                sum(costs), row["metrics"]["cost_usd"], rel_tol=1e-9, abs_tol=1e-12
            ):
                raise ValueError()
            covered += 1
        except (TypeError, ValueError, KeyError):
            reasons.append("decision_coverage_incomplete")
    diagnostics["decision_coverage"] = {"planned": planned, "verified": covered}


def _calibration(data, spec, moment, earliest, reasons, diagnostics):
    report = data["calibration"]
    try:
        if (
            not isinstance(report, dict)
            or report.get("schema_version") != "1.0"
            or report.get("artifact_hash")
            != _digest({key: value for key, value in report.items() if key != "artifact_hash"})
        ):
            raise ValueError()
        if report["selection_inventory_complete"] is not True:
            reasons.append("calibration_inventory_incomplete")
        if (
            not earliest
            <= timestamp(report["validity"]["as_of"])
            <= moment
            < timestamp(report["validity"]["valid_until"])
        ):
            reasons.append("calibration_expired_or_future")
        cohort = next(
            item for item in report["cohorts"] if item["cohort_id"] == data["calibration_cohort"]
        )
        context = cohort["context"]
        cohort_identity = {"context": context, "estimator": cohort["estimator"]}
        if cohort["cohort_id"] != "cal_" + _digest(cohort_identity):
            reasons.append("calibration_context_mismatch")
        if context["cost_basis"] != "provider_reported":
            reasons.append("calibration_cost_basis_mismatch")
        policy = budget_policy(BudgetLimits(**data["candidate"]))
        if (
            context["policy"]
            != {
                "id": policy.policy_id,
                "version": policy.version,
                "config_hash": policy.config_hash,
            }
            or context["workload"] != spec["workload_id"]
            or any(
                context[key] != spec["versions"][key]
                for key in ("model", "provider", "environment", "scorer")
            )
            or context["quality_gate"] != spec["quality_gate"]
        ):
            reasons.append("calibration_context_mismatch")
        rows = [
            row
            for row in report["registrations"]
            if row["cohort_id"] == data["calibration_cohort"] and row["split"] == "held_out"
        ]
        if any(row["cohort"] != cohort_identity for row in rows):
            reasons.append("calibration_context_mismatch")
        diagnostics["calibration_held_out_registrations"] = len(rows)
        diagnostics["calibration_synthetic_count"] = sum(row["synthetic"] for row in rows)
        diagnostics["calibration_selected_count"] = sum(
            row["selection"] == "selected" for row in rows
        )
        for metric, limit in (
            ("latency", "max_calibration_latency_mae_ms"),
            ("cost", "max_calibration_cost_mae_usd"),
        ):
            values = [
                (
                    row["task_key"],
                    abs(row["metrics"][metric]["signed_error"])
                    if row["synthetic"] is False
                    and not row["issues"]
                    and row["metrics"][metric]["empirical_eligible"] is True
                    and row["metrics"][metric]["signed_error"] is not None
                    else None,
                )
                for row in rows
            ]
            summary = task_weighted_summary(values, spec["bootstrap"])
            diagnostics["calibration_" + metric] = summary
            if summary["interval"]["task_count"] < data["thresholds"][
                "min_calibration_tasks"
            ] or summary["observation_summary"]["count"] != len(rows):
                reasons.append("calibration_evidence_missing_or_small")
            elif summary["task_summary"]["mean"] > data["thresholds"][limit]:
                reasons.append("calibration_error_exceeded")
    except (TypeError, ValueError, KeyError, StopIteration, OverflowError):
        reasons.append("invalid_or_missing_calibration")
