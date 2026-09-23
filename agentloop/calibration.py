"""Offline calibration of preserved prediction/outcome artifacts, never runtime learning."""

from __future__ import annotations

import json
import math
import re
from collections import Counter, defaultdict
from datetime import datetime
from fractions import Fraction
from hashlib import sha256
from pathlib import Path

from agentloop.harness_evidence import METADATA_KEY, validate_evidence
from agentloop.interventions import InterventionRecord, canonical_json
from agentloop.markdown import markdown_code_span, markdown_heading, markdown_table_cell
from agentloop.study_statistics import task_weighted_summary
from agentloop.trace_sources import TraceSource

SCHEMA_VERSION = "1.0"
_DIGEST = re.compile(r"[a-f0-9]{64}\Z")
_LABEL = re.compile(r"[A-Za-z0-9_.:-]{1,96}\Z")
_CONTEXT_FIELDS = {
    "workload",
    "model",
    "provider",
    "environment",
    "scorer",
    "policy",
    "cost_basis",
    "pricing",
    "quality_gate",
    "intervention",
}
_METRICS = {
    "latency": ("estimated_latency_savings_ms", "runtime_ms"),
    "cost": ("estimated_cost_savings_usd", "estimated_cost_usd"),
}


class CalibrationValidationError(ValueError):
    """The calibration declaration is ambiguous or malformed."""


def _fields(value, keys, label):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise CalibrationValidationError(f"{label} must contain exactly: {', '.join(sorted(keys))}")


def _text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise CalibrationValidationError(f"{label} must be nonempty text")


def _finite(value, *, nonnegative=False):
    try:
        return (
            type(value) in {int, float} and math.isfinite(value) and (not nonnegative or value >= 0)
        )
    except (OverflowError, TypeError):
        return False


def _time(value):
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.utcoffset() is None:
            raise ValueError
        return result
    except (AttributeError, TypeError, ValueError):
        raise CalibrationValidationError("timestamps require ISO-8601 with a timezone") from None


def _digest(value):
    return sha256(canonical_json(value).encode()).hexdigest()


def _scalar(value, label):
    if type(value) is int or isinstance(value, str) and value:
        return canonical_json([type(value).__name__, value])
    raise CalibrationValidationError(f"{label} must be a nonempty string or integer")


