from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Generator
from typing import Any

import pytest

import agentloop
from agentloop.runtime import reset_runtime
from agentloop.tracer import current_event_id, trace_agent, trace_model_call


def test_traceable_root_creates_trace() -> None:
    reset_runtime()
    captured: dict[str, Any] = {}

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
    captured: dict[str, Any] = {}

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
    captured: dict[str, Any] = {}

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
    captured: dict[str, Any] = {}

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


def test_traceable_root_generator_is_active_only_while_resuming() -> None:
    reset_runtime()
    captured: dict[str, Any] = {}

    @agentloop.trace_tool(name="inner")
    def inner() -> int:
        return 1

    @agentloop.traceable(root=True, name="gen")
    def gen() -> Generator[int, None, None]:
        captured["trace"] = agentloop.current_trace()
        yield inner()
        yield inner()

    iterator = gen()
    assert agentloop.current_trace() is None
    assert next(iterator) == 1
    assert agentloop.current_trace() is None
    assert current_event_id() is None
    assert list(iterator) == [1]

    trace = captured["trace"]
    assert trace.ended_at is not None
    events = {event.name: event for event in trace.events}
    assert events["inner"].parent_id == events["gen"].event_id
    assert events["gen"].status == "ok"


def test_traceable_generator_records_span_after_exhaustion() -> None:
    reset_runtime()

    @agentloop.trace_tool(name="inner")
    def inner() -> int:
        return 1

    with trace_agent("outer") as trace:

        @agentloop.trace_tool(name="gen")
        def gen() -> Generator[int, None, None]:
            yield inner()

        assert list(gen()) == [1]

    events = {event.name: event for event in trace.events}
    assert set(events) == {"inner", "gen"}
    assert events["gen"].status == "ok"
    assert events["gen"].parent_id is None
    assert events["inner"].parent_id == events["gen"].event_id


def test_traceable_generator_early_close_records_ok_and_releases_context() -> None:
    reset_runtime()

    with trace_agent("outer") as trace:

        @agentloop.trace_tool(name="gen")
        def gen() -> Generator[int, None, None]:
            yield 1
            yield 2

        iterator = gen()
        assert next(iterator) == 1
        assert current_event_id() is None
        iterator.close()
        assert current_event_id() is None

    gen_events = [event for event in trace.events if event.name == "gen"]
    assert len(gen_events) == 1
    assert gen_events[0].status == "ok"
    assert gen_events[0].error is None
    assert gen_events[0].duration_ms >= 0


def test_traceable_generator_midway_error_records_once_and_propagates_identity() -> None:
    reset_runtime()
    failure = ValueError("boom")

    with trace_agent("outer") as trace:

        @agentloop.trace_tool(name="gen")
        def gen() -> Generator[int, None, None]:
            yield 1
            raise failure

        iterator = gen()
        assert next(iterator) == 1
        with pytest.raises(ValueError, match="boom") as caught:
            next(iterator)

    assert caught.value is failure
    gen_events = [event for event in trace.events if event.name == "gen"]
    assert len(gen_events) == 1
    assert gen_events[0].status == "error"
    assert gen_events[0].error == "boom"


def test_traceable_generator_preserves_send_and_return_value() -> None:
    reset_runtime()

    with trace_agent("outer") as trace:

        @agentloop.trace_tool(name="gen")
        def gen() -> Generator[str, str, str]:
            payload = yield "ready"
            yield f"sent:{payload}"
            return "done"

        iterator = gen()
        assert next(iterator) == "ready"
        assert iterator.send("payload") == "sent:payload"
        with pytest.raises(StopIteration) as stopped:
            next(iterator)

    assert stopped.value.value == "done"
    gen_events = [event for event in trace.events if event.name == "gen"]
    assert len(gen_events) == 1
    assert gen_events[0].status == "ok"


