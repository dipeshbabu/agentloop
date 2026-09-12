from __future__ import annotations

from typing import Any

from agentloop.findings import build_diagnosis
from agentloop.markdown import markdown_table_cell, markdown_text
from agentloop.replay import ReplayGates, build_replay_report


def build_ci_report(
    baseline_trace: Any,
    candidate_trace: Any,
    *,
    gates: ReplayGates | None = None,
    quality_report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the PR-facing AgentLoop performance report."""
    replay = build_replay_report(
        baseline_trace, candidate_trace, gates=gates, quality_report=quality_report
    )
    diagnosis = build_diagnosis(candidate_trace)
    summary = _summary(replay, diagnosis)
    trace_inputs = {
        "baseline": _trace_input(baseline_trace),
        "candidate": _trace_input(candidate_trace),
    }
    synthetic = any(item["synthetic"] is True for item in trace_inputs.values())
    analysis_complete = diagnosis.get("analysis_complete", True)
    if synthetic:
        summary["merge_recommendation"] = (
            "synthetic self-test only; assess application traces before merge"
        )
    if not analysis_complete:
        summary["merge_recommendation"] = (
            "analysis incomplete; inspect failed finding rules before merge"
        )
    passed = replay["gates"]["passed"] and analysis_complete
    return {
        "passed": passed,
        "status": "passed" if passed else "failed",
        "summary": summary,
        "replay": replay,
        "diagnosis": diagnosis,
        "trace_inputs": trace_inputs,
        "evidence_scope": "includes_synthetic_data" if synthetic else "supplied_trace_comparison",
        "analysis_complete": analysis_complete,
        "rule_errors": diagnosis.get("rule_errors", []),
    }


def ci_report_to_markdown(report: dict[str, Any]) -> str:
    summary = report["summary"]
    replay = report["replay"]
    diagnosis = report["diagnosis"]
    gate_status = report.get("status", "passed" if replay["gates"]["passed"] else "failed")

    lines = [
        "# AgentLoop CI Report",
        "",
        f"- Status: {gate_status}",
        f"- Replay: {markdown_text(replay['summary'])}",
        f"- Merge recommendation: {markdown_text(summary['merge_recommendation'])}",
        (
            f"- Findings: {summary['finding_count']} total, "
            f"{summary['high_severity_count']} high severity, "
            f"{summary['patchable_count']} patchable"
        ),
        "",
        "## Performance Gates",
        "",
        "| Gate | Status | Detail |",
        "|---|---|---|",
    ]
    for gate in replay["gates"]["results"]:
        status = "pass" if gate["passed"] else "fail"
        lines.append(
            f"| {markdown_table_cell(gate['name'])} | {status} | "
            f"{markdown_table_cell(gate['detail'])} |"
        )
    if report.get("rule_errors"):
        lines.extend(["", "## Incomplete analysis", ""])
        for error in report["rule_errors"]:
            lines.append(f"- {markdown_text(error['rule_id'])}: {markdown_text(error['message'])}")

    if report.get("trace_inputs"):
        lines.extend(["", "## Trace inputs", ""])
        if report.get("evidence_scope") == "includes_synthetic_data":
            lines.append(
                "This comparison includes synthetic data. It does not establish application performance gains from a pull request."
            )
        else:
            lines.append(
                "Results apply to the supplied traces and configured gates. Trace provenance is supplied by the producer."
            )
        lines.extend(
            ["", "| Side | Run ID | Path | Synthetic marker | Source |", "|---|---|---|---|---|"]
        )
        for side, item in report["trace_inputs"].items():
            marker = (
                "yes"
                if item["synthetic"] is True
                else ("no" if item["synthetic"] is False else "not supplied")
            )
            cells = (
                side,
                item["run_id"],
                item.get("path", "in-memory trace"),
                marker,
                item["source"],
            )
            lines.append("| " + " | ".join(markdown_table_cell(value) for value in cells) + " |")

    lines.extend(
        [
            "",
            "## Metric Impact",
            "",
            "| Metric | Impact |",
            "|---|---:|",
            f"| Latency improvement | {summary['latency_improvement_pct']:.2f}% |",
            "| Cost improvement | "
            + (
                "unavailable"
                if summary["cost_improvement_pct"] is None
                else f"{summary['cost_improvement_pct']:.2f}%"
            )
            + " |",
            f"| Retry delta | {summary['retry_count_delta']} |",
            f"| Candidate schema validity | {summary['candidate_schema_validity']} |",
            f"| Candidate quality score | {summary['candidate_quality_score']} |",
            "",
            "## Top Findings",
            "",
        ]
    )

    findings = diagnosis.get("findings", [])
    if not findings:
        lines.append("No machine-actionable optimization findings detected.")
    else:
        lines.extend(
            [
                "| Finding | Severity | Type | Rewrite |",
                "|---|---|---|---|",
            ]
        )
        for finding in findings[:5]:
            lines.append(
                "| "
                f"{markdown_table_cell(finding['finding_id'])}: "
                f"{markdown_table_cell(finding['title'])} | "
                f"{markdown_table_cell(finding['severity'])} | "
                f"{markdown_table_cell(finding['type'])} | "
                f"{markdown_table_cell(finding['rewrite']['hint'])} |"
            )

    return "\n".join(lines).rstrip() + "\n"


def _summary(replay: dict[str, Any], diagnosis: dict[str, Any]) -> dict[str, Any]:
    deltas = replay["deltas"]
    diagnosis_summary = diagnosis["summary"]
    candidate = replay["candidate"]
    gates_passed = replay["gates"]["passed"]
    merge_recommendation = (
        "safe to merge by configured AgentLoop gates"
        if gates_passed and diagnosis_summary["high_severity_count"] == 0
        else "review before merge; configured gates or high-severity findings need attention"
    )
    return {
        "latency_improvement_pct": deltas["latency_improvement_pct"],
        "cost_improvement_pct": deltas["cost_improvement_pct"],
        "retry_count_delta": deltas["retry_count_delta"],
        "candidate_schema_validity": _format_optional(
            candidate.get("schema_validity_pct"), suffix="%"
        )
        if candidate.get("schema_validity_pct") is not None
        else _format_optional(candidate.get("schema_valid")),
        "candidate_quality_score": _format_optional(candidate.get("quality_score")),
        "finding_count": diagnosis_summary["finding_count"],
        "high_severity_count": diagnosis_summary["high_severity_count"],
        "patchable_count": diagnosis_summary["patchable_count"],
        "merge_recommendation": merge_recommendation,
    }


def _format_optional(value: Any, suffix: str = "") -> str:
    if value is None:
        return "not provided"
    if isinstance(value, float):
        return f"{value:.4f}{suffix}"
    return f"{value}{suffix}"


def _trace_input(trace: Any) -> dict[str, Any]:
    metadata = getattr(trace, "metadata", {}) or {}
    marker = metadata.get("synthetic")
    return {
        "run_id": str(trace.run_id),
        "synthetic": marker if isinstance(marker, bool) else None,
        "source": str(metadata.get("source") or "not supplied"),
    }