def _validate(manifest):
    _fields(
        manifest,
        {
            "schema_version",
            "name",
            "as_of",
            "valid_until",
            "fit_method",
            "min_fit_tasks",
            "selection_inventory_complete",
            "bootstrap",
            "registrations",
        },
        "calibration manifest",
    )
    if manifest["schema_version"] != SCHEMA_VERSION:
        raise CalibrationValidationError("calibration schema_version must be 1.0")
    _text(manifest["name"], "name")
    _time(manifest["as_of"])
    _time(manifest["valid_until"])
    if manifest["fit_method"] not in ("none", "scale"):
        raise CalibrationValidationError("fit_method must be none or scale")
    if type(manifest["min_fit_tasks"]) is not int or manifest["min_fit_tasks"] < 2:
        raise CalibrationValidationError("min_fit_tasks must be an integer of at least two")
    if type(manifest["selection_inventory_complete"]) is not bool:
        raise CalibrationValidationError("selection_inventory_complete must be a boolean")
    settings = manifest["bootstrap"]
    _fields(settings, {"samples", "seed", "confidence"}, "bootstrap")
    if (
        type(settings["samples"]) is not int
        or not 1 <= settings["samples"] <= 10000
        or type(settings["seed"]) is not int
        or not _finite(settings["confidence"])
        or not 0 < settings["confidence"] < 1
    ):
        raise CalibrationValidationError(
            "bootstrap requires 1..10000 samples, integer seed and confidence in (0,1)"
        )
    cases = manifest["registrations"]
    if not isinstance(cases, list):
        raise CalibrationValidationError("registrations must be a list, including when empty")
    ids, tasks, digests = set(), {}, {}
    for case in cases:
        _fields(
            case,
            {
                "case_id",
                "task_id",
                "task_sha256",
                "repetition",
                "split",
                "selection",
                "selection_reason",
                "synthetic",
                "context",
                "prediction",
                "prediction_recorded_at",
                "outcome",
            },
            "registration",
        )
        _text(case["case_id"], "case_id")
        if case["case_id"] in ids:
            raise CalibrationValidationError("case IDs must be unique")
        ids.add(case["case_id"])
        task = _scalar(case["task_id"], "task_id")
        _scalar(case["repetition"], "repetition")
        if case["split"] not in ("fit", "held_out") or case["selection"] not in (
            "selected",
            "rejected",
            "unselected",
        ):
            raise CalibrationValidationError("invalid split or selection")
        if not isinstance(case["selection_reason"], str) or not _LABEL.fullmatch(
            case["selection_reason"]
        ):
            raise CalibrationValidationError("selection_reason must be a bounded identifier")
        if type(case["synthetic"]) is not bool:
            raise CalibrationValidationError("synthetic must be a boolean")
        if not isinstance(case["task_sha256"], str) or not _DIGEST.fullmatch(case["task_sha256"]):
            raise CalibrationValidationError("task_sha256 must identify the full frozen task")
        context = case["context"]
        _fields(context, _CONTEXT_FIELDS, "context")
        for key in ("workload", "model", "provider", "environment", "scorer"):
            _text(context[key], key)
        _fields(context["intervention"], {"type", "configuration"}, "planned intervention")
        _text(context["intervention"]["type"], "intervention type")
        if not isinstance(context["intervention"]["configuration"], dict):
            raise CalibrationValidationError("planned intervention configuration must be an object")
        if context["cost_basis"] not in ("provider_reported", "calculated", "mixed", "unknown"):
            raise CalibrationValidationError("unsupported cost_basis")
        if context["pricing"] is not None:
            _text(context["pricing"], "pricing")
        gate = context["quality_gate"]
        _fields(gate, {"min_score", "max_regression"}, "quality_gate")
        if any(not _finite(value) or not 0 <= value <= 1 for value in gate.values()):
            raise CalibrationValidationError("quality thresholds must be in [0,1]")
        policy = context["policy"]
        if policy is not None:
            _fields(policy, {"id", "version", "config_hash"}, "policy")
            for key in ("id", "version"):
                _text(policy[key], key)
            if not isinstance(policy["config_hash"], str) or not _DIGEST.fullmatch(
                policy["config_hash"]
            ):
                raise CalibrationValidationError("policy config_hash must be a SHA-256 digest")
        identity = (context["workload"], task)
        declaration = (case["split"], case["task_sha256"])
        if identity in tasks and tasks[identity] != declaration:
            raise CalibrationValidationError(
                "a task cannot cross splits or change its input digest"
            )
        tasks[identity] = declaration
        digest = case["task_sha256"]
        if digest in digests and digests[digest] != (case["split"], identity):
            raise CalibrationValidationError(
                "identical task inputs must not cross splits or inflate task IDs"
            )
        digests[digest] = (case["split"], identity)
        if not isinstance(case["prediction"], dict):
            raise CalibrationValidationError(
                "prediction must be the original complete finding snapshot"
            )
        _text(case["prediction"].get("finding_id"), "prediction finding_id")
        if case["prediction_recorded_at"] is not None:
            _time(case["prediction_recorded_at"])
        outcome = case["outcome"]
        if outcome is not None:
            _fields(
                outcome,
                {"record", "baseline_trace", "candidate_trace", "recorded_at", "status"},
                "outcome",
            )
            for key in ("record", "baseline_trace", "candidate_trace"):
                if outcome[key] is not None:
                    _text(outcome[key], key)
            _time(outcome["recorded_at"])
            if outcome["status"] not in (
                "completed",
                "failed",
                "cancelled",
                "timed_out",
                "stopped",
                "incomplete",
            ):
                raise CalibrationValidationError("unsupported outcome status")
            if case["selection"] != "selected":
                raise CalibrationValidationError("an executed outcome requires selected status")


