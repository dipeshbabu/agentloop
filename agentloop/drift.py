"""Immutable reviewed baselines and conservative cohort drift comparisons."""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass

from agentloop.drift_types import DriftRule, label, timestamp
from agentloop.drift_windows import DRIFT_VERSION, seal, validate_window, verify


@dataclass(frozen=True, init=False)
class ReviewedBaseline:
    _json: str

    def __init__(self, window, *, rules: tuple[DriftRule, ...], review_ref: str):
        owned = validate_window(window)
        label(review_ref, "review_ref")
        if (
            not isinstance(rules, tuple)
            or not 1 <= len(rules) <= 32
            or any(not isinstance(rule, DriftRule) for rule in rules)
        ):
            raise ValueError("rules must be a tuple of 1..32 DriftRule declarations")
        if len({rule.metric for rule in rules}) != len(rules):
            raise ValueError("baseline metrics must be unique")
        value = seal(
            {
                "schema_version": DRIFT_VERSION,
                "kind": "agentloop.reviewed_baseline",
                "window": owned,
                "rules": [asdict(rule) for rule in rules],
                "review_ref": review_ref,
                "review_basis": "Explicit host review assertion; not an authenticated deployment approval.",
            }
        )
        object.__setattr__(self, "_json", json.dumps(value, sort_keys=True, allow_nan=False))

    def to_dict(self):
        return json.loads(self._json)

    @property
    def sha256(self):
        return self.to_dict()["sha256"]

    @classmethod
    def from_dict(cls, value):
        verify(value, "agentloop.reviewed_baseline")
        try:
            result = cls(
                value["window"],
                rules=tuple(DriftRule(**rule) for rule in value["rules"]),
                review_ref=value["review_ref"],
            )
            if result.to_dict() != value:
                raise ValueError("reviewed baseline fields do not match the supported contract")
            return result
        except (KeyError, TypeError) as exc:
            raise ValueError("invalid reviewed baseline") from exc


def _metric(cohort, name):
    aggregate = cohort["aggregate"]
    runs = aggregate["counts"]["runs"]
    result = {
        "interval": None,
        "observed_count": 0,
        "total_count": runs,
        "run_count": runs,
        "coverage": 0.0 if runs else None,
        "kind": "observed",
        "unit": "fraction",
    }
    value = None
    observed = runs
    if name == "latency_p95_ms":
        quantile = aggregate["latency"]["quantile_intervals"]["0.95"]
        result["unit"] = "ms"
        result["kind"] = "histogram_interval"
        result["timing_basis"] = aggregate["categories"]["runtime_basis"]
        if quantile is not None:
            result["interval"] = [quantile["lower_ms"], quantile["upper_ms"]]
    elif name == "latency_mean_ms":
        value, result["unit"] = aggregate["latency"]["mean_ms"], "ms"
        result["timing_basis"] = aggregate["categories"]["runtime_basis"]
    elif name in {"input_tokens_mean", "output_tokens_mean"}:
        key = name.removesuffix("_mean")
        complete = aggregate["completeness"][key]
        value = complete / runs if complete is not None and runs else None
        observed = runs - aggregate["counts"]["inexact_token_runs"]
        result["unit"] = "tokens_per_run"
    elif name == "known_cost_mean_usd":
        complete = aggregate["completeness"]["pricing_complete_model_cost_usd"]
        value = complete / runs if complete is not None and runs else None
        observed = runs - aggregate["counts"]["unknown_cost_runs"]
        result["unit"], result["kind"] = (
            "USD_per_run",
            "known_model_cost_including_calculated_estimates",
        )
        result["cost_sources"] = {
            **aggregate["cost"],
            "call_counts": aggregate["categories"]["cost_calls"],
            "inexact_token_runs": aggregate["counts"]["inexact_token_runs"],
        }
    elif name == "failure_rate":
        counts = aggregate["categories"]["outcomes"]
        observed = counts["success"] + counts["failure"]
        value = counts["failure"] / observed if observed else None
    elif name in {
        "timeout_rate",
        "abstention_rate",
        "finding_incidence",
        "instrumentation_complete_rate",
    }:
        key = {
            "timeout_rate": "timeout",
            "abstention_rate": "abstention",
            "finding_incidence": "finding",
            "instrumentation_complete_rate": "instrumentation",
        }[name]
        flag = cohort["flags"][key]
        observed = flag["known"]
        value = flag["true"] / observed if observed else None
    elif name.startswith("quality:"):
        quality = cohort["quality"].get(name.partition(":")[2], {"count": 0, "sum": 0})
        observed = quality["count"]
        value = quality["sum"] / observed if observed else None
        result["kind"] = "caller_supplied_quality_or_proxy"
    if value is not None:
        result["interval"] = [value, value]
    result.update(observed_count=observed, coverage=observed / runs if runs else None)
    return result


