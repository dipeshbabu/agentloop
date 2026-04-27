from __future__ import annotations

import asyncio

import agentloop
from agentloop.runtime import reset_runtime
from agentloop.tracer import trace_agent


def test_traceable_root_creates_trace() -> None:
    reset_runtime()
    captured = {}

    @agentloop.trace_tool(name="inner_tool")
    def inner(value: int) -> int:
        return value + 1

    @agentloop.traceable(root=True, agent_name="decorated_agent")
    def run(value: int) -> int:
        result = inner(value)
        captured["trace"] = agentloop.current_trace()
        return result

    assert run(2) == 3
    trace = captured["trace"]
    assert trace.name == "decorated_agent"
    assert [event.name for event in trace.events] == ["inner_tool", "run"]


def test_trace_model_records_model_event() -> None:
    reset_runtime()

    @agentloop.trace_model(name="llm", model="demo-model")
    def call_model() -> str:
        return "ok"

    with trace_agent("agent") as trace:
        assert call_model() == "ok"

    assert len(trace.events) == 1
    event = trace.events[0]
    assert event.event_type == "model_call"
    assert event.name == "llm"
    assert event.model == "demo-model"


def test_traceable_async_function() -> None:
    reset_runtime()
    captured = {}

    @agentloop.traceable(root=True, name="async_agent", agent_name="async_agent")
    async def run() -> str:
        captured["trace"] = agentloop.current_trace()
        return "done"

    assert asyncio.run(run()) == "done"
    assert captured["trace"].name == "async_agent"
    assert captured["trace"].events[0].name == "async_agent"
