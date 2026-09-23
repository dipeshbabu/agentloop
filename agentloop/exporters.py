from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agentloop.costs import format_cost_usd
from agentloop.markdown import (
    markdown_code_span,
    markdown_heading,
    markdown_table_cell,
    markdown_text,
)


def export_report_json(report: dict[str, Any], path: str | Path) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return out


def export_report_markdown(report: dict[str, Any], path: str | Path) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"# AgentLoop Report: {markdown_heading(report['name'])}",
        "",
        f"- Run ID: {markdown_code_span(report['run_id'])}",
        f"- Runtime: {report['total_runtime_ms'] / 1000:.2f}s",
        "- Estimated cost: "
        + format_cost_usd(report.get("estimated_cost_usd"), report.get("cost_status", "complete")),
        f"- Model calls: {report['model_call_count']}",
        f"- Tool calls: {report['tool_call_count']}",
        f"- Retries: {report['retry_count']}",
        f"- Input tokens: {report['input_tokens']}",
        f"- Output tokens: {report['output_tokens']}",
        f"- Repeated context: {report['repeated_context_ratio']:.1%}",
        "",
        "## Recommendations",
    ]
    for rec in report["recommendations"]:
        lines.append(f"- **{markdown_text(rec['title'])}** — {markdown_text(rec['description'])}")
    if "execution" in report:
        lines.extend(
            [
                "",
                "## Workflow",
                "",
                markdown_code_span(json.dumps(report["execution"], sort_keys=True)),
                "",
                "Cost and token totals cover recorded model calls, not arbitrary service billing.",
            ]
        )
    lines.extend(
        [
            "",
            "## Events",
            "",
            "| Type | Name | Duration ms | Model | Input | Output | Status |",
            "|---|---|---:|---|---:|---:|---|",
        ]
    )
    for event in report["events"]:
        lines.append(
            f"| {markdown_table_cell(event['event_type'])} | "
            f"{markdown_table_cell(event['name'])} | {event['duration_ms']:.2f} | "
            f"{markdown_table_cell(event.get('model') or '')} | "
            f"{event.get('input_tokens', 0)} | {event.get('output_tokens', 0)} | "
            f"{markdown_table_cell(event.get('status', 'ok'))} |"
        )
    if report.get("stages"):
        lines.extend(
            [
                "",
                "## Stages",
                "",
                "| Span | Stage / version | Kind | Outcome | Schema references | Data references | Dependencies |",
                "| --- | --- | --- | --- | --- | --- | --- |",
            ]
        )
        for identity, stage in report["stages"].items():
            cells = [
                identity,
                f"{stage.get('stage_id')} / {stage.get('version')}",
                stage.get("kind"),
                stage.get("outcome"),
                f"{stage.get('input_schema_ref')} -> {stage.get('output_schema_ref')}",
                f"{stage.get('input_ref')} -> {stage.get('output_ref')}",
                ", ".join(stage.get("depends_on", [])),
            ]
            lines.append(
                "| "
                + " | ".join(
                    markdown_table_cell("unavailable" if value is None else str(value))
                    for value in cells
                )
                + " |"
            )
    if "semantic_judgments" in report:
        from agentloop.judgment_views import judgment_markdown

        lines.extend(judgment_markdown(report["semantic_judgments"]))
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out
