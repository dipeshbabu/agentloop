from __future__ import annotations

from agentloop.events import AgentEvent
from agentloop.optimizer import (
    OptimizationCard,
    RecommendationType,
    _aggregate_savings,
    build_optimization_plan,
)
from agentloop.savings import SavingsItem, select_compatible
from agentloop.tracer import AgentTrace


def _card(
    rec: RecommendationType, latency: float, cost: float, nodes: list[str]
) -> OptimizationCard:
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


def test_overlapping_cards_take_best_alternative_not_sum() -> None:
    # Two parallelization cards + one oscillation card, all pairwise
    # overlapping: only one can apply, so the aggregate is the best single
    # estimate (300), not the naive sum (300 + 300 + 200 = 800).
    cards = [
        _card(RecommendationType.PARALLELIZE_TOOLS, 300, 0.0, ["n1", "n2"]),
        _card(RecommendationType.PARALLELIZE_TOOLS, 300, 0.0, ["n2", "n3"]),
        _card(RecommendationType.TOOL_OSCILLATION, 200, 0.0, ["n1", "n3"]),
    ]
    agg = _aggregate_savings(cards, current_runtime=800.0, current_cost=0.0)
    assert agg["latency_savings_ms"] == 300.0
    assert len(agg["explanation"]["selected_card_indexes"]) == 1
    assert agg["explanation"]["selection_optimal"] is True


def test_chain_overlap_selects_compatible_disjoint_cards() -> None:
    # A=[n1]/100 and C=[n2]/100 are compatible even though B=[n1, n2]/150
    # overlaps both: the best realizable plan applies A+C for 200, not the
    # single component maximum of 150.
    cards = [
        _card(RecommendationType.PARALLELIZE_TOOLS, 100, 0.0, ["n1"]),
        _card(RecommendationType.PARALLELIZE_TOOLS, 150, 0.0, ["n1", "n2"]),
        _card(RecommendationType.PARALLELIZE_TOOLS, 100, 0.0, ["n2"]),
    ]
    agg = _aggregate_savings(cards, current_runtime=1000.0, current_cost=0.0)
    assert agg["latency_savings_ms"] == 200.0
    assert agg["explanation"]["selected_card_indexes"] == [0, 2]


def test_latency_and_cost_come_from_the_same_selection() -> None:
    # Overlapping alternatives with 100 ms/$0 and 0 ms/$10: no single plan
    # achieves both maxima, so the totals must come from one alternative.
    # Latency is the primary objective, so (100 ms, $0), never (100 ms, $10).
    cards = [
        _card(RecommendationType.PARALLELIZE_TOOLS, 100, 0.0, ["n1"]),
        _card(RecommendationType.ROUTE_TO_SMALLER_MODEL, 0, 10.0, ["n1"]),
    ]
    agg = _aggregate_savings(cards, current_runtime=1000.0, current_cost=20.0)
    assert agg["latency_savings_ms"] == 100.0
    assert agg["cost_savings_usd"] == 0.0
    assert agg["explanation"]["selected_card_indexes"] == [0]


def test_equal_latency_ties_break_by_cost() -> None:
    cards = [
        _card(RecommendationType.PARALLELIZE_TOOLS, 100, 0.0, ["n1"]),
        _card(RecommendationType.BATCH_MODEL_CALLS, 100, 5.0, ["n1"]),
    ]
    agg = _aggregate_savings(cards, current_runtime=1000.0, current_cost=20.0)
    assert agg["latency_savings_ms"] == 100.0
    assert agg["cost_savings_usd"] == 5.0
    assert agg["explanation"]["selected_card_indexes"] == [1]


def test_equal_savings_ties_are_deterministic() -> None:
    items = [
        SavingsItem(frozenset({"shared"}), 100.0, 1.0),
        SavingsItem(frozenset({"shared"}), 100.0, 1.0),
    ]
    selections = [select_compatible(items) for _ in range(5)]
    assert {selection.indices for selection in selections} == {(0,)}


def test_disjoint_cards_are_additive() -> None:
    # Batching + routing on different spans do not overlap, so they sum.
    cards = [
        _card(RecommendationType.BATCH_MODEL_CALLS, 100, 0.02, ["m1", "m2"]),
        _card(RecommendationType.ROUTE_TO_SMALLER_MODEL, 50, 0.05, ["m3"]),
    ]
    agg = _aggregate_savings(cards, current_runtime=1000.0, current_cost=1.0)
    assert agg["latency_savings_ms"] == 150.0
    assert agg["cost_savings_usd"] == 0.07
    assert agg["explanation"]["selected_card_indexes"] == [0, 1]


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


def test_cards_without_affected_spans_always_count() -> None:
    cards = [
        _card(RecommendationType.CACHE_CONTEXT, 100, 0.0, []),
        _card(RecommendationType.ADD_SCHEMA_VALIDATION, 50, 0.0, []),
    ]
    agg = _aggregate_savings(cards, current_runtime=1000.0, current_cost=0.0)
    assert agg["latency_savings_ms"] == 150.0
    assert agg["explanation"]["selected_card_indexes"] == [0, 1]


def test_seventeen_item_star_uses_exact_optimum() -> None:
    all_spans = [f"s{i}" for i in range(16)]
    items = [SavingsItem(frozenset(all_spans), 100.0, 0.0)]
    items.extend(SavingsItem(frozenset({span}), 99.0, 0.0) for span in all_spans)

    selection = select_compatible(items)

    assert selection.indices == tuple(range(1, 17))
    assert selection.latency_ms == 1584.0
    assert selection.optimal is True
    assert selection.algorithm == "exact_branch_and_bound"
    assert selection.exact_component_limit >= 17


def test_large_component_fallback_is_explicitly_approximate() -> None:
    all_spans = [f"s{i}" for i in range(24)]
    items = [SavingsItem(frozenset(all_spans), 100.0, 0.0)]
    items.extend(SavingsItem(frozenset({span}), 99.0, 0.0) for span in all_spans)

    selection = select_compatible(items)

    assert selection.indices == (0,)
    assert selection.latency_ms == 100.0
    assert selection.optimal is False
    assert selection.algorithm == "hybrid_exact_greedy"
    selected_spans: set[str] = set()
    for index in selection.indices:
        assert not (items[index].spans & selected_spans)
        selected_spans.update(items[index].spans)


def test_aggregate_exposes_approximation_metadata() -> None:
    all_spans = [f"s{i}" for i in range(24)]
    cards = [_card(RecommendationType.PARALLELIZE_TOOLS, 100, 0.0, all_spans)]
    cards.extend(
        _card(RecommendationType.PARALLELIZE_TOOLS, 99, 0.0, [span]) for span in all_spans
    )

    agg = _aggregate_savings(cards, current_runtime=5000.0, current_cost=0.0)
    explanation = agg["explanation"]

    assert explanation["selection_optimal"] is False
    assert explanation["selection_algorithm"] == "hybrid_exact_greedy"
    assert explanation["exact_component_limit"] == 24
    assert "approximation" in explanation["rule"]
    assert "maximizes" not in explanation["rule"]


# --- end-to-end reproduction from the issue ----------------------------------


def _alternating_tool_trace() -> AgentTrace:
    """Eight alternating 100 ms tool events named 'a' and 'b.

    Reproduces the two-parallelization-plus-oscillation overlap from the issue.
    """
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
    assert agg["selection_optimal"] is True