def _load(path, kind, sources, cache):
    if path is None:
        return None, f"missing_{kind}"
    path = Path(path).resolve()
    sources.add(path)
    key = (path, kind)
    if key not in cache:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            value = (
                InterventionRecord.from_dict(payload).to_dict()
                if kind == "intervention"
                else TraceSource.from_dict(payload)
            )
            cache[key] = (value, None)
        except FileNotFoundError:
            cache[key] = (None, f"missing_{kind}")
        except (OSError, ValueError, TypeError, KeyError, RecursionError):
            cache[key] = (None, f"invalid_{kind}")
    return cache[key]


def _safe_difference(before, after):
    if not _finite(before) or not _finite(after):
        return None
    result = before - after
    return float(result) if _finite(result) else None


def _quality(case, record, traces, verified):
    outcome = case["outcome"]
    if case["selection"] != "selected":
        return "not_evaluated"
    if outcome is not None and outcome["status"] != "completed":
        return "rejected"
    if not verified or record is None or outcome is None:
        return "indeterminate"
    success = [trace.metadata.get("success") for trace in traces]
    if any(value is False for value in success):
        return "rejected"
    if any(value is not True for value in success):
        return "indeterminate"
    quality = record["measured"].get("quality")
    if isinstance(quality, dict):
        scores = [quality.get("baseline_score"), quality.get("candidate_score")]
    else:
        scores = [
            record["measured"].get(side, {}).get("quality_score")
            for side in ("baseline", "candidate")
        ]
    if any(not _finite(value) or not 0 <= value <= 1 for value in scores):
        return "indeterminate"
    before, after = scores
    gate = case["context"]["quality_gate"]
    if min(scores) < gate["min_score"] or Fraction(str(before)) - Fraction(str(after)) > Fraction(
        str(gate["max_regression"])
    ):
        return "rejected"
    return "accepted"


def _policy_observed(policy, trace):
    if policy is None:
        return True
    try:
        evidence = validate_evidence(trace.metadata.get(METADATA_KEY), trace_id=trace.run_id)
        if evidence["capture_errors"]:
            return False
        snapshot = evidence["policies"].get(policy["config_hash"], {})
        return (
            snapshot.get("policy_id") == policy["id"]
            and snapshot.get("version") == policy["version"]
            and any(
                item["policy_config_hash"] == policy["config_hash"]
                and item["mode"] == "enforce"
                and item["origin"] == "policy"
                for item in evidence["decisions"].values()
            )
        )
    except (ValueError, TypeError, KeyError):
        return False


def _cost_known(summary, basis):
    return (
        basis in {"provider_reported", "calculated"}
        and summary.get("cost_status") in {"complete", "empty"}
        and summary.get("has_unknown_cost") is False
        and (basis == "provider_reported" or summary.get("token_status") in {"exact", "empty"})
    )


def _source_cost_basis(traces, summaries, basis):
    """Verify reported-vs-calculated origin without looking up current prices."""
    for trace, summary in zip(traces, summaries):
        if trace is None:
            return False
        model_events = [event for event in trace.events if event.event_type == "model_call"]
        amounts = [
            event.metadata.get("provider_reported_cost_usd", event.metadata.get("cost_usd"))
            for event in model_events
        ]
        if basis == "calculated":
            if any(value is not None for value in amounts):
                return False
        elif basis == "provider_reported":
            if any(not _finite(value, nonnegative=True) for value in amounts):
                return False
            total = sum(amounts)
            if not _finite(total) or round(total, 6) != summary.get("estimated_cost_usd"):
                return False
        else:
            return False
    return len(traces) == 2


