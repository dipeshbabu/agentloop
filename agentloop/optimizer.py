from __future__ import annotations

from typing import Any

from agentloop.costs import is_cost_evaluable
from agentloop.graph import ExecutionGraph
from agentloop.rules import AnalysisContext, run_rules
from agentloop.rules import FindingCandidate as OptimizationCard
from agentloop.rules import RecommendationType as RecommendationType
from agentloop.savings import SavingsItem, select_compatible


def build_optimization_plan(trace: Any, report: dict[str, Any] | None = None) -> dict[str, Any]:
    report = report or trace.report()
    graph = ExecutionGraph.from_trace(trace)
    if "finding_candidates" in report:
        cards = [OptimizationCard.from_dict(item) for item in report["finding_candidates"]]
        rule_errors = list(report.get("rule_errors", []))
    else:
        cards, rule_errors = run_rules(AnalysisContext(report=report, graph=graph))

    current_runtime = report.get("total_runtime_ms", graph.total_runtime_ms())
    current_cost = report.get("estimated_cost_usd", 0.0)
    # `estimated_cost_usd` is only a complete measurement when cost_status is
    # "complete"/"empty"; otherwise it's a lower bound (some model calls were
    # unpriced). Surface that so a consumer doesn't read the cost/savings numbers
    # below as exact for a trace that used unpriced models.
    cost_status = report.get("cost_status", "complete")
    cost_evaluable = is_cost_evaluable(cost_status)
    if not cost_evaluable:
        for card in cards:
            card.estimated_cost_savings_usd = None
    aggregate = _aggregate_savings(
        cards, current_runtime, current_cost, cost_evaluable=cost_evaluable
    )
    total_latency_savings = aggregate["latency_savings_ms"]
    total_cost_savings = aggregate["cost_savings_usd"]

    return {
        **(
            {
                "semantic_judgments": report["semantic_judgments"],
                "evidence_categories": report["evidence_categories"],
            }
            if "semantic_judgments" in report
            else {}
        ),
        "run_id": trace.run_id,
        "name": trace.name,
        "cost_status": cost_status,
        "current": {
            "runtime_ms": current_runtime,
            "estimated_cost_usd": current_cost,
            "cost_status": cost_status,
            "input_tokens": report.get("input_tokens", 0),
            "output_tokens": report.get("output_tokens", 0),
            "retry_count": report.get("retry_count", 0),
            "repeated_context_ratio": report.get("repeated_context_ratio", 0.0),
        },
        "estimated_after": {
            "runtime_ms": round(max(0.0, current_runtime - total_latency_savings), 3),
            "estimated_cost_usd": (
                round(max(0.0, current_cost - total_cost_savings), 6)
                if total_cost_savings is not None
                else None
            ),
            "cost_status": cost_status,
            "latency_reduction_pct": round((total_latency_savings / current_runtime) * 100, 2)
            if current_runtime
            else 0.0,
            "cost_reduction_pct": (
                round((total_cost_savings / current_cost) * 100, 2)
                if total_cost_savings is not None and current_cost
                else (0.0 if total_cost_savings == 0 else None)
            ),
        },
        "savings_aggregation": aggregate["explanation"],
        "graph": graph.to_dict(),
        "optimization_cards": [card.to_dict() for card in cards],
        "rule_errors": rule_errors,
        "analysis_complete": not rule_errors,
    }


def _aggregate_savings(
    cards: list[OptimizationCard],
    current_runtime: float,
    current_cost: float,
    *,
    cost_evaluable: bool = True,
) -> dict[str, Any]:
    """Combine per-card savings without double-counting overlapping spans.

    Cards that share any affected span compete for the same work, so their
    estimates are mutually exclusive alternatives, not additive. Small overlap
    components are solved exactly. If an unusually large component crosses the
    selector's exact limit, the same compatible subset still drives both
    latency and cost totals, but the explanation marks it as an approximation
    rather than claiming a proven maximum. Totals are capped at current runtime
    and cost because a plan cannot save more than the run spent.
    """
    items = [
        SavingsItem(
            spans=frozenset(card.affected_nodes or []),
            latency_ms=float(card.estimated_latency_savings_ms or 0.0),
            cost_usd=float(card.estimated_cost_savings_usd or 0.0),
        )
        for card in cards
    ]
    selection = select_compatible(items)
    capped_latency = min(selection.latency_ms, current_runtime) if current_runtime else 0.0
    capped_cost = (
        (min(selection.cost_usd, current_cost) if current_cost else 0.0) if cost_evaluable else None
    )
    raw_latency = sum(card.estimated_latency_savings_ms for card in cards)
    raw_cost = (
        sum(float(card.estimated_cost_savings_usd or 0.0) for card in cards)
        if cost_evaluable
        else None
    )
    if selection.optimal:
        rule = (
            "cards sharing affected spans are mutually exclusive alternatives; "
            "totals come from the proven-optimal compatible (span-disjoint) subset "
            "that maximizes latency savings, breaking ties by cost savings, capped "
            "at current runtime/cost"
        )
    else:
        rule = (
            "cards sharing affected spans are mutually exclusive alternatives; "
            "components above the exact selection limit use a deterministic greedy "
            "approximation, so totals may understate achievable savings; latency and "
            "cost still come from one compatible subset and are capped at current runtime/cost"
        )
    return {
        "latency_savings_ms": capped_latency,
        "cost_savings_usd": capped_cost,
        "explanation": {
            "rule": rule,
            "card_count": len(cards),
            "selected_card_indexes": list(selection.indices),
            "selection_optimal": selection.optimal,
            "selection_algorithm": selection.algorithm,
            "exact_component_limit": selection.exact_component_limit,
            "raw_latency_savings_ms": round(raw_latency, 3),
            "effective_latency_savings_ms": round(capped_latency, 3),
            "raw_cost_savings_usd": None if raw_cost is None else round(raw_cost, 6),
            "effective_cost_savings_usd": (None if capped_cost is None else round(capped_cost, 6)),
        },
    }
