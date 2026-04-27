from __future__ import annotations

from agentloop.demo import run_baseline
from agentloop.graph import ExecutionGraph
from agentloop.optimizer import build_optimization_plan
from agentloop.tracer import AgentTrace


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
