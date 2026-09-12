from __future__ import annotations

import asyncio
import json

import pytest
from integration_conformance import ADAPTERS, CAPABILITIES, AsyncStream, SyncStream, single_event

from agentloop.tracer import current_event_id, current_trace, trace_agent, trace_tool_call


@pytest.fixture(params=ADAPTERS, ids=lambda adapter: adapter.name)
def adapter(request):
    return request.param


def test_capability_declarations_are_complete(adapter):
    assert set(adapter.capabilities) == CAPABILITIES
    assert adapter.without_trace in {"passthrough", "requires_active_trace"}
    for value in adapter.capabilities.values():
        assert value is True or isinstance(value, str) and value.strip()


def test_sync_identity_arguments_privacy_and_repeated_instrumentation(adapter):
    adapter.require("sync")
    response = {"result": "private-result-123"}
    calls = []

    def operation(*args, **kwargs):
        calls.append((args, kwargs))
        return response

    wrapped = adapter.wrap(operation, 3)
    with trace_agent("conformance") as trace:
        assert wrapped("private-prompt-123", api_key="secret-key-123") is response
    assert calls == [(("private-prompt-123",), {"api_key": "secret-key-123"})]
    single_event(trace, adapter)
    serialized = json.dumps(trace.to_dict())
    for secret in ("private-prompt-123", "secret-key-123", "private-result-123"):
        assert secret not in serialized


def test_sync_original_exception_identity(adapter):
    adapter.require("sync")
    error = ValueError("original failure")

    def operation():
        raise error

    with trace_agent("failure") as trace:
        with pytest.raises(ValueError) as caught:
            adapter.wrap(operation, 2)()
        assert caught.value is error
        assert current_trace() is trace
    assert single_event(trace, adapter, status="error").error == str(error)
    assert current_trace() is None


@pytest.mark.parametrize("outcome", ["return", "exception", "cancel"])
def test_async_return_exception_and_cancellation_identity(adapter, outcome):
    adapter.require("async")
    response = object()
    error = asyncio.CancelledError("cancelled") if outcome == "cancel" else ValueError("original")
    calls = []

    async def operation(**kwargs):
        calls.append(kwargs)
        if outcome != "return":
            raise error
        return response

    wrapped = adapter.wrap(operation, 3)

    async def run():
        with trace_agent("async") as trace:
            if outcome == "return":
                assert await wrapped(value=1) is response
            else:
                with pytest.raises(type(error)) as caught:
                    await wrapped(value=1)
                assert caught.value is error
        single_event(trace, adapter, status="ok" if outcome == "return" else "error")
        assert current_trace() is None

    asyncio.run(run())
    assert calls == [{"value": 1}]


def test_calls_inherit_parent_and_restore_context(adapter):
    adapter.require("sync")
    wrapped = adapter.wrap(lambda: object(), 2)
    with trace_agent("nested") as trace:
        with trace_tool_call("parent"):
            parent_id = current_event_id()
            wrapped()
            assert current_event_id() == parent_id
        assert current_event_id() is None
    assert len(trace.events) == 2
    child = next(event for event in trace.events if event.event_id != parent_id)
    assert child.parent_id == parent_id


def test_no_active_trace_follows_declared_policy(adapter):
    response = object()
    wrapped = adapter.wrap(lambda: response, 2)
    assert current_trace() is None
    if adapter.without_trace == "passthrough":
        assert wrapped() is response
    else:
        with pytest.raises(RuntimeError, match="No active AgentLoop trace"):
            wrapped()
    assert current_trace() is None


def test_absent_model_usage_is_unavailable_without_changing_result(adapter):
    adapter.require("model_usage")
    response = object()
    with trace_agent("missing-usage") as trace:
        assert adapter.wrap(lambda: response, 2)() is response
    event = single_event(trace, adapter)
    assert event.token_provenance == "unavailable"
    assert event.input_tokens == event.output_tokens == 0


@pytest.mark.parametrize(
    "mode", ["consume", "close_empty", "close_partial", "failure", "close_failure"]
)
def test_sync_stream_lifecycle(adapter, mode):
    adapter.require("sync_stream")
    chunks = [object(), {"usage": {"input_tokens": 12, "output_tokens": 3}}]
    error = ValueError("stream failure")
    source = SyncStream(
        chunks,
        error=error if mode == "failure" else None,
        close_error=error if mode == "close_failure" else None,
    )
    wrapped = adapter.wrap(lambda **kwargs: source, 2)
    with trace_agent("stream") as trace:
        stream = wrapped(stream=True)
        assert not trace.events
        if mode != "close_empty":
            assert next(stream) is chunks[0]
            assert not trace.events
        if mode == "consume":
            remaining = list(stream)
            assert len(remaining) == 1 and remaining[0] is chunks[1]
        elif mode in {"close_empty", "close_partial"}:
            assert stream.close() is source.close_result
            assert source.close_count == 1
        else:
            with pytest.raises(ValueError) as caught:
                stream.close() if mode == "close_failure" else next(stream)
            assert caught.value is error
    event = single_event(trace, adapter, status="error" if "failure" in mode else "ok")
    if mode == "consume":
        assert (event.input_tokens, event.output_tokens) == (12, 3)


@pytest.mark.parametrize(
    "mode", ["consume", "close_empty", "close_partial", "failure", "cancel", "close_failure"]
)
def test_async_stream_lifecycle(adapter, mode):
    adapter.require("async_stream")
    chunks = [object(), {"usage": {"input_tokens": 12, "output_tokens": 3}}]
    error = (
        asyncio.CancelledError("cancelled stream")
        if mode == "cancel"
        else ValueError("stream failure")
    )
    source = AsyncStream(
        chunks,
        error=error if mode in {"failure", "cancel"} else None,
        close_error=error if mode == "close_failure" else None,
    )

    async def operation(**kwargs):
        return source

    wrapped = adapter.wrap(operation, 2)

    async def run():
        with trace_agent("async-stream") as trace:
            stream = await wrapped(stream=True)
            assert not trace.events
            if mode != "close_empty":
                assert await anext(stream) is chunks[0]
                assert not trace.events
            if mode == "consume":
                remaining = [chunk async for chunk in stream]
                assert len(remaining) == 1 and remaining[0] is chunks[1]
            elif mode in {"close_empty", "close_partial"}:
                assert await stream.aclose() is source.source.close_result
                assert source.source.close_count == 1
            else:
                with pytest.raises(type(error)) as caught:
                    if mode == "close_failure":
                        await stream.aclose()
                    else:
                        await anext(stream)
                assert caught.value is error
        event = single_event(
            trace,
            adapter,
            status="error" if mode in {"failure", "cancel", "close_failure"} else "ok",
        )
        if mode == "consume":
            assert (event.input_tokens, event.output_tokens) == (12, 3)
        assert current_trace() is None

    asyncio.run(run())
