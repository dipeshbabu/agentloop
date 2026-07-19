from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from agentloop.graph import ExecutionGraph
from agentloop.savings import SavingsItem, select_compatible


class RecommendationType(str, Enum):
    PARALLELIZE_TOOLS = "parallelize_tools"
    CACHE_CONTEXT = "cache_context"
    BATCH_MODEL_CALLS = "batch_model_calls"
    ROUTE_TO_SMALLER_MODEL = "route_to_smaller_model"
    REMOVE_RETRY_LOOP = "remove_retry_loop"
    ADD_SCHEMA_VALIDATION = "add_schema_validation"
    SPLIT_LARGE_STEP = "split_large_step"
    RUNAWAY_LOOP = "runaway_loop"
    TOOL_OSCILLATION = "tool_oscillation"


@dataclass
class OptimizationCard:
    type: RecommendationType
    title: str
    why: str
    rewrite_hint: str
    confidence: str
    estimated_latency_savings_ms: float = 0.0
    estimated_cost_savings_usd: float = 0.0
    affected_nodes: list[str] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type.value,
            "title": self.title,
            "why": self.why,
            "rewrite_hint": self.rewrite_hint,
            "confidence": self.confidence,
            "estimated_latency_savings_ms": round(self.estimated_latency_savings_ms, 3),
            "estimated_cost_savings_usd": round(self.estimated_cost_savings_usd, 6),
            "affected_nodes": self.affected_nodes or [],
        }


def build_optimization_plan(trace: Any, report: dict[str, Any] | None = None) -> dict[str, Any]:
    report = report or trace.report()
    graph = ExecutionGraph.from_trace(trace)
    cards: list[OptimizationCard] = []
    cards.extend(_parallelization_cards(graph))
    cards.extend(_context_cache_cards(report, graph))
    cards.extend(_retry_cards(graph))
    cards.extend(_batch_model_cards(graph))
    cards.extend(_routing_cards(graph))
    cards.extend(_split_large_step_cards(graph))
    cards.extend(_runaway_loop_cards(graph))
    cards.extend(_tool_oscillation_cards(graph))

    current_runtime = report.get("total_runtime_ms", graph.total_runtime_ms())
    current_cost = report.get("estimated_cost_usd", 0.0)
    # `estimated_cost_usd` is only a complete measurement when cost_status is
    # "complete"/"empty"; otherwise it's a lower bound (some model calls were
    # unpriced). Surface that so a consumer doesn't read the cost/savings numbers
    # below as exact for a trace that used unpriced models.
    cost_status = report.get("cost_status", "complete")
    aggregate = _aggregate_savings(cards, current_runtime, current_cost)
    total_latency_savings = aggregate["latency_savings_ms"]
    total_cost_savings = aggregate["cost_savings_usd"]

    return {
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
            "estimated_cost_usd": round(max(0.0, current_cost - total_cost_savings), 6),
            "cost_status": cost_status,
            "latency_reduction_pct": round((total_latency_savings / current_runtime) * 100, 2)
            if current_runtime
            else 0.0,
            "cost_reduction_pct": round((total_cost_savings / current_cost) * 100, 2)
            if current_cost
            else 0.0,
        },
        "savings_aggregation": aggregate["explanation"],
        "graph": graph.to_dict(),
        "optimization_cards": [card.to_dict() for card in cards],
    }


