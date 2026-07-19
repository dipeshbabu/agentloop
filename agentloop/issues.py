from __future__ import annotations

from typing import Any

from agentloop.costs import format_cost_usd
from agentloop.markdown import markdown_code_span, markdown_heading, markdown_text

# Generated drafts target this repository, so use only deliberately maintained
# labels that already exist. Finding type and severity remain structured fields
# in the draft body instead of creating an unbounded dynamic label taxonomy.
ISSUE_DRAFT_LABELS = ("enhancement",)


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
                f"## {markdown_heading(draft['title'])}",
                "",
                f"- Labels: {markdown_text(', '.join(map(str, draft['labels'])))}",
                "",
                draft["body"],
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def _issue_for_queue_item(item: dict[str, Any]) -> dict[str, Any]:
    title = f"Optimize agent workflow: {item['title']}"
    labels = list(ISSUE_DRAFT_LABELS)
    body = "\n".join(
        [
            "AgentLoop detected a recurring patchable optimization opportunity.",
            "",
            "## Finding",
            "",
            f"- Type: {markdown_code_span(item['type'])}",
            f"- Severity: {markdown_code_span(item['severity'])}",
            f"- Occurrences: {item['occurrence_count']}",
            f"- Affected runs: {item['run_count']}",
            f"- Patchable findings: {item['patchable_count']}",
            f"- Quality risk: {markdown_code_span(item.get('quality_risk', 'unknown'))}",
            f"- Requires scorer: {item.get('requires_scorer', True)}",
            f"- Safe to auto-patch: {item.get('safe_to_auto_patch', False)}",
            f"- Estimated latency savings: {item['estimated_latency_savings_ms'] / 1000:.2f}s",
            "- Estimated cost savings: " + format_cost_usd(item.get("estimated_cost_savings_usd")),
            f"- Priority score: {item['priority_score']:.1f}",
            "",
            "## Affected Runs",
            "",
            "\n".join(
                f"- {markdown_code_span(run_id)}" for run_id in item.get("affected_runs", [])[:20]
            ),
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
