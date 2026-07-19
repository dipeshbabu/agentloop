from __future__ import annotations

from agentloop.demo import run_baseline
from agentloop.graph import ExecutionGraph
from agentloop.optimizer import build_optimization_plan
from agentloop.tracer import AgentTrace, trace_agent, trace_model_call


def test_execution_graph_from_demo(tmp_path) -> None:
    path = run_baseline(tmp_path)
    trace = AgentTrace.from_json(path)
    graph = ExecutionGraph.from_trace(trace)
    data = graph.to_dict()
    assert len(data["nodes"]) > 0
    assert "critical_path" in data
    assert "bottlenecks" in data


def test_optimizer_plan_has_cards(tmp_path) -> None:
    path = run_baseline(tmp_path)
    trace = AgentTrace.from_json(path)
    plan = build_optimization_plan(trace)
    assert plan["current"]["runtime_ms"] > 0
    assert "estimated_after" in plan
    assert len(plan["optimization_cards"]) >= 1


def test_partial_cost_plan_does_not_publish_cost_savings() -> None:
    repeated = "stable context " * 100
    with trace_agent("partial") as trace:
        with trace_model_call(
            "known",
            model="gpt-4o",
            input_tokens=200,
            output_tokens=10,
            input_text=repeated,
        ):
            pass
        with trace_model_call(
            "unknown",
            model="private-model",
            input_tokens=200,
            output_tokens=10,
            input_text=repeated,
        ):
            pass

    plan = build_optimization_plan(trace)

    assert plan["cost_status"] == "partial"
    assert plan["current"]["estimated_cost_usd"] > 0
    assert plan["estimated_after"]["estimated_cost_usd"] is None
    assert plan["estimated_after"]["cost_reduction_pct"] is None
    assert plan["savings_aggregation"]["effective_cost_savings_usd"] is None
    cost_cards = [card for card in plan["optimization_cards"] if card["type"] == "cache_context"]
    assert cost_cards and cost_cards[0]["estimated_cost_savings_usd"] is None
    assert all(card["estimated_cost_savings_usd"] is None for card in plan["optimization_cards"])
