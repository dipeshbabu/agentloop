from __future__ import annotations

from typing import Any


def build_issue_drafts(queue: list[dict[str, Any]], *, limit: int = 5) -> list[dict[str, Any]]:
    drafts = []
    for item in queue:
        if item.get("patchable_count", 0) <= 0:
            continue
        drafts.append(_issue_for_queue_item(item))
        if len(drafts) >= limit:
            break
    return drafts


def issue_drafts_to_markdown(drafts: list[dict[str, Any]]) -> str:
    lines = ["# AgentLoop GitHub Issue Drafts", ""]
    if not drafts:
        lines.append("No patchable optimization queue items found.")
        return "\n".join(lines).rstrip() + "\n"

    for draft in drafts:
        lines.extend(
            [
                f"## {draft['title']}",
                "",
                f"- Labels: {', '.join(draft['labels'])}",
                "",
                draft["body"],
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def _issue_for_queue_item(item: dict[str, Any]) -> dict[str, Any]:
    title = f"Optimize agent workflow: {item['title']}"
    labels = ["agentloop", "agent-performance", f"agentloop:{item['type']}"]
    body = "\n".join(
        [
            "AgentLoop detected a recurring patchable optimization opportunity.",
            "",
            "## Finding",
            "",
            f"- Type: `{item['type']}`",
            f"- Severity: `{item['severity']}`",
            f"- Occurrences: {item['occurrence_count']}",
            f"- Affected runs: {item['run_count']}",
            f"- Patchable findings: {item['patchable_count']}",
            f"- Quality risk: `{item.get('quality_risk', 'unknown')}`",
            f"- Requires scorer: {item.get('requires_scorer', True)}",
            f"- Safe to auto-patch: {item.get('safe_to_auto_patch', False)}",
            f"- Estimated latency savings: {item['estimated_latency_savings_ms'] / 1000:.2f}s",
            f"- Estimated cost savings: ${item['estimated_cost_savings_usd']:.4f}",
            f"- Priority score: {item['priority_score']:.1f}",
            "",
            "## Affected Runs",
            "",
            "\n".join(f"- `{run_id}`" for run_id in item.get("affected_runs", [])[:20]),
            "",
            "## Acceptance Criteria",
            "",
            "- Generate or inspect the AgentLoop patch plan for one affected run.",
            "- Apply a constrained workflow rewrite.",
            "- Run `agentloop replay` against baseline and candidate traces.",
            "- Attach a quality fixture or scorer when quality risk is medium or high.",
            "- Run `agentloop ci` and confirm cost, latency, retry, schema, and quality gates pass.",
        ]
    )
    return {
        "title": title,
        "body": body,
        "labels": labels,
        "queue_id": item["queue_id"],
        "type": item["type"],
        "severity": item["severity"],
        "priority_score": item["priority_score"],
    }
