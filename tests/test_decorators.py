from __future__ import annotations

import asyncio

import pytest

import agentloop
from agentloop.runtime import reset_runtime
from agentloop.tracer import current_event_id, trace_agent, trace_model_call


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


def test_native_model_span_records_cancellation_and_preserves_exception() -> None:
    cancellation = asyncio.CancelledError()

    with trace_agent("cancelled_model") as trace:
        with pytest.raises(asyncio.CancelledError) as caught:
            with trace_model_call("model"):
                raise cancellation

    assert caught.value is cancellation
    assert len(trace.events) == 1
    assert trace.events[0].status == "error"
    assert trace.events[0].error == "CancelledError"


def test_traceable_direct_task_cancellation_records_once_and_cleans_context() -> None:
    captured = {}

    async def run() -> None:
        started = asyncio.Event()

        @agentloop.traceable(root=True, name="cancelled_agent")
        async def work() -> None:
            captured["trace"] = agentloop.current_trace()
            started.set()
            await asyncio.Future()

        async def supervised() -> None:
            try:
                await work()
            except asyncio.CancelledError:
                captured["cleanup_trace"] = agentloop.current_trace()
                captured["cleanup_event"] = current_event_id()
                raise

        task = asyncio.create_task(supervised())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert task.cancelled() is True

    asyncio.run(run())

    trace = captured["trace"]
    assert len(trace.events) == 1
    assert trace.events[0].name == "cancelled_agent"
    assert trace.events[0].status == "error"
    assert trace.events[0].error == "CancelledError"
    assert captured["cleanup_trace"] is None
    assert captured["cleanup_event"] is None


def test_traceable_timeout_records_nested_cancellation_without_duplicate_events() -> None:
    captured = {}

    async def run() -> None:
        started = asyncio.Event()

        @agentloop.traceable(root=True, name="outer")
        async def work() -> None:
            captured["trace"] = agentloop.current_trace()
            with trace_model_call("inner"):
                started.set()
                await asyncio.Future()

        task = asyncio.create_task(work())
        await started.wait()
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(task, timeout=0)
        assert task.cancelled() is True

    asyncio.run(run())

    events = {event.name: event for event in captured["trace"].events}
    assert set(events) == {"inner", "outer"}
    assert all(event.status == "error" for event in events.values())
    assert all(event.error == "CancelledError" for event in events.values())
    assert events["inner"].parent_id == events["outer"].event_id
