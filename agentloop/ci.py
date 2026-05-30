from __future__ import annotations

from typing import Any

from agentloop.findings import build_diagnosis
from agentloop.replay import ReplayGates, build_replay_report


def build_ci_report(
    baseline_trace: Any,
    candidate_trace: Any,
    *,
    gates: ReplayGates | None = None,
) -> dict[str, Any]:
    """Build the PR-facing AgentLoop performance report."""
    replay = build_replay_report(baseline_trace, candidate_trace, gates=gates)
    diagnosis = build_diagnosis(candidate_trace)
    summary = _summary(replay, diagnosis)
    return {
        "passed": replay["gates"]["passed"],
        "status": "passed" if replay["gates"]["passed"] else "failed",
        "summary": summary,
        "replay": replay,
        "diagnosis": diagnosis,
    }


def ci_report_to_markdown(report: dict[str, Any]) -> str:
    summary = report["summary"]
    replay = report["replay"]
    diagnosis = report["diagnosis"]
    gate_status = "passed" if replay["gates"]["passed"] else "failed"

    lines = [
        "# AgentLoop CI Report",
        "",
        f"- Status: {gate_status}",
        f"- Replay: {replay['summary']}",
        f"- Merge recommendation: {summary['merge_recommendation']}",
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
        lines.append(f"| {_cell(gate['name'])} | {status} | {_cell(gate['detail'])} |")

    lines.extend(
        [
            "",
            "## Metric Impact",
            "",
            "| Metric | Impact |",
            "|---|---:|",
            f"| Latency improvement | {summary['latency_improvement_pct']:.2f}% |",
            f"| Cost improvement | {summary['cost_improvement_pct']:.2f}% |",
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
                f"{_cell(finding['finding_id'])}: {_cell(finding['title'])} | "
                f"{_cell(finding['severity'])} | "
                f"`{_cell(finding['type'])}` | "
                f"{_cell(finding['rewrite']['hint'])} |"
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
        "candidate_schema_validity": _format_optional(candidate.get("schema_validity_pct"), suffix="%")
        if candidate.get("schema_validity_pct") is not None
        else _format_optional(candidate.get("schema_valid")),
        "candidate_quality_score": _format_optional(candidate.get("quality_score")),
        "finding_count": diagnosis_summary["finding_count"],
        "high_severity_count": diagnosis_summary["high_severity_count"],
        "patchable_count": diagnosis_summary["patchable_count"],
        "merge_recommendation": merge_recommendation,
    }


def _cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def _format_optional(value: Any, suffix: str = "") -> str:
    if value is None:
        return "not provided"
    if isinstance(value, float):
        return f"{value:.4f}{suffix}"
    return f"{value}{suffix}"