def test_traceable_generator_forwards_thrown_exception() -> None:
    reset_runtime()
    thrown = ValueError("boom")
    captured: dict[str, BaseException] = {}

    with trace_agent("outer") as trace:

        @agentloop.trace_tool(name="gen")
        def gen() -> Generator[str, None, None]:
            try:
                yield "ready"
            except ValueError as exc:
                captured["exception"] = exc
                yield "handled"

        iterator = gen()
        assert next(iterator) == "ready"
        assert iterator.throw(thrown) == "handled"
        with pytest.raises(StopIteration):
            next(iterator)

    assert captured["exception"] is thrown
    gen_events = [event for event in trace.events if event.name == "gen"]
    assert len(gen_events) == 1
    assert gen_events[0].status == "ok"


def test_traceable_generators_do_not_leak_parent_context_when_interleaved() -> None:
    reset_runtime()

    with trace_agent("outer") as trace:

        @agentloop.trace_tool(name="one-inner")
        def one_inner() -> int:
            return 1

        @agentloop.trace_tool(name="two-inner")
        def two_inner() -> int:
            return 2

        @agentloop.trace_tool(name="sibling")
        def sibling() -> int:
            return 3

        @agentloop.trace_tool(name="one")
        def one() -> Generator[int, None, None]:
            yield one_inner()
            yield one_inner()

        @agentloop.trace_tool(name="two")
        def two() -> Generator[int, None, None]:
            yield two_inner()
            yield two_inner()

        first = one()
        second = two()
        assert next(first) == 1
        assert current_event_id() is None
        assert sibling() == 3
        assert next(second) == 2
        assert next(first) == 1
        assert next(second) == 2
        with pytest.raises(StopIteration):
            next(first)
        with pytest.raises(StopIteration):
            next(second)

    events = {event.name: event for event in trace.events if event.name != "one-inner"}
    one_event = next(event for event in trace.events if event.name == "one")
    two_event = next(event for event in trace.events if event.name == "two")
    one_inner_events = [event for event in trace.events if event.name == "one-inner"]
    two_inner_events = [event for event in trace.events if event.name == "two-inner"]

    assert events["sibling"].parent_id is None
    assert all(event.parent_id == one_event.event_id for event in one_inner_events)
    assert all(event.parent_id == two_event.event_id for event in two_inner_events)


def test_traceable_async_generator_keeps_trace_active_and_records_once() -> None:
    reset_runtime()

    @agentloop.trace_tool(name="inner")
    async def inner() -> int:
        return 1

    async def run() -> tuple[Any, list[int]]:
        with trace_agent("outer") as trace:

            @agentloop.trace_tool(name="agen")
            async def agen() -> AsyncGenerator[int, None]:
                yield await inner()
                yield await inner()

            collected = [item async for item in agen()]
        return trace, collected

    trace, collected = asyncio.run(run())
    assert collected == [1, 1]
    events = {event.name: event for event in trace.events}
    assert events["agen"].status == "ok"
    assert events["agen"].parent_id is None
    assert events["inner"].parent_id == events["agen"].event_id
    assert len([event for event in trace.events if event.name == "agen"]) == 1


def test_traceable_async_generator_cross_task_aclose_records_ok() -> None:
    reset_runtime()

    async def run() -> Any:
        with trace_agent("outer") as trace:

            @agentloop.trace_tool(name="agen")
            async def agen() -> AsyncGenerator[int, None]:
                yield 1
                yield 2

            iterator = agen()
            assert await iterator.__anext__() == 1
            assert current_event_id() is None
            await asyncio.create_task(iterator.aclose())
            assert current_event_id() is None
        return trace

    trace = asyncio.run(run())
    agen_events = [event for event in trace.events if event.name == "agen"]
    assert len(agen_events) == 1
    assert agen_events[0].status == "ok"
    assert agen_events[0].error is None


