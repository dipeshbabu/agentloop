from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agentloop.markdown import markdown_code_span, markdown_table_cell


@dataclass(frozen=True)
class ReplayGates:
    max_cost_regression_pct: float = 0.0
    max_latency_regression_pct: float = 0.0
    min_latency_improvement_pct: float = 0.0
    min_cost_improvement_pct: float = 0.0
    require_retry_non_increase: bool = True
    require_schema_valid: bool = False
    min_quality_score: float | None = None


def build_replay_report(
    baseline_trace: Any,
    candidate_trace: Any,
    *,
    gates: ReplayGates | None = None,
    quality_report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    gates = gates or ReplayGates()
    baseline_report = baseline_trace.report()
    candidate_report = candidate_trace.report()
    baseline = _trace_summary(baseline_trace, baseline_report)
    candidate = _trace_summary(candidate_trace, candidate_report)
    if quality_report is not None:
        baseline["quality_score"] = float(quality_report.get("baseline_score", 0.0))
        candidate["quality_score"] = float(quality_report.get("candidate_score", 0.0))
    cost_evaluable = not (
        bool(baseline.get("has_unknown_cost")) or bool(candidate.get("has_unknown_cost"))
    )
    deltas = _deltas(baseline, candidate)
    if not cost_evaluable:
        # The cost totals are lower bounds when a model is unpriced, so their
        # delta and percentages are not a valid comparison — expose them as
        # unavailable (None) rather than let a consumer read lower-bound
        # arithmetic as a real number. The gate stays indeterminate below.
        deltas["cost_usd_delta"] = None
        deltas["cost_improvement_pct"] = None
        deltas["cost_regression_pct"] = None
    gate_results = _gate_results(
        deltas,
        baseline,
        candidate,
        gates,
        quality_report=quality_report,
        cost_evaluable=cost_evaluable,
    )
    passed = all(item["passed"] for item in gate_results)
    indeterminate_gates = [item["name"] for item in gate_results if item.get("indeterminate")]

    return {
        "baseline": baseline,
        "candidate": candidate,
        "deltas": deltas,
        "gates": {
            "passed": passed,
            "cost_evaluable": cost_evaluable,
            "indeterminate": indeterminate_gates,
            "config": {
                "max_cost_regression_pct": gates.max_cost_regression_pct,
                "max_latency_regression_pct": gates.max_latency_regression_pct,
                "min_latency_improvement_pct": gates.min_latency_improvement_pct,
                "min_cost_improvement_pct": gates.min_cost_improvement_pct,
                "require_retry_non_increase": gates.require_retry_non_increase,
                "require_schema_valid": gates.require_schema_valid,
                "min_quality_score": gates.min_quality_score,
            },
            "results": gate_results,
        },
        "quality": quality_report,
        "summary": _summary(deltas, passed, cost_evaluable=cost_evaluable),
    }


def replay_report_to_markdown(report: dict[str, Any]) -> str:
    baseline = report["baseline"]
    candidate = report["candidate"]
    deltas = report["deltas"]
    gates = report["gates"]
    status = "passed" if gates["passed"] else "failed"

    lines = [
        "# AgentLoop Replay Report",
        "",
        f"- Status: {status}",
        f"- Baseline: {markdown_code_span(baseline['name'])} "
        f"{markdown_code_span(baseline['run_id'])}",
        f"- Candidate: {markdown_code_span(candidate['name'])} "
        f"{markdown_code_span(candidate['run_id'])}",
        "",
        "## Metric Deltas",
        "",
        "| Metric | Baseline | Candidate | Delta | Improvement |",
        "|---|---:|---:|---:|---:|",
        _metric_row(
            "Runtime",
            baseline["runtime_ms"],
            candidate["runtime_ms"],
            deltas["runtime_ms_delta"],
            deltas["latency_improvement_pct"],
            "ms",
        ),
        _metric_row(
            "Cost",
            baseline["estimated_cost_usd"],
            candidate["estimated_cost_usd"],
            deltas["cost_usd_delta"],
            deltas["cost_improvement_pct"],
            "usd",
        ),
        _metric_row(
            "Input tokens",
            baseline["input_tokens"],
            candidate["input_tokens"],
            deltas["input_tokens_delta"],
            deltas["input_token_improvement_pct"],
            "count",
        ),
        _metric_row(
            "Output tokens",
            baseline["output_tokens"],
            candidate["output_tokens"],
            deltas["output_tokens_delta"],
            deltas["output_token_improvement_pct"],
            "count",
        ),
        _metric_row(
            "Retries",
            baseline["retry_count"],
            candidate["retry_count"],
            deltas["retry_count_delta"],
            deltas["retry_improvement_pct"],
            "count",
        ),
        _metric_row(
            "Tool calls",
            baseline["tool_call_count"],
            candidate["tool_call_count"],
            deltas["tool_call_count_delta"],
            deltas["tool_call_improvement_pct"],
            "count",
        ),
        _metric_row(
            "Model calls",
            baseline["model_call_count"],
            candidate["model_call_count"],
            deltas["model_call_count_delta"],
            deltas["model_call_improvement_pct"],
            "count",
        ),
    ]
    if (
        baseline.get("schema_validity_pct") is not None
        or candidate.get("schema_validity_pct") is not None
    ):
        lines.append(
            _metric_row(
                "Schema validity",
                baseline.get("schema_validity_pct") or 0.0,
                candidate.get("schema_validity_pct") or 0.0,
                deltas["schema_validity_delta_pct"],
                deltas["schema_validity_improvement_pct"],
                "pct",
            )
        )
    if baseline.get("quality_score") is not None or candidate.get("quality_score") is not None:
        lines.append(
            _metric_row(
                "Quality score",
                baseline.get("quality_score") or 0.0,
                candidate.get("quality_score") or 0.0,
                deltas["quality_score_delta"],
                deltas["quality_score_improvement_pct"],
                "score",
            )
        )
    if report.get("quality"):
        quality = report["quality"]
        lines.extend(
            [
                "",
                "## Quality",
                "",
                f"- Cases: {quality['case_count']}",
                f"- Candidate score: {quality['candidate_score']:.4f}",
                f"- Quality delta: {quality['quality_delta']:.4f}",
                f"- Failed cases: {quality['failed_case_count']}",
            ]
        )
    lines.extend(
        [
            "",
            "## Gates",
            "",
            "| Gate | Status | Detail |",
            "|---|---|---|",
        ]
    )
    for item in gates["results"]:
        if item.get("indeterminate"):
            gate_status = "indeterminate"
        else:
            gate_status = "pass" if item["passed"] else "fail"
        lines.append(
            f"| {markdown_table_cell(item['name'])} | {gate_status} | "
            f"{markdown_table_cell(item['detail'])} |"
        )
    return "\n".join(lines).rstrip() + "\n"


def _trace_summary(trace: Any, report: dict[str, Any]) -> dict[str, Any]:
    metadata = getattr(trace, "metadata", {}) or {}
    cost = report.get("cost_breakdown") or {}
    return {
        "run_id": trace.run_id,
        "name": trace.name,
        "runtime_ms": float(report.get("total_runtime_ms", 0.0) or 0.0),
        "estimated_cost_usd": float(report.get("estimated_cost_usd", 0.0) or 0.0),
        # Carried so the cost gates can tell "no cost" apart from "cost unknown":
        # estimated_cost_usd is the *known* portion, has_unknown_cost flags that
        # some model calls had no available rate, and cost_status summarizes
        # completeness (see costs.py / metrics.py).
        "has_unknown_cost": bool(cost.get("has_unknown_cost", False)),
        "cost_status": report.get("cost_status", cost.get("cost_status", "complete")),
        "input_tokens": int(report.get("input_tokens", 0) or 0),
        "output_tokens": int(report.get("output_tokens", 0) or 0),
        "retry_count": int(report.get("retry_count", 0) or 0),
        "tool_call_count": int(report.get("tool_call_count", 0) or 0),
        "model_call_count": int(report.get("model_call_count", 0) or 0),
        "schema_validity_pct": _optional_float(
            report.get("schema_validity_pct")
            if report.get("schema_validity_pct") is not None
            else metadata.get("schema_validity_pct")
        ),
        "schema_valid": _optional_bool(
            report.get("schema_valid")
            if report.get("schema_valid") is not None
            else metadata.get("schema_valid")
        ),
        "quality_score": _optional_float(
            report.get("quality_score")
            if report.get("quality_score") is not None
            else metadata.get("quality_score")
        ),
    }


def _deltas(baseline: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    base_runtime = float(baseline.get("runtime_ms", baseline.get("total_runtime_ms", 0.0)) or 0.0)
    cand_runtime = float(candidate.get("runtime_ms", candidate.get("total_runtime_ms", 0.0)) or 0.0)
    base_cost = float(baseline.get("estimated_cost_usd", 0.0) or 0.0)
    cand_cost = float(candidate.get("estimated_cost_usd", 0.0) or 0.0)
    base_input = int(baseline.get("input_tokens", 0) or 0)
    cand_input = int(candidate.get("input_tokens", 0) or 0)
    base_output = int(baseline.get("output_tokens", 0) or 0)
    cand_output = int(candidate.get("output_tokens", 0) or 0)
    base_retries = int(baseline.get("retry_count", 0) or 0)
    cand_retries = int(candidate.get("retry_count", 0) or 0)
    base_tools = int(baseline.get("tool_call_count", 0) or 0)
    cand_tools = int(candidate.get("tool_call_count", 0) or 0)
    base_models = int(baseline.get("model_call_count", 0) or 0)
    cand_models = int(candidate.get("model_call_count", 0) or 0)
    base_schema = _optional_float(baseline.get("schema_validity_pct"))
    if base_schema is None:
        base_schema = _schema_validity_from_bool(baseline.get("schema_valid"))
    cand_schema = _optional_float(candidate.get("schema_validity_pct"))
    if cand_schema is None:
        cand_schema = _schema_validity_from_bool(candidate.get("schema_valid"))
    base_quality = _optional_float(baseline.get("quality_score"))
    cand_quality = _optional_float(candidate.get("quality_score"))

    return {
        "runtime_ms_delta": round(cand_runtime - base_runtime, 3),
        "latency_improvement_pct": _improvement_pct(base_runtime, cand_runtime),
        "latency_regression_pct": _regression_pct(base_runtime, cand_runtime),
        "cost_usd_delta": round(cand_cost - base_cost, 6),
        "cost_improvement_pct": _improvement_pct(base_cost, cand_cost),
        "cost_regression_pct": _regression_pct(base_cost, cand_cost),
        "input_tokens_delta": cand_input - base_input,
        "input_token_improvement_pct": _improvement_pct(base_input, cand_input),
        "output_tokens_delta": cand_output - base_output,
        "output_token_improvement_pct": _improvement_pct(base_output, cand_output),
        "retry_count_delta": cand_retries - base_retries,
        "retry_improvement_pct": _improvement_pct(base_retries, cand_retries),
        "tool_call_count_delta": cand_tools - base_tools,
        "tool_call_improvement_pct": _improvement_pct(base_tools, cand_tools),
        "model_call_count_delta": cand_models - base_models,
        "model_call_improvement_pct": _improvement_pct(base_models, cand_models),
        "schema_validity_delta_pct": round((cand_schema or 0.0) - (base_schema or 0.0), 3),
        "schema_validity_improvement_pct": _relative_gain_pct(base_schema, cand_schema),
        "quality_score_delta": round((cand_quality or 0.0) - (base_quality or 0.0), 4),
        "quality_score_improvement_pct": _relative_gain_pct(base_quality, cand_quality),
    }


def _gate_results(
    deltas: dict[str, Any],
    baseline: dict[str, Any],
    candidate: dict[str, Any],
    gates: ReplayGates,
    *,
    quality_report: dict[str, Any] | None = None,
    cost_evaluable: bool = True,
) -> list[dict[str, Any]]:
    if cost_evaluable:
        cost_regression_gate = _gate(
            "cost_regression",
            deltas["cost_regression_pct"] <= gates.max_cost_regression_pct,
            f"{deltas['cost_regression_pct']:.2f}% regression "
            f"<= {gates.max_cost_regression_pct:.2f}% allowed",
        )
        cost_improvement_gate = _gate(
            "cost_improvement",
            deltas["cost_improvement_pct"] >= gates.min_cost_improvement_pct,
            f"{deltas['cost_improvement_pct']:.2f}% improvement "
            f">= {gates.min_cost_improvement_pct:.2f}% required",
        )
    else:
        # A missing rate is not itself evidence of a regression, so the
        # regression gate stays non-failing rather than blocking every
        # latency-only optimization that uses an unpriced model. But a *required*
        # cost improvement can't be verified, so it must fail — we won't claim an
        # improvement we can't compute.
        cost_regression_gate = _indeterminate_cost_gate("cost_regression", fail=False)
        cost_improvement_gate = _indeterminate_cost_gate(
            "cost_improvement", fail=gates.min_cost_improvement_pct > 0
        )
    results = [
        _gate(
            "latency_regression",
            deltas["latency_regression_pct"] <= gates.max_latency_regression_pct,
            f"{deltas['latency_regression_pct']:.2f}% regression <= {gates.max_latency_regression_pct:.2f}% allowed",
        ),
        cost_regression_gate,
        _gate(
            "latency_improvement",
            deltas["latency_improvement_pct"] >= gates.min_latency_improvement_pct,
            f"{deltas['latency_improvement_pct']:.2f}% improvement >= {gates.min_latency_improvement_pct:.2f}% required",
        ),
        cost_improvement_gate,
    ]
    if gates.require_retry_non_increase:
        base_retries = int(baseline.get("retry_count", 0) or 0)
        cand_retries = int(candidate.get("retry_count", 0) or 0)
        results.append(
            _gate(
                "retry_non_increase",
                cand_retries <= base_retries,
                f"{cand_retries} candidate retries <= {base_retries} baseline retries",
            )
        )
    if gates.require_schema_valid:
        schema_valid = _candidate_schema_valid(candidate)
        results.append(
            _gate(
                "schema_valid",
                schema_valid,
                "candidate schema validity passed"
                if schema_valid
                else "candidate schema validity missing or failed",
            )
        )
    if quality_report is not None:
        fixture_passed = bool(quality_report.get("passed"))
        failed_count = int(quality_report.get("failed_case_count", 0) or 0)
        results.append(
            _gate(
                "quality_fixtures",
                fixture_passed,
                "supplied quality fixtures passed"
                if fixture_passed
                else f"{failed_count} supplied quality fixture case(s) failed",
            )
        )
    if gates.min_quality_score is not None:
        quality_score = _optional_float(candidate.get("quality_score"))
        passed = quality_score is not None and quality_score >= gates.min_quality_score
        detail = (
            f"{quality_score:.4f} candidate quality >= {gates.min_quality_score:.4f} required"
            if quality_score is not None
            else f"candidate quality score missing; {gates.min_quality_score:.4f} required"
        )
        results.append(_gate("quality_score", passed, detail))
    return results


def _gate(name: str, passed: bool, detail: str, *, indeterminate: bool = False) -> dict[str, Any]:
    return {"name": name, "passed": passed, "detail": detail, "indeterminate": indeterminate}


def _indeterminate_cost_gate(name: str, *, fail: bool) -> dict[str, Any]:
    """A cost gate that couldn't be evaluated because a model cost is unknown.

    Rather than silently comparing coerced/lower-bound numbers, the gate is
    marked ``indeterminate``. It fails only when the caller explicitly required a
    cost outcome (``fail``) that therefore can't be verified; otherwise it is a
    non-failing informational gate.
    """
    detail = (
        "cost could not be evaluated: a model call has no known pricing "
        "(see cost_breakdown.unknown_models). "
        + ("failing because a cost outcome was required." if fail else "not gating.")
    )
    return _gate(name, passed=not fail, detail=detail, indeterminate=True)


def _summary(deltas: dict[str, Any], passed: bool, *, cost_evaluable: bool = True) -> str:
    status = "passed" if passed else "failed"
    cost_phrase = (
        f"cost improvement {deltas['cost_improvement_pct']:.2f}%"
        if cost_evaluable
        else "cost improvement unavailable (unknown model pricing)"
    )
    return (
        f"Replay {status}: latency improvement {deltas['latency_improvement_pct']:.2f}%, "
        f"{cost_phrase}, "
        f"retry delta {deltas['retry_count_delta']}."
    )


def _improvement_pct(baseline: float, candidate: float) -> float:
    if baseline == 0:
        return 0.0 if candidate == 0 else -100.0
    return round(((baseline - candidate) / baseline) * 100, 2)


def _relative_gain_pct(baseline: float | None, candidate: float | None) -> float:
    if baseline is None:
        return 0.0 if candidate is None else 100.0
    if baseline == 0:
        return 0.0 if not candidate else 100.0
    return round((((candidate or 0.0) - baseline) / baseline) * 100, 2)


def _regression_pct(baseline: float, candidate: float) -> float:
    if baseline == 0:
        return 0.0 if candidate == 0 else 100.0
    return round(max(0.0, ((candidate - baseline) / baseline) * 100), 2)


def _metric_row(
    metric: str,
    baseline: float,
    candidate: float,
    delta: float | None,
    improvement: float | None,
    kind: str,
) -> str:
    improvement_cell = "unavailable" if improvement is None else f"{improvement:.2f}%"
    return (
        f"| {metric} | {_format_metric(baseline, kind)} | {_format_metric(candidate, kind)} | "
        f"{_format_metric(delta, kind)} | {improvement_cell} |"
    )


def _format_metric(value: float | None, kind: str) -> str:
    if value is None:
        return "unavailable"
    if kind == "usd":
        return f"${value:.6f}"
    if kind == "ms":
        return f"{value:.3f}ms"
    if kind == "pct":
        return f"{value:.3f}%"
    if kind == "score":
        return f"{value:.4f}"
    return str(int(value))


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def _optional_bool(value: Any) -> bool | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "pass", "passed", "valid"}
    return bool(value)


def _schema_validity_from_bool(value: Any) -> float | None:
    parsed = _optional_bool(value)
    if parsed is None:
        return None
    return 100.0 if parsed else 0.0


def _candidate_schema_valid(candidate: dict[str, Any]) -> bool:
    schema_valid = _optional_bool(candidate.get("schema_valid"))
    if schema_valid is not None:
        return schema_valid
    validity_pct = _optional_float(candidate.get("schema_validity_pct"))
    return validity_pct is not None and validity_pct >= 100.0