def _aggregate_savings(
    cards: list[OptimizationCard], current_runtime: float, current_cost: float
) -> dict[str, Any]:
    """Combine per-card savings without double-counting overlapping spans.

    Cards that share any affected span compete for the same work, so their
    estimates are mutually exclusive alternatives, not additive. The plan
    totals come from the compatible (span-disjoint) subset of cards that
    maximizes latency savings, breaking ties by cost savings — so the reported
    latency and cost pair is achievable by one concrete set of changes, never a
    mix of incompatible alternatives. The totals are finally capped at the
    current runtime/cost, because no plan can save more wall-clock time (or
    money) than the run actually spent. This keeps ``latency_reduction_pct`` at
    or below 100% and internally consistent with ``runtime_ms``, while per-card
    estimates stay untouched and explainable.
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
    capped_cost = min(selection.cost_usd, current_cost) if current_cost else 0.0
    raw_latency = sum(card.estimated_latency_savings_ms for card in cards)
    raw_cost = sum(card.estimated_cost_savings_usd for card in cards)
    return {
        "latency_savings_ms": capped_latency,
        "cost_savings_usd": capped_cost,
        "explanation": {
            "rule": (
                "cards sharing affected spans are mutually exclusive alternatives; "
                "totals come from the compatible (span-disjoint) subset of cards "
                "that maximizes latency savings, breaking ties by cost savings, "
                "capped at current runtime/cost"
            ),
            "card_count": len(cards),
            "selected_card_indexes": list(selection.indices),
            "raw_latency_savings_ms": round(raw_latency, 3),
            "effective_latency_savings_ms": round(capped_latency, 3),
            "raw_cost_savings_usd": round(raw_cost, 6),
            "effective_cost_savings_usd": round(capped_cost, 6),
        },
    }


def _parallelization_cards(graph: ExecutionGraph) -> list[OptimizationCard]:
    cards = []
    for group in graph.parallelizable_groups():
        cards.append(
            OptimizationCard(
                type=RecommendationType.PARALLELIZE_TOOLS,
                title=f"Parallelize repeated `{group['name']}` tool calls",
                why=f"{group['count']} `{group['name']}` calls appear serial and independent.",
                rewrite_hint="Use asyncio.gather or ThreadPoolExecutor around independent tool calls.",
                confidence="medium",
                estimated_latency_savings_ms=group["estimated_savings_ms"],
                affected_nodes=group["node_ids"],
            )
        )
    return cards


def _context_cache_cards(report: dict[str, Any], graph: ExecutionGraph) -> list[OptimizationCard]:
    ratio = report.get("repeated_context_ratio", 0.0)
    if ratio < 0.10:
        return []
    model_nodes = [node.node_id for node in graph.nodes if node.event_type == "model_call"]
    current_cost = report.get("estimated_cost_usd", 0.0)
    return [
        OptimizationCard(
            type=RecommendationType.CACHE_CONTEXT,
            title="Cache repeated prompt/context prefix",
            why=f"Repeated context ratio is {ratio:.1%}, which suggests stable instructions or source text are being resent.",
            rewrite_hint="Move stable instructions into cached prefixes, summaries, or framework-level memory instead of resending full text.",
            confidence="high" if ratio >= 0.20 else "medium",
            estimated_cost_savings_usd=current_cost * min(0.5, ratio),
            affected_nodes=model_nodes,
        )
    ]


def _retry_cards(graph: ExecutionGraph) -> list[OptimizationCard]:
    retry_nodes = [node for node in graph.nodes if node.event_type == "retry"]
    if not retry_nodes:
        return []
    retry_time = sum(node.duration_ms for node in retry_nodes)
    return [
        OptimizationCard(
            type=RecommendationType.ADD_SCHEMA_VALIDATION,
            title="Reduce retry loop with structured outputs",
            why=f"Observed {len(retry_nodes)} retry span(s), costing {retry_time / 1000:.2f}s.",
            rewrite_hint="Add schema validation, JSON mode, constrained decoding, or a cheap repair prompt before rerunning a full step.",
            confidence="high",
            estimated_latency_savings_ms=retry_time * 0.8,
            affected_nodes=[node.node_id for node in retry_nodes],
        )
    ]


def _batch_model_cards(graph: ExecutionGraph) -> list[OptimizationCard]:
    groups: dict[str, list[Any]] = {}
    for node in graph.nodes:
        if node.event_type == "model_call":
            groups.setdefault(node.name, []).append(node)
    cards = []
    for name, nodes in groups.items():
        if len(nodes) < 3:
            continue
        duration = sum(node.duration_ms for node in nodes)
        cards.append(
            OptimizationCard(
                type=RecommendationType.BATCH_MODEL_CALLS,
                title=f"Batch repeated `{name}` model calls",
                why=f"{len(nodes)} `{name}` calls have the same role and may be batchable.",
                rewrite_hint="Batch documents/items into one prompt or use map-reduce only when outputs truly need independent reasoning.",
                confidence="medium",
                estimated_latency_savings_ms=duration * 0.35,
                affected_nodes=[node.node_id for node in nodes],
            )
        )
    return cards


def _routing_cards(graph: ExecutionGraph) -> list[OptimizationCard]:
    cards = []
    for node in graph.nodes:
        if node.event_type != "model_call" or not node.model:
            continue
        if "mini" in node.model.lower():
            continue
        if node.output_tokens <= 400 and node.input_tokens <= 2500:
            cards.append(
                OptimizationCard(
                    type=RecommendationType.ROUTE_TO_SMALLER_MODEL,
                    title=f"Route `{node.name}` to a cheaper model",
                    why=f"`{node.name}` is a relatively small model step using {node.total_tokens} tokens on {node.model}.",
                    rewrite_hint="Try a smaller model for planning, summarization, extraction, or verification steps and keep the larger model for final synthesis.",
                    confidence="low",
                    estimated_latency_savings_ms=node.duration_ms * 0.25,
                    affected_nodes=[node.node_id],
                )
            )
    return cards[:3]


def _split_large_step_cards(graph: ExecutionGraph) -> list[OptimizationCard]:
    cards = []
    for node in graph.nodes:
        if node.event_type == "model_call" and node.total_tokens >= 4000:
            cards.append(
                OptimizationCard(
                    type=RecommendationType.SPLIT_LARGE_STEP,
                    title=f"Split or compress `{node.name}`",
                    why=f"`{node.name}` used {node.total_tokens} tokens, making it a large and fragile step.",
                    rewrite_hint="Split into retrieve-filter-summarize or compress the context before the final reasoning call.",
                    confidence="medium",
                    estimated_latency_savings_ms=node.duration_ms * 0.20,
                    affected_nodes=[node.node_id],
                )
            )
    return cards


def _runaway_loop_cards(graph: ExecutionGraph) -> list[OptimizationCard]:
    cards = []
    groups: dict[tuple[str, str], list[Any]] = {}
    for node in graph.nodes:
        groups.setdefault((node.event_type, node.name), []).append(node)

    for (event_type, name), nodes in groups.items():
        if len(nodes) < 8:
            continue
        duration = sum(node.duration_ms for node in nodes)
        cards.append(
            OptimizationCard(
                type=RecommendationType.RUNAWAY_LOOP,
                title=f"Add a loop guardrail around `{name}`",
                why=f"`{name}` ran {len(nodes)} times in one trace, which suggests an unbounded or weakly bounded agent loop.",
                rewrite_hint="Add max-iteration, max-cost, and unchanged-state guards before the workflow can keep looping.",
                confidence="high" if len(nodes) >= 12 else "medium",
                estimated_latency_savings_ms=duration * 0.30,
                affected_nodes=[node.node_id for node in nodes],
            )
        )
    return cards[:3]


def _tool_oscillation_cards(graph: ExecutionGraph) -> list[OptimizationCard]:
    tool_nodes = [node for node in graph.nodes if node.event_type == "tool_call"]
    if len(tool_nodes) < 4:
        return []

    cards = []
    index = 0
    while index <= len(tool_nodes) - 4:
        first = tool_nodes[index].name
        second = tool_nodes[index + 1].name
        if first == second:
            index += 1
            continue

        window = tool_nodes[index : index + 4]
        if [node.name for node in window] != [first, second, first, second]:
            index += 1
            continue

        end = index + 4
        while end < len(tool_nodes) and tool_nodes[end].name == (
            first if (end - index) % 2 == 0 else second
        ):
            end += 1
        oscillating = tool_nodes[index:end]
        duration = sum(node.duration_ms for node in oscillating)
        cards.append(
            OptimizationCard(
                type=RecommendationType.TOOL_OSCILLATION,
                title=f"Stop `{first}`/`{second}` tool oscillation",
                why=f"Observed {len(oscillating)} alternating `{first}` and `{second}` tool calls.",
                rewrite_hint="Add a state-change check or decision memo so the agent does not repeat equivalent tool transitions.",
                confidence="medium",
                estimated_latency_savings_ms=duration * 0.40,
                affected_nodes=[node.node_id for node in oscillating],
            )
        )
        index = end

    return sorted(cards, key=lambda card: card.estimated_latency_savings_ms, reverse=True)[:3]
