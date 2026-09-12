from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, fields
from enum import Enum
from typing import Any

from agentloop.costs import is_cost_evaluable
from agentloop.graph import ExecutionGraph
from agentloop.parallelism import PARALLELISM_REWRITE


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
class FindingCandidate:
    type: RecommendationType
    title: str
    why: str
    rewrite_hint: str
    confidence: str
    estimated_latency_savings_ms: float = 0.0
    estimated_cost_savings_usd: float | None = 0.0
    affected_nodes: list[str] | None = None
    evidence_level: str | None = None
    assumptions: list[str] | None = None
    observations: dict[str, Any] | None = None
    estimate_formula: str | None = None

    rule_id: str | None = None
    rule_version: str | None = None

    def to_dict(self) -> dict[str, Any]:
        result = {
            "type": self.type.value,
            "title": self.title,
            "why": self.why,
            "rewrite_hint": self.rewrite_hint,
            "confidence": self.confidence,
            "estimated_latency_savings_ms": round(self.estimated_latency_savings_ms, 3),
            "estimated_cost_savings_usd": (
                None
                if self.estimated_cost_savings_usd is None
                else round(self.estimated_cost_savings_usd, 6)
            ),
            "affected_nodes": self.affected_nodes or [],
        }
        if self.evidence_level is not None:
            result.update(
                evidence_level=self.evidence_level,
                assumptions=self.assumptions or [],
                observations=self.observations or {},
                estimate_formula=self.estimate_formula,
            )
        if self.rule_id is not None:
            result.update(rule_id=self.rule_id, rule_version=self.rule_version)
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FindingCandidate:
        values = {item.name: data[item.name] for item in fields(cls) if item.name in data}
        values["type"] = RecommendationType(values["type"])
        return cls(**values)


@dataclass(frozen=True)
class AnalysisContext:
    report: dict[str, Any]
    graph: ExecutionGraph


@dataclass(frozen=True)
class FindingRule:
    rule_id: str
    version: str
    detect: Callable[[AnalysisContext], list[FindingCandidate]]


def run_rules(
    context: AnalysisContext, rules: tuple[FindingRule, ...] | None = None
) -> tuple[list[FindingCandidate], list[dict[str, str]]]:
    """Run canonical local rules in declared order, retaining failure diagnostics."""
    candidates: list[FindingCandidate] = []
    errors = []
    for rule in BUILTIN_RULES if rules is None else rules:
        try:
            detected = list(rule.detect(context))
            if any(not isinstance(item, FindingCandidate) for item in detected):
                raise TypeError("rule must return FindingCandidate objects")
            for item in detected:
                item.rule_id = rule.rule_id
                item.rule_version = rule.version
            candidates.extend(detected)
        except Exception as exc:
            errors.append(
                {
                    "rule_id": rule.rule_id,
                    "rule_version": rule.version,
                    "error_type": type(exc).__name__,
                    "message": str(exc)[:500],
                }
            )
    if not is_cost_evaluable(context.report.get("cost_status", "complete")):
        for candidate in candidates:
            candidate.estimated_cost_savings_usd = None
    return candidates, errors


def _parallelization_cards(graph: ExecutionGraph) -> list[FindingCandidate]:
    cards = []
    for group in graph.parallelizable_groups():
        cards.append(
            FindingCandidate(
                type=RecommendationType.PARALLELIZE_TOOLS,
                title=f"Parallelize repeated `{group['name']}` tool calls",
                why=f"{group['count']} repeated calls. {group['description']}",
                rewrite_hint=PARALLELISM_REWRITE,
                confidence=group["confidence"],
                estimated_latency_savings_ms=group["estimated_savings_ms"],
                affected_nodes=group["node_ids"],
                evidence_level=group["evidence_level"],
                assumptions=group["assumptions"],
                observations=group["observations"],
                estimate_formula=group["estimate_formula"],
            )
        )
    return cards


