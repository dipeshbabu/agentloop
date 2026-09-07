from __future__ import annotations

import asyncio
from contextlib import nullcontext

import pytest

import agentloop
from agentloop.tracer import (
    AgentTrace,
    current_event_id,
    current_trace,
    record_model_call,
    record_tool_call,
    trace_agent,
    trace_tool_call,
)

RECORDERS = [record_model_call, record_tool_call]
STARTED_AT = "2026-01-01T00:00:00+00:00"


@pytest.mark.parametrize("record", RECORDERS)
@pytest.mark.parametrize("parent_id", [None, "captured-parent"])
def test_completed_event_accepts_explicit_trace_without_ambient_context(record, parent_id) -> None:
    trace = AgentTrace(name="captured")
    assert current_trace() is None

    record("completed", duration_ms=12, started_at=STARTED_AT, trace=trace, parent_id=parent_id)

    assert len(trace.events) == 1
    assert trace.events[0].run_id == trace.run_id
    assert trace.events[0].parent_id == parent_id
    assert trace.events[0].duration_ms == 12
    assert current_trace() is None
    assert current_event_id() is None


@pytest.mark.parametrize("record", RECORDERS)
@pytest.mark.parametrize("same_trace", [False, True])
@pytest.mark.parametrize("parent_id", [None, "captured-parent"])
def test_explicit_trace_never_inherits_ambient_parent(record, same_trace, parent_id) -> None:
    with trace_agent("ambient") as ambient:
        with trace_tool_call("ambient-parent"):
            ambient_parent = current_event_id()
            target = ambient if same_trace else AgentTrace(name="captured")

            record(
                "completed",
                duration_ms=12,
                started_at=STARTED_AT,
                trace=target,
                parent_id=parent_id,
            )

            event = target.events[-1]
            assert event.run_id == target.run_id
            assert event.parent_id == parent_id
            assert current_trace() is ambient
            assert current_event_id() == ambient_parent
            if not same_trace:
                assert ambient.events == []


@pytest.mark.parametrize("record", RECORDERS)
@pytest.mark.parametrize("options", [{}, {"trace": None}])
def test_implicit_trace_retains_ambient_parent_and_requires_active_trace(record, options) -> None:
    with pytest.raises(RuntimeError, match="No active AgentLoop trace"):
        record("missing", duration_ms=0, started_at=STARTED_AT, **options)

    with trace_agent("ambient") as trace:
        with trace_tool_call("parent"):
            parent_id = current_event_id()
            record("completed", duration_ms=12, started_at=STARTED_AT, **options)
            assert trace.events[-1].parent_id == parent_id
            assert trace.events[-1].run_id == trace.run_id


@pytest.mark.parametrize(
    "failure", [None, ValueError("failed"), asyncio.CancelledError(), GeneratorExit()]
)
def test_bind_trace_context_restores_nested_context_even_on_base_exceptions(failure) -> None:
    captured = AgentTrace(name="captured")
    nested = AgentTrace(name="nested")
    with trace_agent("ambient") as ambient:
        with trace_tool_call("ambient-parent"):
            ambient_parent = current_event_id()
            expected = pytest.raises(type(failure)) if failure is not None else nullcontext()
            with expected as caught:
                with agentloop.bind_trace_context(captured, "captured-parent"):
                    assert current_trace() is captured
                    assert current_event_id() == "captured-parent"
                    with agentloop.bind_trace_context(nested):
                        assert current_trace() is nested
                        assert current_event_id() is None
                    assert current_trace() is captured
                    assert current_event_id() == "captured-parent"
                    if failure is not None:
                        raise failure
            if failure is not None:
                assert caught.value is failure
            assert current_trace() is ambient
            assert current_event_id() == ambient_parent
    assert current_trace() is None
    assert current_event_id() is None
    assert captured.events == nested.events == []
    assert captured.ended_at is nested.ended_at is None


def test_bind_trace_context_is_task_local_and_resets_after_cancellation() -> None:
    async def run() -> None:
        first = AgentTrace(name="first")
        second = AgentTrace(name="second")
        first_started = asyncio.Event()
        second_started = asyncio.Event()
        release = asyncio.Event()

        with trace_agent("ambient") as ambient:
            with trace_tool_call("ambient-parent"):
                ambient_parent = current_event_id()

                async def worker(target, event_id, started):
                    try:
                        with agentloop.bind_trace_context(target, event_id):
                            started.set()
                            await release.wait()
                            assert current_trace() is target
                            assert current_event_id() == event_id
                            record_tool_call("completed", duration_ms=0, started_at=STARTED_AT)
                    finally:
                        assert current_trace() is ambient
                        assert current_event_id() == ambient_parent

                cancelled = asyncio.create_task(worker(first, "first-parent", first_started))
                completed = asyncio.create_task(worker(second, "second-parent", second_started))
                await asyncio.wait_for(
                    asyncio.gather(first_started.wait(), second_started.wait()), timeout=30
                )
                assert current_trace() is ambient
                assert current_event_id() == ambient_parent
                cancelled.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await cancelled
                release.set()
                await completed
                assert first.events == []
                assert len(second.events) == 1
                assert second.events[0].parent_id == "second-parent"
                assert second.events[0].run_id == second.run_id
        assert current_trace() is None
        assert current_event_id() is None

    asyncio.run(run())
