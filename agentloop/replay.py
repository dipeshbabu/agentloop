from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ReplayGates:
    max_cost_regression_pct: float = 0.0
    max_latency_regression_pct: float = 0.0
    min_latency_improvement_pct: float = 0.0
    min_cost_improvement_pct: float = 0.0
    require_retry_non_increase: bool = True


def build_replay_report(
    baseline_trace: Any,
    candidate_trace: Any,
    *,
    gates: ReplayGates | None = None,
) -> dict[str, Any]:
    gates = gates or ReplayGates()
    baseline = baseline_trace.report()
    candidate = candidate_trace.report()
    deltas = _deltas(baseline, candidate)
    gate_results = _gate_results(deltas, baseline, candidate, gates)
    passed = all(item["passed"] for item in gate_results)

    return {
        "baseline": _trace_summary(baseline_trace, baseline),
        "candidate": _trace_summary(candidate_trace, candidate),
        "deltas": deltas,
        "gates": {
            "passed": passed,
            "config": {
                "max_cost_regression_pct": gates.max_cost_regression_pct,
                "max_latency_regression_pct": gates.max_latency_regression_pct,
                "min_latency_improvement_pct": gates.min_latency_improvement_pct,
                "min_cost_improvement_pct": gates.min_cost_improvement_pct,
                "require_retry_non_increase": gates.require_retry_non_increase,
            },
            "results": gate_results,
        },
        "summary": _summary(deltas, passed),
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
        f"- Baseline: `{baseline['name']}` `{baseline['run_id']}`",
        f"- Candidate: `{candidate['name']}` `{candidate['run_id']}`",
        "",
        "## Metric Deltas",
        "",
        "| Metric | Baseline | Candidate | Delta | Improvement |",
        "|---|---:|---:|---:|---:|",
        _metric_row("Runtime", baseline["runtime_ms"], candidate["runtime_ms"], deltas["runtime_ms_delta"], deltas["latency_improvement_pct"], "ms"),
        _metric_row("Cost", baseline["estimated_cost_usd"], candidate["estimated_cost_usd"], deltas["cost_usd_delta"], deltas["cost_improvement_pct"], "usd"),
        _metric_row("Input tokens", baseline["input_tokens"], candidate["input_tokens"], deltas["input_tokens_delta"], deltas["input_token_improvement_pct"], "count"),
        _metric_row("Output tokens", baseline["output_tokens"], candidate["output_tokens"], deltas["output_tokens_delta"], deltas["output_token_improvement_pct"], "count"),
        _metric_row("Retries", baseline["retry_count"], candidate["retry_count"], deltas["retry_count_delta"], deltas["retry_improvement_pct"], "count"),
        _metric_row("Tool calls", baseline["tool_call_count"], candidate["tool_call_count"], deltas["tool_call_count_delta"], deltas["tool_call_improvement_pct"], "count"),
        _metric_row("Model calls", baseline["model_call_count"], candidate["model_call_count"], deltas["model_call_count_delta"], deltas["model_call_improvement_pct"], "count"),
        "",
        "## Gates",
        "",
        "| Gate | Status | Detail |",
        "|---|---|---|",
    ]
    for item in gates["results"]:
        gate_status = "pass" if item["passed"] else "fail"
        lines.append(f"| {item['name']} | {gate_status} | {item['detail']} |")
    return "\n".join(lines).rstrip() + "\n"


def _trace_summary(trace: Any, report: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": trace.run_id,
        "name": trace.name,
        "runtime_ms": float(report.get("total_runtime_ms", 0.0) or 0.0),
        "estimated_cost_usd": float(report.get("estimated_cost_usd", 0.0) or 0.0),
        "input_tokens": int(report.get("input_tokens", 0) or 0),
        "output_tokens": int(report.get("output_tokens", 0) or 0),
        "retry_count": int(report.get("retry_count", 0) or 0),
        "tool_call_count": int(report.get("tool_call_count", 0) or 0),
        "model_call_count": int(report.get("model_call_count", 0) or 0),
    }


def _deltas(baseline: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    base_runtime = float(baseline.get("total_runtime_ms", 0.0) or 0.0)
    cand_runtime = float(candidate.get("total_runtime_ms", 0.0) or 0.0)
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
    }


def _gate_results(
    deltas: dict[str, Any],
    baseline: dict[str, Any],
    candidate: dict[str, Any],
    gates: ReplayGates,
) -> list[dict[str, Any]]:
    results = [
        _gate(
            "latency_regression",
            deltas["latency_regression_pct"] <= gates.max_latency_regression_pct,
            f"{deltas['latency_regression_pct']:.2f}% regression <= {gates.max_latency_regression_pct:.2f}% allowed",
        ),
        _gate(
            "cost_regression",
            deltas["cost_regression_pct"] <= gates.max_cost_regression_pct,
            f"{deltas['cost_regression_pct']:.2f}% regression <= {gates.max_cost_regression_pct:.2f}% allowed",
        ),
        _gate(
            "latency_improvement",
            deltas["latency_improvement_pct"] >= gates.min_latency_improvement_pct,
            f"{deltas['latency_improvement_pct']:.2f}% improvement >= {gates.min_latency_improvement_pct:.2f}% required",
        ),
        _gate(
            "cost_improvement",
            deltas["cost_improvement_pct"] >= gates.min_cost_improvement_pct,
            f"{deltas['cost_improvement_pct']:.2f}% improvement >= {gates.min_cost_improvement_pct:.2f}% required",
        ),
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
    return results


def _gate(name: str, passed: bool, detail: str) -> dict[str, Any]:
    return {"name": name, "passed": passed, "detail": detail}


def _summary(deltas: dict[str, Any], passed: bool) -> str:
    status = "passed" if passed else "failed"
    return (
        f"Replay {status}: latency improvement {deltas['latency_improvement_pct']:.2f}%, "
        f"cost improvement {deltas['cost_improvement_pct']:.2f}%, "
        f"retry delta {deltas['retry_count_delta']}."
    )


def _improvement_pct(baseline: float, candidate: float) -> float:
    if baseline == 0:
        return 0.0 if candidate == 0 else -100.0
    return round(((baseline - candidate) / baseline) * 100, 2)


def _regression_pct(baseline: float, candidate: float) -> float:
    if baseline == 0:
        return 0.0 if candidate == 0 else 100.0
    return round(max(0.0, ((candidate - baseline) / baseline) * 100), 2)


def _metric_row(metric: str, baseline: float, candidate: float, delta: float, improvement: float, kind: str) -> str:
    return (
        f"| {metric} | {_format_metric(baseline, kind)} | {_format_metric(candidate, kind)} | "
        f"{_format_metric(delta, kind)} | {improvement:.2f}% |"
    )


def _format_metric(value: float, kind: str) -> str:
    if kind == "usd":
        return f"${value:.6f}"
    if kind == "ms":
        return f"{value:.3f}ms"
    return str(int(value))
