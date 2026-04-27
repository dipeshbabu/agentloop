from __future__ import annotations

import asyncio
from typing import Any

from agentloop.integrations.langgraph import instrument_state_graph, trace_node, trace_runnable
from agentloop.tracer import trace_agent


class FakeStateGraph:
    def __init__(self) -> None:
        self.nodes: list[tuple[str, Any]] = []

    def add_node(self, name: str, action: Any) -> None:
        self.nodes.append((name, action))

    def compile(self) -> "FakeCompiledGraph":
        return FakeCompiledGraph(self.nodes)


class FakeCompiledGraph:
    def __init__(self, nodes: list[tuple[str, Any]]) -> None:
        self.nodes = nodes

    def invoke(self, state: dict[str, Any]) -> dict[str, Any]:
        for _, action in self.nodes:
            state = action(state)
        return state

    async def ainvoke(self, state: dict[str, Any]) -> dict[str, Any]:
        for _, action in self.nodes:
            result = action(state)
            if hasattr(result, "__await__"):
                state = await result
            else:
                state = result
        return state


def test_trace_node_records_sync_event() -> None:
    @trace_node("step")
    def step(state: dict[str, Any]) -> dict[str, Any]:
        state["x"] = 1
        return state

    with trace_agent("test") as trace:
        result = step({})
    assert result["x"] == 1
    assert len(trace.events) == 1
    assert trace.events[0].name == "step"


def test_trace_node_records_async_event() -> None:
    @trace_node("async_step")
    async def step(state: dict[str, Any]) -> dict[str, Any]:
        state["x"] = 1
        return state

    async def run() -> None:
        with trace_agent("test") as trace:
            result = await step({})
        assert result["x"] == 1
        assert len(trace.events) == 1
        assert trace.events[0].name == "async_step"

    asyncio.run(run())


def test_instrument_state_graph_wraps_added_nodes() -> None:
    graph = FakeStateGraph()
    instrument_state_graph(graph)

    def first(state: dict[str, Any]) -> dict[str, Any]:
        state["first"] = True
        return state

    def second(state: dict[str, Any]) -> dict[str, Any]:
        state["second"] = True
        return state

    graph.add_node("first", first)
    graph.add_node("second", second)

    app = trace_runnable(graph.compile(), name="fake_graph")
    result = app.invoke({})
    assert result == {"first": True, "second": True}
    assert app.last_trace is not None
    assert len(app.last_trace.events) == 2
    assert [event.name for event in app.last_trace.events] == ["first", "second"]
