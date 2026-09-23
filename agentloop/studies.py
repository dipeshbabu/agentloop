"""Deterministic paired study summaries using only the Python standard library."""

from __future__ import annotations

import glob
import json
import math
import random
import statistics
from collections import Counter
from pathlib import Path
from typing import Any

from agentloop.costs import is_cost_evaluable
from agentloop.markdown import markdown_code_span, markdown_heading, markdown_table_cell
from agentloop.tokens import is_token_basis_evaluable
from agentloop.tracer import AgentTrace

STUDY_SCHEMA_VERSION = "1.0"
_METRICS = (
    "success",
    "quality_score",
    "runtime_ms",
    "input_tokens",
    "output_tokens",
    "model_call_count",
    "tool_call_count",
    "retry_count",
    "cost_usd",
)


class StudyValidationError(ValueError):
    """The manifest or one of its source traces is not a valid study input."""


def _quantile_sorted(ordered: list[float], fraction: float) -> float | None:
    """Linearly interpolate at (n - 1) * fraction, including singleton samples."""
    if not ordered:
        return None
    position = (len(ordered) - 1) * fraction
    lower, upper = math.floor(position), math.ceil(position)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def summarize_values(values: list[float | None]) -> dict[str, Any]:
    known = sorted(value for value in values if value is not None)
    return {
        "count": len(known),
        "missing_count": len(values) - len(known),
        "mean": statistics.fmean(known) if known else None,
        "median": _quantile_sorted(known, 0.5),
        "min": known[0] if known else None,
        "max": known[-1] if known else None,
        "p05": _quantile_sorted(known, 0.05),
        "p25": _quantile_sorted(known, 0.25),
        "p75": _quantile_sorted(known, 0.75),
        "p95": _quantile_sorted(known, 0.95),
    }


def _bootstrap(values: list[float | None], config: dict[str, Any]) -> dict[str, Any]:
    known = [value for value in values if value is not None]
    samples, seed, confidence = config["samples"], config["seed"], config["confidence"]
    result = {
        "method": "paired_percentile_bootstrap_mean_delta",
        **config,
        "pair_count": len(known),
        "lower": None,
        "upper": None,
        "status": "insufficient_pairs",
    }
    if len(known) < 2:
        return result
    # Reproducible statistical resampling; this generator creates no credentials.
    rng = random.Random(seed)  # nosec B311
    means = sorted(statistics.fmean(rng.choices(known, k=len(known))) for _ in range(samples))
    tail = (1 - confidence) / 2
    result.update(
        lower=_quantile_sorted(means, tail),
        upper=_quantile_sorted(means, 1 - tail),
        status="computed",
    )
    return result


def _bootstrap_config(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) - {"samples", "seed", "confidence"}:
        raise StudyValidationError("bootstrap must specify samples, seed, and/or confidence")
    config = {"samples": 1000, "seed": 0, "confidence": 0.95, **value}
    if type(config["samples"]) is not int or not 1 <= config["samples"] <= 10000:
        raise StudyValidationError("bootstrap.samples must be an integer between 1 and 10000")
    if type(config["seed"]) is not int:
        raise StudyValidationError("bootstrap.seed must be an integer")
    confidence = config["confidence"]
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, int | float)
        or not 0 < confidence < 1
    ):
        raise StudyValidationError("bootstrap.confidence must be between 0 and 1")
    return config


def _validate_manifest(manifest: Any) -> None:
    if not isinstance(manifest, dict) or manifest.get("schema_version") != STUDY_SCHEMA_VERSION:
        raise StudyValidationError("study schema_version must be 1.0")
    if set(manifest) - {
        "schema_version",
        "name",
        "baseline",
        "conditions",
        "pairing_keys",
        "bootstrap",
    }:
        raise StudyValidationError("unknown study manifest field")
    if not isinstance(manifest.get("name"), str) or not manifest["name"].strip():
        raise StudyValidationError("study name must be a nonempty string")
    conditions = manifest.get("conditions")
    if not isinstance(conditions, dict) or len(conditions) < 2:
        raise StudyValidationError("at least two conditions are required")
    if not isinstance(manifest.get("baseline"), str) or manifest["baseline"] not in conditions:
        raise StudyValidationError("baseline must name one of the conditions")
    keys = manifest.get("pairing_keys")
    if (
        not isinstance(keys, list)
        or not keys
        or any(not isinstance(key, str) or not key for key in keys)
        or len(set(keys)) != len(keys)
    ):
        raise StudyValidationError("pairing_keys must be a nonempty list of distinct metadata keys")
    for name, patterns in conditions.items():
        if (
            not isinstance(name, str)
            or not name.strip()
            or not isinstance(patterns, list)
            or not patterns
            or any(not isinstance(pattern, str) or not pattern for pattern in patterns)
        ):
            raise StudyValidationError(
                "each named condition must contain a nonempty list of trace paths/globs"
            )
    _bootstrap_config(manifest.get("bootstrap"))


