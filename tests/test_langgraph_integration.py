from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

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

    def stream(self, state):
        yield self.invoke(state)

    async def astream(self, state):
        yield await self.ainvoke(state)


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


def test_trace_node_records_async_cancellation_once_and_propagates() -> None:
    cancellation = asyncio.CancelledError()

    @trace_node("cancelled_step")
    async def step(state: dict[str, Any]) -> dict[str, Any]:
        raise cancellation

    async def run() -> None:
        with trace_agent("test") as trace:
            with pytest.raises(asyncio.CancelledError) as caught:
                await step({})
        assert caught.value is cancellation
        assert len(trace.events) == 1
        assert trace.events[0].name == "cancelled_step"
        assert trace.events[0].status == "error"
        assert trace.events[0].error == "CancelledError"

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


@pytest.mark.parametrize("nodes", [{"existing": object()}, [("existing", object())]])
def test_late_instrumentation_rejects_without_mutating_builder(nodes):
    def add_node(*args, **kwargs):
        return None

    graph = SimpleNamespace(nodes=nodes, add_node=add_node)
    with pytest.raises(ValueError, match="before.*add_node"):
        instrument_state_graph(graph)
    assert graph.add_node is add_node
    assert graph.nodes is nodes
    assert not hasattr(graph, "_agentloop_instrumented")


@pytest.mark.parametrize("nodes", [None, 0, "", b"", object()])
def test_uninspectable_node_registry_is_rejected(nodes):
    graph = SimpleNamespace(nodes=nodes, add_node=lambda *args: None)
    with pytest.raises(TypeError, match="nodes.*mapping or sequence"):
        instrument_state_graph(graph)


@pytest.mark.parametrize(
    "graph",
    [object(), SimpleNamespace(add_node=None), SimpleNamespace(add_node=lambda *args: None)],
)
def test_unsupported_builder_is_rejected(graph):
    with pytest.raises(TypeError):
        instrument_state_graph(graph)


@pytest.mark.parametrize("mode", ["invoke", "ainvoke", "stream", "astream"])
def test_repeated_instrumentation_preserves_execution_modes(mode):
    graph = FakeStateGraph()
    assert instrument_state_graph(graph) is graph
    original_error = ValueError("node failed")

    def step(state):
        if state.get("fail"):
            raise original_error
        return {"done": True}

    graph.add_node("step", step)
    assert instrument_state_graph(graph) is graph
    app = trace_runnable(graph.compile())
    if mode == "invoke":
        result = app.invoke({})
    elif mode == "stream":
        result = list(app.stream({}))[0]
    else:

        async def run():
            if mode == "ainvoke":
                return await app.ainvoke({})
            return [item async for item in app.astream({})][0]

        result = asyncio.run(run())
    assert result == {"done": True}
    assert app.last_trace.ended_at is not None
    assert [event.name for event in app.last_trace.events] == ["step"]
    with trace_agent("failure") as trace:
        with pytest.raises(ValueError) as caught:
            graph.nodes[0][1]({"fail": True})
    assert caught.value is original_error
    assert trace.events[0].status == "error"