def _case(case, root, manifest, sources, cache):
    issues = []
    prediction = case["prediction"]
    estimate = prediction.get("estimate")
    estimator = (
        {
            key: estimate.get(key)
            for key in ("estimator_id", "estimator_version", "method", "formula", "parameters")
        }
        if isinstance(estimate, dict)
        else None
    )
    if (
        estimator is None
        or any(
            not isinstance(estimator[key], str) or not estimator[key]
            for key in ("estimator_id", "estimator_version", "method", "formula")
        )
        or not isinstance(estimator["parameters"], dict)
    ):
        issues.append("missing_estimator_provenance")
    cohort = {"context": case["context"], "estimator": estimator}
    record, traces = None, []
    synthetic = case["synthetic"]
    outcome = case["outcome"]
    if case["selection"] != "selected":
        issues.append("not_selected")
    if case["prediction_recorded_at"] is None:
        issues.append("prediction_time_unknown")
    elif _time(case["prediction_recorded_at"]) > _time(manifest["as_of"]):
        issues.append("prediction_after_as_of")
    if outcome is None:
        issues.append("outcome_not_recorded")
    else:
        observed = _time(outcome["recorded_at"])
        if observed > _time(manifest["as_of"]):
            issues.append("outcome_after_as_of")
        if (
            case["prediction_recorded_at"] is not None
            and _time(case["prediction_recorded_at"]) >= observed
        ):
            issues.append("prediction_not_frozen_before_outcome")
        record, error = _load(
            root / outcome["record"] if outcome["record"] is not None else None,
            "intervention",
            sources,
            cache,
        )
        if error is not None:
            issues.append(error)
        for side in ("baseline", "candidate"):
            source, error = _load(
                root / outcome[f"{side}_trace"] if outcome[f"{side}_trace"] is not None else None,
                "trace",
                sources,
                cache,
            )
            if error is not None:
                issues.append(f"{side}_{error}")
            trace = source.trace if source is not None else None
            traces.append(trace)
            if trace is not None:
                synthetic = synthetic or trace.metadata.get("synthetic") is True
                if side == "candidate" and case["prediction_recorded_at"] is not None:
                    try:
                        if _time(case["prediction_recorded_at"]) >= _time(trace.started_at):
                            issues.append("prediction_not_frozen_before_candidate")
                    except CalibrationValidationError:
                        issues.append("candidate_time_unverified")
                actual_repetition = trace.metadata.get("repetition", trace.metadata.get("seed"))
                if canonical_json(
                    [
                        trace.metadata.get("task_id"),
                        type(trace.metadata.get("task_id")).__name__,
                        actual_repetition,
                        type(actual_repetition).__name__,
                    ]
                ) != canonical_json(
                    [
                        case["task_id"],
                        type(case["task_id"]).__name__,
                        case["repetition"],
                        type(case["repetition"]).__name__,
                    ]
                ):
                    issues.append(f"{side}_pairing_unverified")
                if record is not None and (
                    trace.run_id != record[f"{side}_run_id"]
                    or record["trace_fingerprints"][side] not in source.fingerprints
                ):
                    issues.append(f"{side}_source_mismatch")
        if record is not None:
            synthetic = synthetic or record["metadata"].get("synthetic") is True
            planned = case["context"]["intervention"]
            if record["intervention_type"] != planned["type"] or canonical_json(
                record["configuration"]
            ) != canonical_json(planned["configuration"]):
                issues.append("intervention_configuration_mismatch")
            findings = record["predicted"]["findings"]
            if len(findings) != 1:
                issues.append("combined_intervention_unattributed")
            saved = next(
                (item for item in findings if item.get("finding_id") == prediction["finding_id"]),
                None,
            )
            if saved is None or canonical_json(saved) != canonical_json(prediction):
                issues.append("original_prediction_mismatch")
        if (
            len(traces) == 2
            and traces[1] is not None
            and not _policy_observed(case["context"]["policy"], traces[1])
        ):
            issues.append("policy_not_observed_enforced")
    verified = not issues
    quality = _quality(case, record, traces, verified)
    if synthetic:
        issues.append("synthetic")
    metrics = {}
    for name, (prediction_key, measured_key) in _METRICS.items():
        reasons = []
        savings = prediction.get("savings")
        predicted = savings.get(prediction_key) if isinstance(savings, dict) else None
        modeled = estimate.get("unmodeled_metrics") if isinstance(estimate, dict) else None
        if not isinstance(modeled, list) or any(not isinstance(item, str) for item in modeled):
            predicted = None
            reasons.append("modeled_metric_unknown")
        elif name in modeled:
            predicted = None
            reasons.append("unmodeled_metric")
        if not _finite(predicted):
            predicted = None
            reasons.append("prediction_unavailable")
        before = record["measured"]["baseline"] if record is not None else {}
        after = record["measured"]["candidate"] if record is not None else {}
        a, b = before.get(measured_key), after.get(measured_key)
        realized = (
            _safe_difference(a, b)
            if _finite(a, nonnegative=True) and _finite(b, nonnegative=True)
            else None
        )
        if name == "latency" and any(trace is None or trace.ended_at is None for trace in traces):
            realized = None
            reasons.append("incomplete_timing")
        if name == "cost":
            basis = case["context"]["cost_basis"]
            inputs = estimate.get("inputs", {}) if isinstance(estimate, dict) else {}
            if (
                not isinstance(inputs, dict)
                or inputs.get("cost_status") not in {"complete", "empty"}
                or basis not in {"provider_reported", "calculated"}
                or (basis == "calculated" and inputs.get("token_status") not in {"exact", "empty"})
            ):
                predicted = None
                reasons.append("prediction_cost_provenance_incomplete")
            if not _cost_known(before, basis) or not _cost_known(after, basis):
                realized = None
                reasons.append("outcome_cost_provenance_incomplete")
            if not _source_cost_basis(traces, (before, after), basis):
                realized = None
                reasons.append("source_cost_basis_unverified")
            if basis == "calculated" and case["context"]["pricing"] is None:
                predicted = realized = None
                reasons.append("pricing_reference_missing")
        if realized is None:
            reasons.append("outcome_unavailable")
        error = _safe_difference(predicted, realized)
        ratio, ratio_status = None, "unavailable"
        if predicted is not None and realized is not None:
            if predicted == 0:
                ratio_status = "zero_prediction"
            else:
                try:
                    ratio = float(Fraction(str(realized)) / Fraction(str(predicted)))
                    ratio_status = "available" if _finite(ratio) else "numeric_range"
                except (OverflowError, ValueError):
                    ratio_status = "numeric_range"
                if ratio_status != "available":
                    ratio = None
            if error is None:
                reasons.append("error_numeric_range")
        metrics[name] = {
            "predicted_savings": predicted,
            "realized_savings": realized,
            "signed_error": error,
            "realization_ratio": ratio,
            "ratio_status": ratio_status,
            "reasons": sorted(set(reasons)),
            "empirical_eligible": not issues and error is not None,
        }
    return {
        "case_id": case["case_id"],
        "repetition_key": _scalar(case["repetition"], "repetition"),
        "task_key": canonical_json(
            [case["context"]["workload"], _scalar(case["task_id"], "task_id")]
        ),
        "split": case["split"],
        "selection": case["selection"],
        "selection_reason": case["selection_reason"],
        "outcome_status": outcome["status"] if outcome is not None else None,
        "cohort_id": "cal_" + _digest(cohort),
        "cohort": cohort,
        "synthetic": synthetic,
        "issues": sorted(set(issues)),
        "quality_gate": quality,
        "metrics": metrics,
        "prediction_snapshot": prediction,
        "intervention": record,
        "source_event_error_count": sum(
            event.status == "error"
            for trace in traces
            if trace is not None
            for event in trace.events
        ),
    }