def _pair_key(metadata: dict[str, Any], keys: list[str]) -> str | None:
    values = []
    for key in keys:
        value = metadata.get(key)
        if not isinstance(value, str | int | float | bool) or (
            isinstance(value, float) and not math.isfinite(value)
        ):
            return None
        # The type tag prevents booleans, numbers, and strings from aliasing.
        values.append([type(value).__name__, value])
    return json.dumps(values, separators=(",", ":"))


def _run(path: Path, keys: list[str]) -> dict[str, Any]:
    try:
        trace = AgentTrace.from_dict(json.loads(path.read_text(encoding="utf-8")))
        report = trace.report()
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise StudyValidationError(f"invalid trace {path}: {exc}") from exc
    quality = trace.metadata.get("quality_score")
    if quality is not None and (
        isinstance(quality, bool) or not isinstance(quality, int | float) or not 0 <= quality <= 1
    ):
        raise StudyValidationError(
            f"{path}: metadata.quality_score must be a number between 0 and 1"
        )
    success = trace.metadata.get("success")
    if success is not None and type(success) is not bool:
        raise StudyValidationError(f"{path}: metadata.success must be a boolean")
    failures = [event for event in trace.events if event.status == "error"]
    categories = Counter(
        str(event.metadata.get("error_type") or "event_error") for event in failures
    )
    if success is False and not failures:
        categories["task_failure"] += 1
    # No error spans means execution succeeded, not that task quality was verified.
    success_value = not failures if success is None else success and not failures
    success_basis = "task_metadata_and_execution" if success is not None else "execution_only"
    execution = report.get("execution")
    if execution is not None:
        status = execution.get("status") if execution.get("schema_status") == "supported" else None
        if status in {"failed", "cancelled", "interrupted"}:
            success_value = False
            categories[f"workflow_{status}"] += 1
        elif status != "completed":
            success_value = False if failures or success is False else None
        success_basis = "task_metadata_and_workflow" if success is not None else "workflow_status"
    quality_evidence = report.get("quality_evidence")
    if quality_evidence is not None:
        quality = quality_evidence["score"]
        if (
            failures
            or success is False
            or quality_evidence.get("summary", {}).get("execution_failed_count", 0) > 0
            or execution is not None
            and execution.get("status") in {"failed", "cancelled", "interrupted"}
        ):
            success_value = False
        elif quality_evidence.get("status") == "complete":
            success_value = quality_evidence["passed"]
        else:
            success_value = None
        success_basis = "versioned_quality_and_execution"
        if not quality_evidence.get("passed"):
            categories[
                "quality_rejected"
                if quality_evidence.get("status") == "complete"
                else "quality_" + quality_evidence.get("status", "invalid")
            ] += 1
    cost_known = is_cost_evaluable(report["cost_status"]) and is_token_basis_evaluable(
        report["token_status"]
    )
    return {
        "path": str(path),
        **(
            {"quality_evidence": quality_evidence, "decision_count": report["decision_count"]}
            if quality_evidence is not None
            else {}
        ),
        **({"execution": report["execution"]} if "execution" in report else {}),
        **({"stages": report["stages"]} if "stages" in report else {}),
        "run_id": trace.run_id,
        "pair_key": _pair_key(trace.metadata, keys),
        "pairing_metadata": {key: trace.metadata.get(key) for key in keys},
        "metrics": {
            **(
                {"decision_count": report["decision_count"]} if quality_evidence is not None else {}
            ),
            "success": float(success_value) if success_value is not None else None,
            "quality_score": quality,
            "runtime_ms": report["total_runtime_ms"],
            "input_tokens": report["input_tokens"],
            "output_tokens": report["output_tokens"],
            "model_call_count": report["model_call_count"],
            "tool_call_count": report["tool_call_count"],
            "retry_count": report["retry_count"],
            "cost_usd": report["estimated_cost_usd"] if cost_known else None,
        },
        "success_basis": success_basis,
        "cost_status": report["cost_status"],
        "token_status": report["token_status"],
        "operation_counts": report.get("operation_counts", {}),
        "failure_categories": dict(sorted(categories.items())),
        "synthetic": trace.metadata.get("synthetic") is True,
    }