def test_traceable_async_generator_midway_error_records_once_and_propagates_identity() -> None:
    reset_runtime()
    failure = ValueError("boom")

    async def run() -> Any:
        with trace_agent("outer") as trace:

            @agentloop.trace_tool(name="agen")
            async def agen() -> AsyncGenerator[int, None]:
                yield 1
                raise failure

            iterator = agen()
            assert await iterator.__anext__() == 1
            with pytest.raises(ValueError, match="boom") as caught:
                await iterator.__anext__()
            assert caught.value is failure
        return trace

    trace = asyncio.run(run())
    agen_events = [event for event in trace.events if event.name == "agen"]
    assert len(agen_events) == 1
    assert agen_events[0].status == "error"
    assert agen_events[0].error == "boom"


def test_traceable_async_generator_preserves_asend() -> None:
    reset_runtime()

    async def run() -> Any:
        with trace_agent("outer") as trace:

            @agentloop.trace_tool(name="agen")
            async def agen() -> AsyncGenerator[str, str]:
                payload = yield "ready"
                yield f"sent:{payload}"

            iterator = agen()
            assert await iterator.__anext__() == "ready"
            assert await iterator.asend("payload") == "sent:payload"
            with pytest.raises(StopAsyncIteration):
                await iterator.__anext__()
        return trace

    trace = asyncio.run(run())
    agen_events = [event for event in trace.events if event.name == "agen"]
    assert len(agen_events) == 1
    assert agen_events[0].status == "ok"


def test_traceable_async_generator_forwards_athrow() -> None:
    reset_runtime()
    thrown = ValueError("boom")
    captured: dict[str, BaseException] = {}

    async def run() -> Any:
        with trace_agent("outer") as trace:

            @agentloop.trace_tool(name="agen")
            async def agen() -> AsyncGenerator[str, None]:
                try:
                    yield "ready"
                except ValueError as exc:
                    captured["exception"] = exc
                    yield "handled"

            iterator = agen()
            assert await iterator.__anext__() == "ready"
            assert await iterator.athrow(thrown) == "handled"
            with pytest.raises(StopAsyncIteration):
                await iterator.__anext__()
        return trace

    trace = asyncio.run(run())
    assert captured["exception"] is thrown
    agen_events = [event for event in trace.events if event.name == "agen"]
    assert len(agen_events) == 1
    assert agen_events[0].status == "ok"


def test_traceable_async_generator_cancellation_records_once_and_cleans_context() -> None:
    reset_runtime()
    captured: dict[str, Any] = {}

    async def run() -> None:
        started = asyncio.Event()

        @agentloop.traceable(root=True, name="agen")
        async def agen() -> AsyncGenerator[int, None]:
            captured["trace"] = agentloop.current_trace()
            started.set()
            await asyncio.Future()
            yield 1

        iterator = agen()
        task = asyncio.create_task(iterator.__anext__())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert task.cancelled() is True
        assert agentloop.current_trace() is None
        assert current_event_id() is None

    asyncio.run(run())

    agen_events = [event for event in captured["trace"].events if event.name == "agen"]
    assert len(agen_events) == 1
    assert agen_events[0].status == "error"
    assert agen_events[0].error == "CancelledError"


def test_traceable_async_generator_cancelled_aclose_records_once() -> None:
    reset_runtime()

    async def run() -> Any:
        close_started = asyncio.Event()

        with trace_agent("outer") as trace:

            @agentloop.trace_tool(name="agen")
            async def agen() -> AsyncGenerator[int, None]:
                try:
                    yield 1
                finally:
                    close_started.set()
                    await asyncio.Future()

            iterator = agen()
            assert await iterator.__anext__() == 1
            close_task = asyncio.create_task(iterator.aclose())
            await close_started.wait()
            close_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await close_task
            assert current_event_id() is None
        return trace

    trace = asyncio.run(run())
    agen_events = [event for event in trace.events if event.name == "agen"]
    assert len(agen_events) == 1
    assert agen_events[0].status == "error"
    assert agen_events[0].error == "CancelledError"
