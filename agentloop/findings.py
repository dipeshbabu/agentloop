from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from agentloop.costs import format_cost_usd
from agentloop.markdown import markdown_code_span, markdown_heading, markdown_text
from agentloop.optimizer import build_optimization_plan


@dataclass
class OptimizationFinding:
    finding_id: str
    severity: str
    type: str
    title: str
    confidence: str
    affected_spans: list[str]
    evidence: list[dict[str, Any]]
    savings: dict[str, Any]
    rewrite: dict[str, Any]
    validation: dict[str, Any]
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "finding_id": self.finding_id,
            "severity": self.severity,
            "type": self.type,
            "title": self.title,
            "confidence": self.confidence,
            "affected_spans": self.affected_spans,
            "evidence": self.evidence,
            "savings": self.savings,
            "rewrite": self.rewrite,
            "validation": self.validation,
            "metadata": self.metadata,
        }


def build_diagnosis(trace: Any) -> dict[str, Any]:
    plan = build_optimization_plan(trace)
    findings = [_finding_from_card(card, plan) for card in plan.get("optimization_cards", [])]
    findings = [finding for finding in findings if finding is not None]
    return {
        "run_id": plan["run_id"],
        "name": plan["name"],
        "cost_status": plan.get("cost_status", "complete"),
        "current": plan["current"],
        "estimated_after": plan["estimated_after"],
        "summary": _summary(plan, findings),
        "findings": [finding.to_dict() for finding in findings],
        "graph": plan["graph"],
    }