def _condition(runs: list[dict[str, Any]]) -> dict[str, Any]:
    categories: Counter[str] = Counter()
    operations: Counter[str] = Counter()
    for run in runs:
        categories.update(run["failure_categories"])
        operations.update(run["operation_counts"])
    metrics = (
        *_METRICS,
        *(("decision_count",) if any("decision_count" in run["metrics"] for run in runs) else ()),
    )
    return {
        "run_count": len(runs),
        "metrics": {
            key: summarize_values([run["metrics"].get(key) for run in runs]) for key in metrics
        },
        "cost_status_counts": dict(sorted(Counter(run["cost_status"] for run in runs).items())),
        "token_status_counts": dict(sorted(Counter(run["token_status"] for run in runs).items())),
        "success_basis_counts": dict(sorted(Counter(run["success_basis"] for run in runs).items())),
        "operation_counts": dict(sorted(operations.items())),
        "failure_categories": dict(sorted(categories.items())),
        "runs": runs,
    }


def _compare(
    baseline: list[dict[str, Any]],
    candidate: list[dict[str, Any]],
    bootstrap: dict[str, Any] | None,
) -> dict[str, Any]:
    metrics = (
        *_METRICS,
        *(
            ("decision_count",)
            if any(
                "decision_count" in run["metrics"] for runs in (baseline, candidate) for run in runs
            )
            else ()
        ),
    )
    groups: dict[str, dict[str, list[dict[str, Any]]]] = {"baseline": {}, "candidate": {}}
    unmatched = []
    for side, runs in (("baseline", baseline), ("candidate", candidate)):
        for run in runs:
            if run["pair_key"] is None:
                unmatched.append(
                    {
                        "side": side,
                        "run_id": run["run_id"],
                        "path": run["path"],
                        "reason": "missing_or_invalid_pairing_metadata",
                    }
                )
            else:
                groups[side].setdefault(run["pair_key"], []).append(run)
    pairs = []
    for key in sorted(set(groups["baseline"]) | set(groups["candidate"])):
        left, right = groups["baseline"].get(key, []), groups["candidate"].get(key, [])
        if len(left) != 1 or len(right) != 1:
            reason = (
                "duplicate_pairing_key"
                if len(left) > 1 or len(right) > 1
                else "missing_counterpart"
            )
            for side, runs in (("baseline", left), ("candidate", right)):
                unmatched.extend(
                    {
                        "side": side,
                        "run_id": run["run_id"],
                        "path": run["path"],
                        "pairing_metadata": run["pairing_metadata"],
                        "reason": reason,
                    }
                    for run in runs
                )
            continue
        before, after = left[0], right[0]
        deltas = {
            metric: (
                after["metrics"][metric] - before["metrics"][metric]
                if after["metrics"].get(metric) is not None
                and before["metrics"].get(metric) is not None
                else None
            )
            for metric in metrics
        }
        pairs.append(
            {
                "baseline_run_id": before["run_id"],
                "candidate_run_id": after["run_id"],
                "pairing_metadata": before["pairing_metadata"],
                "deltas": deltas,
            }
        )
    summaries = {}
    for metric in metrics:
        values = [pair["deltas"][metric] for pair in pairs]
        summaries[metric] = summarize_values(values)
        if bootstrap:
            summaries[metric]["bootstrap_interval"] = _bootstrap(values, bootstrap)
    return {
        "pair_count": len(pairs),
        "delta_direction": "candidate_minus_baseline",
        "metrics": summaries,
        "pairs": pairs,
        "unmatched": sorted(unmatched, key=lambda run: (run["side"], run["run_id"], run["path"])),
    }


