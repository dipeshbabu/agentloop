from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agentloop.markdown import code_span, escape_cell, escape_inline


def export_optimization_json(plan: dict[str, Any], path: str | Path) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(plan, indent=2), encoding="utf-8")
    return out


def export_optimization_markdown(plan: dict[str, Any], path: str | Path) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    current = plan["current"]
    after = plan["estimated_after"]
    lines = [
        f"# AgentLoop Optimization Plan: {escape_inline(plan['name'])}",
        "",
        f"- Run ID: {code_span(plan['run_id'])}",
        f"- Current runtime: {current['runtime_ms'] / 1000:.2f}s",
        f"- Estimated optimized runtime: {after['runtime_ms'] / 1000:.2f}s",
        f"- Estimated latency reduction: {after['latency_reduction_pct']:.2f}%",
        f"- Current cost: ${current['estimated_cost_usd']:.4f}",
        f"- Estimated optimized cost: ${after['estimated_cost_usd']:.4f}",
        f"- Estimated cost reduction: {after['cost_reduction_pct']:.2f}%",
        f"- Repeated context ratio: {current['repeated_context_ratio']:.1%}",
        f"- Retry count: {current['retry_count']}",
        "",
        "## Optimization cards",
        "",
    ]
    cards = plan.get("optimization_cards", [])
    if not cards:
        lines.append(
            "No major optimization opportunities detected yet. Collect more traces for stronger recommendations."
        )
    for index, card in enumerate(cards, start=1):
        lines.extend(
            [
                f"### {index}. {escape_inline(card['title'])}",
                "",
                f"- Type: {code_span(card['type'])}",
                f"- Confidence: {escape_inline(card['confidence'])}",
                f"- Why: {escape_inline(card['why'])}",
                f"- Rewrite hint: {escape_inline(card['rewrite_hint'])}",
                f"- Estimated latency savings: {card['estimated_latency_savings_ms'] / 1000:.2f}s",
                f"- Estimated cost savings: ${card['estimated_cost_savings_usd']:.4f}",
                "",
            ]
        )
    lines.extend(
        ["## Bottlenecks", "", "| Name | Type | Duration | Runtime share |", "|---|---|---:|---:|"]
    )
    for item in plan.get("graph", {}).get("bottlenecks", []):
        lines.append(
            f"| {escape_cell(item['name'])} | {escape_cell(item['event_type'])} | "
            f"{item['duration_ms'] / 1000:.2f}s | {item['runtime_share']:.1%} |"
        )
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out