def diagnosis_to_markdown(diagnosis: dict[str, Any]) -> str:
    current = diagnosis["current"]
    after = diagnosis["estimated_after"]
    lines = [
        f"# AgentLoop Diagnosis: {markdown_heading(diagnosis['name'])}",
        "",
        f"- Run ID: {markdown_code_span(diagnosis['run_id'])}",
        f"- Current runtime: {current['runtime_ms'] / 1000:.2f}s",
        f"- Estimated runtime after fixes: {after['runtime_ms'] / 1000:.2f}s",
        f"- Estimated latency reduction: {after['latency_reduction_pct']:.2f}%",
        "- Current cost: "
        + format_cost_usd(current.get("estimated_cost_usd"), current.get("cost_status")),
        "- Estimated cost after fixes: "
        + format_cost_usd(after.get("estimated_cost_usd"), after.get("cost_status")),
        "- Estimated cost reduction: "
        + (
            "unavailable"
            if after.get("cost_reduction_pct") is None
            else f"{after['cost_reduction_pct']:.2f}%"
        ),
        "",
        "## Findings",
        "",
    ]
    findings = diagnosis.get("findings", [])
    if not findings:
        lines.append("No machine-actionable optimization findings detected.")
    for finding in findings:
        savings = finding["savings"]
        rewrite = finding["rewrite"]
        validation = finding["validation"]
        lines.extend(
            [
                f"### {markdown_heading(finding['finding_id'])}: "
                f"{markdown_heading(finding['title'])}",
                "",
                f"- Severity: {markdown_text(finding['severity'])}",
                f"- Type: {markdown_code_span(finding['type'])}",
                f"- Confidence: {markdown_text(finding['confidence'])}",
                "- Affected spans: "
                f"{markdown_text(', '.join(map(str, finding['affected_spans'])) or 'none')}",
                f"- Estimated latency savings: {savings['estimated_latency_savings_ms'] / 1000:.2f}s",
                "- Estimated cost savings: "
                + format_cost_usd(savings.get("estimated_cost_savings_usd")),
                f"- Rewrite: {markdown_text(rewrite['hint'])}",
                f"- Validation: {markdown_text(validation['acceptance_criteria'])}",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def _stable_finding_id(finding_type: str, title: str, affected_spans: list[str]) -> str:
    """Return an order-independent identity for one concrete detected finding.

    The old IDs used a card enumeration index, so inserting or reordering an
    unrelated card could make a human-reviewed status attach to different
    evidence. The fingerprint is based on the finding kind, human-readable
    title, and sorted affected span identities instead. A changed finding gets a
    new ID; an unchanged finding keeps its ID regardless of card order.
    """
    identity = {
        "type": finding_type,
        "title": title,
        "affected_spans": sorted(str(span) for span in affected_spans),
    }
    raw = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()[:12]
    return f"al_{finding_type}_{digest}"


def _finding_from_card(card: dict[str, Any], plan: dict[str, Any]) -> OptimizationFinding | None:
    affected_spans = list(card.get("affected_nodes", []))
    finding_type = str(card.get("type", "optimization"))
    title = str(card.get("title", "Optimization opportunity"))
    latency_savings = float(card.get("estimated_latency_savings_ms", 0.0) or 0.0)
    raw_cost_savings = card.get("estimated_cost_savings_usd")
    cost_savings = None if raw_cost_savings is None else float(raw_cost_savings)
    severity = _severity(latency_savings, cost_savings, str(card.get("confidence", "low")), plan)
    node_lookup = {node["node_id"]: node for node in plan.get("graph", {}).get("nodes", [])}
    evidence = [
        _evidence_row(node_lookup[node_id]) for node_id in affected_spans if node_id in node_lookup
    ]

    return OptimizationFinding(
        finding_id=_stable_finding_id(finding_type, title, affected_spans),
        severity=severity,
        type=finding_type,
        title=title,
        confidence=str(card.get("confidence", "unknown")),
        affected_spans=affected_spans,
        evidence=evidence,
        savings={
            "estimated_latency_savings_ms": round(latency_savings, 3),
            "estimated_cost_savings_usd": (
                None if cost_savings is None else round(cost_savings, 6)
            ),
            "formula": _savings_formula(finding_type),
        },
        rewrite={
            "kind": finding_type,
            "hint": str(card.get("rewrite_hint", "")),
            "patchable": finding_type
            in {
                "parallelize_tools",
                "cache_context",
                "add_schema_validation",
                "batch_model_calls",
                "route_to_smaller_model",
                "split_large_step",
                "runaway_loop",
                "tool_oscillation",
            },
        },
        validation={
            "command": "agentloop replay",
            "acceptance_criteria": _acceptance_criteria(finding_type),
        },
        metadata={
            "why": card.get("why", ""),
            "cost_status": plan.get("cost_status"),
            "identity": "content-v1",
        },
    )


def _summary(plan: dict[str, Any], findings: list[OptimizationFinding]) -> dict[str, Any]:
    return {
        "finding_count": len(findings),
        "high_severity_count": sum(1 for finding in findings if finding.severity == "high"),
        "patchable_count": sum(1 for finding in findings if finding.rewrite.get("patchable")),
        "estimated_latency_reduction_pct": plan["estimated_after"]["latency_reduction_pct"],
        "estimated_cost_reduction_pct": plan["estimated_after"]["cost_reduction_pct"],
    }


def _severity(
    latency_savings: float,
    cost_savings: float | None,
    confidence: str,
    plan: dict[str, Any],
) -> str:
    runtime = float(plan.get("current", {}).get("runtime_ms", 0.0) or 0.0)
    cost = float(plan.get("current", {}).get("estimated_cost_usd", 0.0) or 0.0)
    latency_share = latency_savings / runtime if runtime else 0.0
    cost_share = cost_savings / cost if cost and cost_savings is not None else 0.0
    if confidence == "high" or latency_share >= 0.25 or cost_share >= 0.20:
        return "high"
    if confidence == "medium" or latency_share >= 0.10 or cost_share >= 0.10:
        return "medium"
    return "low"


def _evidence_row(node: dict[str, Any]) -> dict[str, Any]:
    return {
        "span_id": node["node_id"],
        "name": node["name"],
        "event_type": node["event_type"],
        "duration_ms": node["duration_ms"],
        "input_tokens": node.get("input_tokens", 0),
        "output_tokens": node.get("output_tokens", 0),
        "model": node.get("model"),
        "status": node.get("status", "ok"),
    }


def _savings_formula(finding_type: str) -> str:
    formulas = {
        "parallelize_tools": "sum(serial tool durations) - max(tool duration)",
        "cache_context": "current cost * repeated context ratio, capped at 50%",
        "add_schema_validation": "retry duration * 80%",
        "batch_model_calls": "sum(repeated model durations) * 35%",
        "route_to_smaller_model": "model step duration * 25%",
        "split_large_step": "large step duration * 20%",
        "runaway_loop": "repeated loop duration * 30%",
        "tool_oscillation": "alternating tool duration * 40%",
    }
    return formulas.get(finding_type, "optimizer estimate")


def _acceptance_criteria(finding_type: str) -> str:
    criteria = {
        "parallelize_tools": "runtime decreases while tool outputs remain equivalent",
        "cache_context": "input tokens and cost decrease without output quality regression",
        "add_schema_validation": "retry count decreases and output schema validity stays at 100%",
        "batch_model_calls": "runtime decreases and item-level outputs remain equivalent",
        "route_to_smaller_model": "cost decreases while configured quality scorer passes",
        "split_large_step": "context size decreases while final answer quality scorer passes",
        "runaway_loop": "iteration count is bounded and replay passes quality and budget gates",
        "tool_oscillation": "tool-call count decreases without losing required state transitions",
    }
    return criteria.get(
        finding_type, "before/after replay passes configured quality and budget gates"
    )