def _distribution(cohort, name):
    mix = cohort["mixes"][name.removesuffix("_mix")]
    return {
        "counts": mix["counts"],
        "observed_count": sum(mix["counts"].values()),
        "total_count": mix["total"],
        "run_count": cohort["aggregate"]["counts"]["runs"],
        "missing_count": mix["missing"],
        "overflow_count": mix["overflow"],
        "individual_evidence_missing_runs": cohort["individual_evidence_missing_runs"],
        "kind": "observed_distribution",
        "unit": "total_variation_distance",
        "coverage": sum(mix["counts"].values()) / mix["total"] if mix["total"] else None,
    }


def _compare_one(left, right, rule):
    result = {
        "metric": rule.metric,
        "rule": asdict(rule),
        "baseline": left,
        "current": right,
        "status": "indeterminate",
        "reason": None,
        "delta_interval": None,
        "relative_delta_pct": None,
        "threshold_interval": None,
    }
    if (
        min(left["observed_count"], right["observed_count"], left["run_count"], right["run_count"])
        < rule.minimum_observations
    ):
        result["reason"] = "small_or_missing_sample"
        return result
    if min(left["coverage"] or 0, right["coverage"] or 0) < rule.minimum_coverage:
        result["reason"] = "incomplete_evidence"
        return result
    if rule.metric.endswith("_mix"):
        if any(
            side["overflow_count"] or side["individual_evidence_missing_runs"]
            for side in (left, right)
        ):
            result["reason"] = "distribution_evidence_missing_or_overflowed"
            return result
        keys = left["counts"].keys() | right["counts"].keys()
        distance = (
            sum(
                abs(
                    left["counts"].get(key, 0) / left["observed_count"]
                    - right["counts"].get(key, 0) / right["observed_count"]
                )
                for key in keys
            )
            / 2
        )
        before, after = [0.0, 0.0], [distance, distance]
    else:
        before, after = left["interval"], right["interval"]
        if before is None or after is None:
            result["reason"] = "required_measurement_unavailable"
            return result
    delta = [after[0] - before[1], after[1] - before[0]]
    result["delta_interval"] = delta
    if before[0] == before[1] and before[0] != 0 and after[0] == after[1]:
        relative = 100 * ((after[0] - before[0]) / abs(before[0]))
        result["relative_delta_pct"] = relative if math.isfinite(relative) else None
    if rule.relative_pct is not None and before[0] <= 0 and rule.absolute is None:
        result["reason"] = "relative_threshold_has_zero_baseline"
        return result
    thresholds = [
        max(rule.absolute or 0, abs(value) * ((rule.relative_pct or 0) / 100)) for value in before
    ]
    if not all(
        math.isfinite(value) and math.isfinite(value + rule.noise_margin) for value in thresholds
    ):
        result["reason"] = "threshold_arithmetic_overflow"
        return result
    result["threshold_interval"] = thresholds
    worsening = delta if rule.direction == "increase" else [-delta[1], -delta[0]]
    if worsening[0] > thresholds[1] + rule.noise_margin:
        result.update(status="alert", reason="observed_change_exceeds_reviewed_threshold")
    elif worsening[1] <= max(0.0, thresholds[0] - rule.noise_margin):
        result.update(status="within_bounds", reason="observed_change_within_reviewed_threshold")
    else:
        result["reason"] = "quantile_resolution_or_reviewed_noise_margin"
    return result


def _quality_coverage(window):
    rows = [
        {
            "cohort": cohort,
            "metric": metric["name"],
            "kind": metric["kind"],
            "observed_count": values["quality"][metric["name"]]["count"],
            "total_count": values["aggregate"]["counts"]["runs"],
        }
        for cohort, values in window["cohorts"].items()
        for metric in window["quality_metrics"]
    ]
    status = (
        "missing"
        if not rows or not any(row["observed_count"] for row in rows)
        else "partial"
        if any(row["observed_count"] < row["total_count"] for row in rows)
        else "complete_for_declared_metrics"
    )
    return {"status": status, "metrics": rows}