def _fit_scale(rows, metric, minimum):
    pairs = defaultdict(list)
    for row in rows:
        value = row["metrics"][metric]
        if row["split"] == "fit" and value["empirical_eligible"]:
            pairs[row["task_key"]].append(
                (
                    Fraction(str(value["predicted_savings"])),
                    Fraction(str(value["realized_savings"])),
                )
            )
    task_means = [
        (sum(pair[0] for pair in group) / len(group), sum(pair[1] for pair in group) / len(group))
        for _, group in sorted(pairs.items())
    ]
    result = {
        "method": "multiplicative_least_squares_on_equal_weight_task_means",
        "task_count": len(task_means),
        "factor": None,
        "status": "insufficient_tasks",
    }
    if len(task_means) < minimum:
        return result
    denominator = sum(before * before for before, _ in task_means)
    if denominator == 0:
        return {**result, "status": "zero_predictions"}
    factor = sum(before * after for before, after in task_means) / denominator
    try:
        numeric = float(factor)
    except OverflowError:
        numeric = math.inf
    if not _finite(numeric):
        return {**result, "status": "numeric_range"}
    return {**result, "factor": numeric, "status": "fitted"}


def _mark_duplicates(rows):
    by_outcome, by_pair = defaultdict(list), defaultdict(list)
    for row in rows:
        if row["intervention"] is not None:
            by_outcome[row["intervention"]["intervention_id"]].append(row)
        by_pair[(row["cohort_id"], row["task_key"], row["repetition_key"])].append(row)
    for groups, issue in ((by_outcome, "duplicate_outcome"), (by_pair, "duplicate_pairing_key")):
        for group in groups.values():
            if len(group) > 1:
                for row in group:
                    row["issues"] = sorted(set([*row["issues"], issue]))
                    if row["quality_gate"] == "accepted":
                        row["quality_gate"] = "indeterminate"
                    for metric in row["metrics"].values():
                        metric["empirical_eligible"] = False


