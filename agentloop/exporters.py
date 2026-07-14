from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agentloop.markdown import code_span, escape_cell, escape_inline


def export_report_json(report: dict[str, Any], path: str | Path) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return out


def export_report_markdown(report: dict[str, Any], path: str | Path) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"# AgentLoop Report: {escape_inline(report['name'])}",
        "",
        f"- Run ID: {code_span(report['run_id'])}",
        f"- Runtime: {report['total_runtime_ms'] / 1000:.2f}s",
        f"- Estimated cost: ${report['estimated_cost_usd']:.4f}",
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
        lines.append(f"- **{escape_inline(rec['title'])}** — {escape_inline(rec['description'])}")
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
            f"| {escape_cell(event['event_type'])} | {escape_cell(event['name'])} | "
            f"{event['duration_ms']:.2f} | {escape_cell(event.get('model') or '')} | "
            f"{event.get('input_tokens', 0)} | {event.get('output_tokens', 0)} | "
            f"{escape_cell(event.get('status', 'ok'))} |"
        )
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out
