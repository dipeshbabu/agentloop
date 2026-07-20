from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agentloop.costs import is_cost_evaluable
from agentloop.exporters import export_report_markdown
from agentloop.tracer import AgentTrace


@dataclass
class AuditResult:
    report: dict[str, Any]
    markdown_path: Path | None = None


def run_audit(trace_path: str | Path, out_path: str | Path | None = None) -> AuditResult:
    trace = AgentTrace.from_json(trace_path)
    report = trace.report()
    md_path = export_report_markdown(report, out_path) if out_path else None
    return AuditResult(report=report, markdown_path=md_path)


def estimate_improvement(report: dict[str, Any]) -> dict[str, float | None]:
    latency_savings = 0.0
    for item in report.get("parallelism_opportunities", []):
        latency_savings += item.get("estimated_savings_ms", 0.0)
    retry_savings = report.get("retry_time_ms", 0.0) * 0.8
    repeated_ratio = report.get("repeated_context_ratio", 0.0)
    cost_savings = (
        report.get("estimated_cost_usd", 0.0) * min(0.5, repeated_ratio)
        if is_cost_evaluable(report.get("cost_status", "complete"))
        else None
    )
    return {
        "estimated_latency_savings_ms": round(latency_savings + retry_savings, 3),
        "estimated_cost_savings_usd": (None if cost_savings is None else round(cost_savings, 6)),
    }