def summarize_study(manifest_path: str | Path) -> dict[str, Any]:
    path = Path(manifest_path).resolve()
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise StudyValidationError(f"cannot read study manifest: {exc}") from exc
    _validate_manifest(manifest)
    bootstrap = _bootstrap_config(manifest.get("bootstrap"))
    conditions = {}
    seen_ids: set[str] = set()
    for name, patterns in sorted(manifest["conditions"].items()):
        paths: set[Path] = set()
        for pattern in patterns:
            resolved_pattern = str(path.parent / pattern)
            matches = [
                Path(match).resolve()
                for match in glob.glob(resolved_pattern, recursive=True)
                if Path(match).is_file()
            ]
            if not matches:
                raise StudyValidationError(
                    f"condition {name!r}: trace pattern matched no files: {pattern}"
                )
            paths.update(matches)
        runs = [_run(trace_path, manifest["pairing_keys"]) for trace_path in sorted(paths)]
        for run in runs:
            if run["run_id"] in seen_ids:
                raise StudyValidationError(f"duplicate run_id in study: {run['run_id']}")
            seen_ids.add(run["run_id"])
        runs.sort(key=lambda run: (run["pair_key"] or "", run["run_id"]))
        conditions[name] = _condition(runs)
    baseline = manifest["baseline"]
    return {
        "schema_version": STUDY_SCHEMA_VERSION,
        "name": manifest["name"],
        "manifest": manifest,
        "baseline_condition": baseline,
        "conditions": conditions,
        "comparisons": {
            name: _compare(conditions[baseline]["runs"], condition["runs"], bootstrap)
            for name, condition in conditions.items()
            if name != baseline
        },
        "includes_synthetic_data": any(
            run["synthetic"] for condition in conditions.values() for run in condition["runs"]
        ),
        "interpretation": "Descriptive paired comparisons. Bootstrap intervals assume exchangeable independent pairs; repeated tasks may require cluster-aware analysis. No significance decision is made.",
    }


def study_to_markdown(report: dict[str, Any]) -> str:
    lines = [
        f"# Study: {markdown_heading(report['name'])}",
        "",
        report["interpretation"],
        "",
        "Deltas are candidate minus baseline. Negative latency/cost deltas indicate reductions.",
        "",
        "Cost statistics use evaluable runs or pairs only; counts disclose missing observations.",
        "",
    ]
    if report["includes_synthetic_data"]:
        lines.extend(
            [
                "This study includes synthetic data; fixture results are not application performance evidence.",
                "",
            ]
        )
    for name, condition in report["conditions"].items():
        lines.extend(
            [f"## Condition: {markdown_heading(name)}", "", f"Runs: {condition['run_count']}", ""]
        )
        lines.extend(_metric_table(condition["metrics"]))
        lines.extend(
            [
                "",
                "Failure categories: "
                + markdown_code_span(json.dumps(condition["failure_categories"], sort_keys=True)),
                "Success basis: "
                + markdown_code_span(json.dumps(condition["success_basis_counts"], sort_keys=True)),
                "",
            ]
        )
    for name, comparison in report["comparisons"].items():
        lines.extend(
            [
                f"## Paired comparison: {markdown_heading(name)}",
                "",
                f"Matched pairs: {comparison['pair_count']}",
                "",
            ]
        )
        lines.extend(_metric_table(comparison["metrics"]))
        lines.extend(
            ["", "### Unmatched runs", "", "| Side | Run | Reason |", "| --- | --- | --- |"]
        )
        for run in comparison["unmatched"]:
            lines.append(
                "| "
                + " | ".join(markdown_table_cell(run[key]) for key in ("side", "run_id", "reason"))
                + " |"
            )
        if not comparison["unmatched"]:
            lines.append("| — | — | None |")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _metric_table(metrics: dict[str, Any]) -> list[str]:
    def display(value: Any) -> str:
        return "unavailable" if value is None else f"{value:.6g}"

    lines = [
        "| Metric | Count | Missing | Mean | Median | P05 | P95 | Bootstrap interval |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for metric, summary in metrics.items():
        interval = summary.get("bootstrap_interval")
        bounds = (
            "not requested"
            if interval is None
            else (
                f"{interval['confidence']:.1%}: [{display(interval['lower'])}, {display(interval['upper'])}]"
                if interval["status"] == "computed"
                else "insufficient pairs"
            )
        )
        cells = [
            metric,
            str(summary["count"]),
            str(summary["missing_count"]),
            *(display(summary[key]) for key in ("mean", "median", "p05", "p95")),
            bounds,
        ]
        lines.append("| " + " | ".join(markdown_table_cell(cell) for cell in cells) + " |")
    return lines