def _metric_summary(rows, metric, key, settings, *, quality_only=False):
    return task_weighted_summary(
        (
            (
                row["task_key"],
                row["metrics"][metric].get(key)
                if row["metrics"][metric]["empirical_eligible"]
                and (not quality_only or row["quality_gate"] == "accepted")
                else None,
            )
            for row in rows
            if not row["synthetic"]
        ),
        settings,
    )


def _scaled_prediction(value, factor):
    if value is None or factor is None:
        return None
    try:
        result = float(Fraction(str(value)) * Fraction(str(factor)))
        return result if _finite(result) else None
    except (ValueError, OverflowError):
        return None


def _cohort(rows, manifest):
    metrics = {}
    for metric in _METRICS:
        fitted = (
            _fit_scale(rows, metric, manifest["min_fit_tasks"])
            if manifest["fit_method"] == "scale"
            else {"method": "none", "status": "not_requested", "factor": None, "task_count": 0}
        )
        for row in rows:
            values = row["metrics"][metric]
            values["scaled_prediction"] = _scaled_prediction(
                values["predicted_savings"], fitted["factor"]
            )
            values["scaled_signed_error"] = _safe_difference(
                values["scaled_prediction"], values["realized_savings"]
            )
        splits = {}
        for split in ("fit", "held_out"):
            subset = [row for row in rows if row["split"] == split]
            non_synthetic = [row for row in subset if not row["synthetic"]]
            selected = [row for row in non_synthetic if row["selection"] == "selected"]
            counts = Counter(row["quality_gate"] for row in selected)
            splits[split] = {
                "registration_count": len(subset),
                "non_synthetic_registration_count": len(non_synthetic),
                "selected_count": len(selected),
                "synthetic_excluded_count": len(subset) - len(non_synthetic),
                "quality_gate_counts": {
                    key: counts[key] for key in ("accepted", "rejected", "indeterminate")
                },
                "quality_acceptance_rate": counts["accepted"] / len(selected) if selected else None,
                "selection_counts": dict(
                    sorted(Counter(row["selection"] for row in non_synthetic).items())
                ),
                "original": {
                    key: _metric_summary(subset, metric, key, manifest["bootstrap"])
                    for key in (
                        "predicted_savings",
                        "realized_savings",
                        "signed_error",
                        "realization_ratio",
                    )
                },
                "quality_preserving": {
                    key: _metric_summary(
                        subset, metric, key, manifest["bootstrap"], quality_only=True
                    )
                    for key in ("realized_savings", "signed_error")
                },
                "scaled_error": _metric_summary(
                    subset, metric, "scaled_signed_error", manifest["bootstrap"]
                ),
            }
        metrics[metric] = {
            "fit": fitted,
            "splits": splits,
            "held_out_status": "observed"
            if splits["held_out"]["original"]["signed_error"]["observation_summary"]["count"]
            else "no_evaluable_held_out_outcomes",
        }
    return {
        "cohort_id": rows[0]["cohort_id"],
        **rows[0]["cohort"],
        "registration_count": len(rows),
        "metrics": metrics,
        "issues": dict(sorted(Counter(issue for row in rows for issue in row["issues"]).items())),
        "selection_reasons": dict(sorted(Counter(row["selection_reason"] for row in rows).items())),
    }


