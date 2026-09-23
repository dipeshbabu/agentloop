from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agentloop.costs import format_cost_usd
from agentloop.estimates import estimate_markdown
from agentloop.markdown import (
    markdown_code_span,
    markdown_heading,
    markdown_table_cell,
    markdown_text,
)
from agentloop.timing import format_duration_ms


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
        f"# AgentLoop Optimization Plan: {markdown_heading(plan['name'])}",
        "",
        f"- Run ID: {markdown_code_span(plan['run_id'])}",
        f"- Current runtime: {current['runtime_ms'] / 1000:.2f}s",
        f"- Estimated optimized runtime: {after['runtime_ms'] / 1000:.2f}s",
        f"- Estimated latency reduction: {after['latency_reduction_pct']:.2f}%",
        "- Current cost: "
        + format_cost_usd(current.get("estimated_cost_usd"), current.get("cost_status")),
        "- Estimated optimized cost: "
        + format_cost_usd(after.get("estimated_cost_usd"), after.get("cost_status")),
        "- Estimated cost reduction: "
        + (
            "unavailable"
            if after.get("cost_reduction_pct") is None
            else f"{after['cost_reduction_pct']:.2f}%"
        ),
        f"- Repeated context ratio: {current['repeated_context_ratio']:.1%}",
        f"- Retry count: {current['retry_count']}",
        "",
        "## Optimization cards",
        "",
    ]
    cards = plan.get("optimization_cards", [])
    if (
        plan.get("savings_aggregation", {}).get("latency_estimate_complete") is False
        or plan.get("savings_aggregation", {}).get("cost_estimate_complete") is False
    ):
        lines.extend(["Some savings are unavailable; totals cover modeled candidates only.", ""])
    if plan.get("rule_errors"):
        lines.extend(["Analysis incomplete; some finding rules failed:", ""])
        for error in plan["rule_errors"]:
            lines.append(
                f"- {markdown_code_span(error['rule_id'])}: {markdown_text(error['message'])}"
            )
        lines.append("")
    if not cards:
        lines.append(
            "No major optimization opportunities detected yet. Collect more traces for stronger recommendations."
        )
    for index, card in enumerate(cards, start=1):
        lines.extend(
            [
                f"### {index}. {markdown_heading(card['title'])}",
                "",
                f"- Type: {markdown_code_span(card['type'])}",
                f"- Confidence: {markdown_text(card['confidence'])}",
                f"- Why: {markdown_text(card['why'])}",
                f"- Rewrite hint: {markdown_text(card['rewrite_hint'])}",
                f"- Estimated latency savings: {format_duration_ms(card['estimated_latency_savings_ms'])}",
                "- Estimated cost savings: "
                + format_cost_usd(card.get("estimated_cost_savings_usd")),
                "",
            ]
        )
        if card.get("rule_id"):
            lines.append(
                f"- Rule: {markdown_code_span(card['rule_id'])} version {markdown_text(card['rule_version'])}"
            )
        if card.get("evidence_level") and card.get("estimate"):
            lines.append(f"- Evidence level: {markdown_text(card['evidence_level'])}")
        lines.extend(estimate_markdown(card.get("estimate")))
        if card.get("evidence_level") and not card.get("estimate"):
            lines.extend(
                [
                    f"- Evidence level: {markdown_text(card['evidence_level'])}",
                    "- Assumptions: "
                    + "; ".join(markdown_text(value) for value in card.get("assumptions", [])),
                    f"- Savings formula: {markdown_text(card.get('estimate_formula', ''))}",
                    "",
                ]
            )
    lines.extend(
        ["## Bottlenecks", "", "| Name | Type | Duration | Runtime share |", "|---|---|---:|---:|"]
    )
    for item in plan.get("graph", {}).get("bottlenecks", []):
        lines.append(
            f"| {markdown_table_cell(item['name'])} | "
            f"{markdown_table_cell(item['event_type'])} | "
            f"{item['duration_ms'] / 1000:.2f}s | {item['runtime_share']:.1%} |"
        )
    if "semantic_waste" in plan:
        from agentloop.semantic_waste import semantic_waste_markdown

        lines.extend(semantic_waste_markdown(plan["semantic_waste"]))
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out
