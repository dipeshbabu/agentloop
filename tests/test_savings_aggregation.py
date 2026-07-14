from __future__ import annotations

from agentloop.events import AgentEvent
from agentloop.optimizer import (
    OptimizationCard,
    RecommendationType,
    _aggregate_savings,
    build_optimization_plan,
)
from agentloop.tracer import AgentTrace


def _card(rec: RecommendationType, latency: float, cost: float, nodes: list[str]) -> OptimizationCard:
    return OptimizationCard(
        type=rec,
        title=rec.value,
        why="",
        rewrite_hint="",
        confidence="medium",
        estimated_latency_savings_ms=latency,
        estimated_cost_savings_usd=cost,
        affected_nodes=nodes,
    )


# --- aggregation rule --------------------------------------------------------

def test_overlapping_cards_take_max_not_sum() -> None:
    # Two parallelization cards + one oscillation card, all on the same spans:
    # they are alternatives, so the aggregate is the best single estimate (300),
    # not the naive sum (300 + 300 + 200 = 800).
    cards = [
        _card(RecommendationType.PARALLELIZE_TOOLS, 300, 0.0, ["n1", "n2"]),
        _card(RecommendationType.PARALLELIZE_TOOLS, 300, 0.0, ["n2", "n3"]),
        _card(RecommendationType.TOOL_OSCILLATION, 200, 0.0, ["n1", "n3"]),
    ]
    agg = _aggregate_savings(cards, current_runtime=800.0, current_cost=0.0)
    assert agg["latency_savings_ms"] == 300.0
    assert agg["explanation"]["independent_group_count"] == 1
    assert agg["explanation"]["overlapping_group_count"] == 1


def test_disjoint_cards_are_additive() -> None:
    # Batching + routing on different spans do not overlap, so they sum.
    cards = [
        _card(RecommendationType.BATCH_MODEL_CALLS, 100, 0.02, ["m1", "m2"]),
        _card(RecommendationType.ROUTE_TO_SMALLER_MODEL, 50, 0.05, ["m3"]),
    ]
    agg = _aggregate_savings(cards, current_runtime=1000.0, current_cost=1.0)
    assert agg["latency_savings_ms"] == 150.0
    assert agg["cost_savings_usd"] == 0.07
    assert agg["explanation"]["independent_group_count"] == 2
    assert agg["explanation"]["overlapping_group_count"] == 0


def test_repeated_same_span_loop_detectors_do_not_stack() -> None:
    # Runaway-loop + oscillation flagged on the same repeated span -> alternatives.
    cards = [
        _card(RecommendationType.RUNAWAY_LOOP, 400, 0.0, ["loop"]),
        _card(RecommendationType.TOOL_OSCILLATION, 250, 0.0, ["loop"]),
    ]
    agg = _aggregate_savings(cards, current_runtime=1000.0, current_cost=0.0)
    assert agg["latency_savings_ms"] == 400.0


def test_aggregate_is_capped_at_current_runtime_and_cost() -> None:
    cards = [
        _card(RecommendationType.PARALLELIZE_TOOLS, 900, 0.4, ["a"]),
        _card(RecommendationType.BATCH_MODEL_CALLS, 900, 0.4, ["b"]),
    ]
    agg = _aggregate_savings(cards, current_runtime=800.0, current_cost=0.5)
    assert agg["latency_savings_ms"] == 800.0  # capped, not 1800
    assert agg["cost_savings_usd"] == 0.5


def test_cards_without_affected_spans_are_separate_groups() -> None:
    cards = [
        _card(RecommendationType.CACHE_CONTEXT, 100, 0.0, []),
        _card(RecommendationType.ADD_SCHEMA_VALIDATION, 50, 0.0, []),
    ]
    agg = _aggregate_savings(cards, current_runtime=1000.0, current_cost=0.0)
    assert agg["latency_savings_ms"] == 150.0
    assert agg["explanation"]["independent_group_count"] == 2


# --- end-to-end reproduction from the issue ----------------------------------

def _alternating_tool_trace() -> AgentTrace:
    """Eight alternating 100 ms tool events named 'a' and 'b' — reproduces the
    two-parallelization-plus-oscillation overlap from the issue."""
    trace = AgentTrace(name="oscillation", run_id="run_" + "a" * 16)
    t = 0
    for i in range(8):
        start = t
        end = t + 100
        trace.add_event(
            AgentEvent(
                event_id=f"evt_{i:016x}",
                run_id=trace.run_id,
                event_type="tool_call",
                name="a" if i % 2 == 0 else "b",
                started_at=_iso(start),
                ended_at=_iso(end),
                duration_ms=100.0,
            )
        )
        t = end
    return trace


def _iso(ms: int) -> str:
    seconds = ms / 1000
    return f"2026-01-01T00:00:{seconds:06.3f}+00:00"


def test_reduction_pct_never_exceeds_100() -> None:
    trace = _alternating_tool_trace()
    plan = build_optimization_plan(trace)
    after = plan["estimated_after"]

    assert 0.0 <= after["latency_reduction_pct"] <= 100.0
    assert after["runtime_ms"] >= 0.0
    # runtime_ms and the percentage must agree (be derived from one aggregate).
    current = plan["current"]["runtime_ms"]
    implied = (current - after["runtime_ms"]) / current * 100
    assert abs(implied - after["latency_reduction_pct"]) < 0.5
    # The overlap is surfaced, and the naive sum is recorded for transparency.
    agg = plan["savings_aggregation"]
    assert agg["raw_latency_savings_ms"] >= agg["effective_latency_savings_ms"]
