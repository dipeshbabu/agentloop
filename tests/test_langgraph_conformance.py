"""Optional real-SDK checks; see docs/INTEGRATIONS.md for the pinned invocation."""

from __future__ import annotations

import asyncio
from typing import TypedDict

import pytest

from agentloop.integrations.langgraph import instrument_state_graph, trace_runnable

langgraph = pytest.importorskip("langgraph.graph")


class State(TypedDict):
    value: int


def increment(state: State) -> State:
    return {"value": state["value"] + 1}


def builder(instrument: bool):
    graph = langgraph.StateGraph(State)
    if instrument:
        assert instrument_state_graph(graph) is graph
    graph.add_node("increment", increment)
    graph.set_entry_point("increment")
    graph.set_finish_point("increment")
    return graph


def test_real_builder_rejects_late_instrumentation_without_mutation():
    graph = builder(False)
    original = graph.add_node
    nodes = dict(graph.nodes)
    with pytest.raises(ValueError, match="before.*add_node"):
        instrument_state_graph(graph)
    assert graph.add_node == original
    assert graph.nodes == nodes
    assert graph.compile().invoke({"value": 0}) == {"value": 1}


@pytest.mark.parametrize("mode", ["invoke", "ainvoke", "stream", "astream"])
def test_real_compiled_graph_preserves_results_and_records_one_node(mode):
    graph = builder(True)
    assert instrument_state_graph(graph) is graph
    traced = trace_runnable(graph.compile())
    plain = builder(False).compile()
    if mode == "invoke":
        actual, expected = traced.invoke({"value": 0}), plain.invoke({"value": 0})
    elif mode == "stream":
        actual, expected = list(traced.stream({"value": 0})), list(plain.stream({"value": 0}))
    else:

        async def run():
            if mode == "ainvoke":
                return await traced.ainvoke({"value": 0}), await plain.ainvoke({"value": 0})
            return (
                [item async for item in traced.astream({"value": 0})],
                [item async for item in plain.astream({"value": 0})],
            )

        actual, expected = asyncio.run(run())
    assert actual == expected
    assert traced.last_trace.ended_at is not None
    assert [event.name for event in traced.last_trace.events] == ["increment"]