def summarize_calibration(manifest_path):
    """Read historical artifacts without re-diagnosing, re-pricing, or rerunning agents."""
    path = Path(manifest_path).resolve()
    sources, cache = {path}, {}
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
        # Own a finite JSON snapshot so neither reads nor reporting can mutate inputs.
        manifest = json.loads(canonical_json(manifest))
        _validate(manifest)
        rows = [
            _case(case, path.parent, manifest, sources, cache) for case in manifest["registrations"]
        ]
        rows.sort(key=lambda row: row["case_id"])
        _mark_duplicates(rows)
        groups = defaultdict(list)
        for row in rows:
            groups[row["cohort_id"]].append(row)
        cohorts = [_cohort(group, manifest) for _, group in sorted(groups.items())]
        report = {
            "schema_version": SCHEMA_VERSION,
            "name": manifest["name"],
            "manifest": manifest,
            "manifest_hash": _digest(manifest),
            "registrations": rows,
            "cohorts": cohorts,
            "registration_count": len(rows),
            "synthetic_excluded_count": sum(row["synthetic"] for row in rows),
            "selection_inventory_complete": manifest["selection_inventory_complete"],
            "source_paths": sorted(str(source) for source in sources),
            "validity": {
                "as_of": manifest["as_of"],
                "valid_until": manifest["valid_until"],
                "expired": _time(manifest["as_of"]) > _time(manifest["valid_until"]),
                "scope": "Only the exact recorded estimator/policy, workload, model/provider, environment, scorer and quality-gate cohort. Changes require renewed validation; freshness is assessed at the declared as_of, not the reader's current clock.",
            },
            "runtime_coefficients_changed": False,
            "interpretation": "Historical descriptive calibration, not a causal or universal-confidence claim. Savings are baseline minus candidate; signed error is prediction minus realized savings (positive means overprediction). Synthetic, ambiguous and unverifiable cases are excluded from empirical aggregates but retained. All measurable outcomes, including failures and unknown quality, enter raw error summaries and any requested scale fit. Quality-preserving subsets are conditional and retain denominators. Scale fitting targets raw resource savings, never task utility; fitting-task error is in-sample and held-out error is separate. Task means, not seeds, are the bootstrap units. Selection, missingness, small-task uncertainty, multiplicity, scorer validity and workload drift remain limitations. No estimator or runtime policy is modified.",
        }
        report["artifact_hash"] = _digest(report)
        return report
    except (OSError, ValueError, TypeError, KeyError, RecursionError, OverflowError) as exc:
        raise CalibrationValidationError(f"cannot read calibration manifest: {exc}") from exc