def compare_drift(baseline: ReviewedBaseline, current, *, expected_baseline_sha256=None):
    if not isinstance(baseline, ReviewedBaseline):
        raise TypeError("baseline must be ReviewedBaseline")
    reference = baseline.to_dict()
    if expected_baseline_sha256 is not None and reference["sha256"] != expected_baseline_sha256:
        raise ValueError("reviewed baseline does not match the pinned hash")
    current = validate_window(current)
    previous = reference["window"]
    chronological = timestamp(current["spec"]["started_at"]) >= timestamp(
        previous["spec"]["ended_at"]
    )
    confounders = [
        key
        for key in ("workload_version", "config_version", "model_version")
        if previous["spec"][key] != current["spec"][key]
    ]
    metrics = []
    for cohort in sorted(previous["cohorts"].keys() | current["cohorts"].keys()):
        left, right = previous["cohorts"].get(cohort), current["cohorts"].get(cohort)
        for raw_rule in reference["rules"]:
            rule = DriftRule(**raw_rule)
            missing = left is None or right is None
            getter = _distribution if rule.metric.endswith("_mix") else _metric
            before = getter(left, rule.metric) if left is not None else None
            after = getter(right, rule.metric) if right is not None else None
            if rule.metric.startswith("quality:"):
                name = rule.metric.partition(":")[2]
                for item, window in ((before, previous), (after, current)):
                    if item is not None:
                        definition = next(
                            (
                                metric
                                for metric in window["quality_metrics"]
                                if metric["name"] == name
                            ),
                            None,
                        )
                        item["quality_definition"] = definition
                        item["kind"] = definition["kind"] if definition else "missing"
            incompatibility = None
            if not chronological:
                incompatibility = "overlapping_or_nonlater_window"
            elif missing:
                incompatibility = "cohort_missing_from_one_window"
            elif (
                left["aggregate"]["semantics"] != right["aggregate"]["semantics"]
                or left["aggregate"]["config"] != right["aggregate"]["config"]
            ):
                incompatibility = "sampling_retention_pricing_or_aggregation_changed"
            elif rule.metric.startswith("latency_") and {
                key for key, count in before["timing_basis"].items() if count
            } != {key for key, count in after["timing_basis"].items() if count}:
                incompatibility = "runtime_measurement_basis_changed"
            elif (
                rule.metric.startswith("quality:")
                and before["quality_definition"] != after["quality_definition"]
            ):
                incompatibility = "quality_definition_changed"
            elif rule.metric == "finding_incidence" and previous["settings"] != current["settings"]:
                incompatibility = "finding_analysis_configuration_changed"
            if incompatibility:
                result = {
                    "metric": rule.metric,
                    "rule": raw_rule,
                    "baseline": before,
                    "current": after,
                    "status": "incomparable",
                    "reason": incompatibility,
                    "delta_interval": None,
                    "relative_delta_pct": None,
                    "threshold_interval": None,
                }
            else:
                result = _compare_one(before, after, rule)
            result["cohort"] = cohort
            result["window_membership"] = {
                "baseline": left["time_membership"] if left is not None else None,
                "current": right["time_membership"] if right is not None else None,
            }
            metrics.append(result)
    statuses = {metric["status"] for metric in metrics}
    status = (
        "alert"
        if "alert" in statuses
        else "indeterminate"
        if statuses & {"indeterminate", "incomparable"} or not metrics
        else "within_bounds"
    )
    return seal(
        {
            "schema_version": DRIFT_VERSION,
            "kind": "agentloop.drift_report",
            "status": status,
            "baseline_sha256": reference["sha256"],
            "baseline_review_ref": reference["review_ref"],
            "baseline_window": {"sha256": previous["sha256"], **previous["spec"]},
            "current_window": {"sha256": current["sha256"], **current["spec"]},
            "potential_confounders": confounders,
            "metrics": metrics,
            "producer_markers": {
                "baseline": {
                    cohort: data["producer_markers"] for cohort, data in previous["cohorts"].items()
                },
                "current": {
                    cohort: data["producer_markers"] for cohort, data in current["cohorts"].items()
                },
            },
            "quality_evidence": {
                "baseline_definitions": previous["quality_metrics"],
                "current_definitions": current["quality_metrics"],
                "baseline": _quality_coverage(previous),
                "current": _quality_coverage(current),
            },
            "interpretation": "Descriptive threshold comparisons, not causal attribution, significance tests or a correctness inference from outcome drift. No deployment or notification action is taken.",
        }
    )