def _context_cache_cards(report: dict[str, Any], graph: ExecutionGraph) -> list[FindingCandidate]:
    ratio = report.get("repeated_context_ratio", 0.0)
    if ratio < 0.10:
        return []
    model_nodes = [node.node_id for node in graph.nodes if node.event_type == "model_call"]
    current_cost = report.get("estimated_cost_usd", 0.0)
    cost_evaluable = is_cost_evaluable(report.get("cost_status", "complete"))
    return [
        FindingCandidate(
            type=RecommendationType.CACHE_CONTEXT,
            title="Cache repeated prompt/context prefix",
            why=f"Repeated context ratio is {ratio:.1%}, which suggests stable instructions or source text are being resent.",
            rewrite_hint="Move stable instructions into cached prefixes, summaries, or framework-level memory instead of resending full text.",
            confidence="high" if ratio >= 0.20 else "medium",
            estimated_cost_savings_usd=(current_cost * min(0.5, ratio) if cost_evaluable else None),
            affected_nodes=model_nodes,
        )
    ]


def _retry_cards(graph: ExecutionGraph) -> list[FindingCandidate]:
    retry_nodes = [node for node in graph.nodes if node.event_type == "retry"]
    if not retry_nodes:
        return []
    retry_time = sum(node.duration_ms for node in retry_nodes)
    return [
        FindingCandidate(
            type=RecommendationType.ADD_SCHEMA_VALIDATION,
            title="Reduce retry loop with structured outputs",
            why=f"Observed {len(retry_nodes)} retry span(s), costing {retry_time / 1000:.2f}s.",
            rewrite_hint="Add schema validation, JSON mode, constrained decoding, or a cheap repair prompt before rerunning a full step.",
            confidence="high",
            estimated_latency_savings_ms=retry_time * 0.8,
            affected_nodes=[node.node_id for node in retry_nodes],
        )
    ]


def _batch_model_cards(graph: ExecutionGraph) -> list[FindingCandidate]:
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
            FindingCandidate(
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


def _routing_cards(graph: ExecutionGraph) -> list[FindingCandidate]:
    cards = []
    for node in graph.nodes:
        if node.event_type != "model_call" or not node.model:
            continue
        if "mini" in node.model.lower():
            continue
        if node.output_tokens <= 400 and node.input_tokens <= 2500:
            cards.append(
                FindingCandidate(
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


def _split_large_step_cards(graph: ExecutionGraph) -> list[FindingCandidate]:
    cards = []
    for node in graph.nodes:
        if node.event_type == "model_call" and node.total_tokens >= 4000:
            cards.append(
                FindingCandidate(
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


def _runaway_loop_cards(graph: ExecutionGraph) -> list[FindingCandidate]:
    cards = []
    groups: dict[tuple[str, str], list[Any]] = {}
    for node in graph.nodes:
        groups.setdefault((node.event_type, node.name), []).append(node)

    for (event_type, name), nodes in groups.items():
        if len(nodes) < 8:
            continue
        duration = sum(node.duration_ms for node in nodes)
        cards.append(
            FindingCandidate(
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


def _tool_oscillation_cards(graph: ExecutionGraph) -> list[FindingCandidate]:
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
            FindingCandidate(
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


BUILTIN_RULES = (
    FindingRule("parallelize_tools", "1.0", lambda ctx: _parallelization_cards(ctx.graph)),
    FindingRule("cache_context", "1.0", lambda ctx: _context_cache_cards(ctx.report, ctx.graph)),
    FindingRule("add_schema_validation", "1.0", lambda ctx: _retry_cards(ctx.graph)),
    FindingRule("batch_model_calls", "1.0", lambda ctx: _batch_model_cards(ctx.graph)),
    FindingRule("route_to_smaller_model", "1.0", lambda ctx: _routing_cards(ctx.graph)),
    FindingRule("split_large_step", "1.0", lambda ctx: _split_large_step_cards(ctx.graph)),
    FindingRule("runaway_loop", "1.0", lambda ctx: _runaway_loop_cards(ctx.graph)),
    FindingRule("tool_oscillation", "1.0", lambda ctx: _tool_oscillation_cards(ctx.graph)),
)