def calibration_to_markdown(report):
    lines = [
        f"# Calibration: {markdown_heading(report['name'])}",
        "",
        report["interpretation"],
        "",
        f"Registrations: {report['registration_count']}; synthetic excluded: {report['synthetic_excluded_count']}.",
        "",
        f"Selection inventory declared complete: {report['selection_inventory_complete']}. This is a host declaration, not proof of unbiased selection.",
        "",
        f"Freshness at {markdown_heading(report['validity']['as_of'])}: {'expired' if report['validity']['expired'] else 'within declared validity window'}; valid until {markdown_heading(report['validity']['valid_until'])}.",
        "",
    ]
    for cohort in report["cohorts"]:
        context = cohort["context"]
        lines.extend(
            [
                f"## {markdown_heading(cohort['cohort_id'])}",
                "",
                f"Workload: {markdown_heading(context['workload'])}; model/provider: {markdown_heading(context['model'])} / {markdown_heading(context['provider'])}; cost basis: {markdown_heading(context['cost_basis'])}.",
                "",
                "Estimator: " + markdown_code_span(canonical_json(cohort["estimator"])),
                "",
                "Environment/scorer/pricing/policy: "
                + markdown_code_span(
                    canonical_json(
                        {
                            key: context[key]
                            for key in (
                                "environment",
                                "scorer",
                                "pricing",
                                "policy",
                                "quality_gate",
                                "intervention",
                            )
                        }
                    )
                ),
                "",
                "Selection reasons: "
                + markdown_code_span(canonical_json(cohort["selection_reasons"])),
                "",
                "Excluded/unverifiable evidence: "
                + markdown_code_span(canonical_json(cohort["issues"])),
                "",
                "| Metric / split | Registered (non-synthetic) | Selected | Quality accepted / rejected / unknown | Evaluable / planned: observations; tasks | Mean original error | Mean scaled error | Mean realized savings | Original error interval |",
                "| --- | ---: | ---: | --- | --- | ---: | ---: | ---: | --- |",
            ]
        )
        notes = []
        for metric, details in cohort["metrics"].items():
            for split, values in details["splits"].items():
                errors = values["original"]["signed_error"]
                savings = values["original"]["realized_savings"]
                counts, interval = values["quality_gate_counts"], errors["interval"]
                bounds = (
                    f"{interval['confidence']:.1%}: [{interval['lower']:.6g}, {interval['upper']:.6g}]"
                    if interval["status"] == "computed"
                    else "insufficient tasks"
                )
                cells = [
                    f"{metric} / {split}",
                    values["non_synthetic_registration_count"],
                    values["selected_count"],
                    f"{counts['accepted']} / {counts['rejected']} / {counts['indeterminate']}",
                    f"{errors['observation_summary']['count']}/{errors['planned_observation_count']}; {errors['task_summary']['count']}/{errors['planned_task_count']}",
                    errors["task_summary"]["mean"],
                    values["scaled_error"]["task_summary"]["mean"],
                    savings["task_summary"]["mean"],
                    bounds,
                ]
                lines.append(
                    "| "
                    + " | ".join(
                        markdown_table_cell("unavailable" if value is None else str(value))
                        for value in cells
                    )
                    + " |"
                )
                accepted = values["quality_acceptance_rate"]
                eligible = values["quality_preserving"]["realized_savings"]
                notes.extend(
                    [
                        "",
                        f"{metric}/{split} quality acceptance among selected non-synthetic registrations: {'undefined (zero selected)' if accepted is None else f'{accepted:.1%}'}. Conditional mean savings: {eligible['task_summary']['mean']}; eligible tasks: {eligible['task_summary']['count']}/{eligible['planned_task_count']}.",
                        "",
                    ]
                )
            fitted = details["fit"]
            notes.extend(
                [
                    "",
                    f"{metric} diagnostic fit: {fitted['status']}; factor: {fitted['factor']}; fitting tasks: {fitted['task_count']}. {details['held_out_status']}.",
                    "",
                ]
            )
        lines.extend(notes)
    lines.extend(
        [
            "Every registration, original snapshot, outcome status, exclusion reason, task/observation denominator and held-out scaled error remains in JSON. No runtime coefficients were changed.",
            "",
        ]
    )
    return "\n".join(lines)
