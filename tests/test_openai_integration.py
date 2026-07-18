from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from agentloop.integrations.openai import instrument_callable, instrument_openai_client
from agentloop.tracer import trace_agent


class FakeSyncStream:
    def __init__(self, chunks):
        self._chunks = chunks

    def __iter__(self):
        return iter(self._chunks)


class FakeCtxStream:
    def __init__(self, chunks):
        self._chunks = chunks
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def __iter__(self):
        return iter(self._chunks)

    def close(self):
        self.closed = True


class FakeAsyncStream:
    def __init__(self, chunks):
        self._chunks = chunks

    def __aiter__(self):
        self._it = iter(self._chunks)
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration


class FakeResponses:
    def create(self, **kwargs):
        return SimpleNamespace(
            usage=SimpleNamespace(input_tokens=10, output_tokens=5), kwargs=kwargs
        )


class FakeCompletions:
    def create(self, **kwargs):
        return {"usage": {"prompt_tokens": 7, "completion_tokens": 3}, "kwargs": kwargs}


class FakeClient:
    def __init__(self):
        self.responses = FakeResponses()
        self.chat = SimpleNamespace(completions=FakeCompletions())


def test_instrument_openai_client_records_responses_call() -> None:
    client = instrument_openai_client(FakeClient())
    with trace_agent("openai_test") as trace:
        result = client.responses.create(model="gpt-test", input="hello")
    assert result.usage.input_tokens == 10
    assert len(trace.events) == 1
    assert trace.events[0].name == "openai.responses.create"
    assert trace.events[0].model == "gpt-test"
    assert trace.events[0].input_tokens == 10
    assert trace.events[0].output_tokens == 5


def test_instrument_openai_client_records_chat_call() -> None:
    client = instrument_openai_client(FakeClient())
    with trace_agent("chat_test") as trace:
        client.chat.completions.create(
            model="gpt-chat", messages=[{"role": "user", "content": "hi"}]
        )
    assert len(trace.events) == 1
    assert trace.events[0].name == "openai.chat.completions.create"
    assert trace.events[0].metadata["message_count"] == 1
    assert trace.events[0].input_tokens == 7
    assert trace.events[0].output_tokens == 3


def test_instrument_callable_records_async_call() -> None:
    async def call_model(**kwargs):
        return SimpleNamespace(usage=SimpleNamespace(input_tokens=2, output_tokens=4))

    wrapped = instrument_callable(call_model, name="custom.async")

    async def run() -> None:
        with trace_agent("async_test") as trace:
            await wrapped(model="gpt-async")
        assert len(trace.events) == 1
        assert trace.events[0].name == "custom.async"
        assert trace.events[0].model == "gpt-async"
        assert trace.events[0].input_tokens == 2
        assert trace.events[0].output_tokens == 4

    asyncio.run(run())


def test_double_instrumentation_records_one_event_per_request() -> None:
    client = FakeClient()
    instrument_openai_client(client)
    instrument_openai_client(client)  # idempotent: must not stack wrappers

    with trace_agent("double") as trace:
        client.responses.create(model="gpt-test", input="hello")

    assert len(trace.events) == 1


def test_instrument_callable_is_idempotent() -> None:
    def call(**kwargs):
        return {"usage": {"prompt_tokens": 1, "completion_tokens": 1}}

    wrapped = instrument_callable(call, name="c")
    rewrapped = instrument_callable(wrapped, name="c")
    assert wrapped is rewrapped

    with trace_agent("idem") as trace:
        rewrapped()
    assert len(trace.events) == 1


def test_call_without_active_trace_is_not_recorded() -> None:
    def create(**kwargs):
        return SimpleNamespace(usage=SimpleNamespace(input_tokens=1, output_tokens=1))

    wrapped = instrument_callable(create, name="notrace")
    # No active trace: previously this raised; it must now return normally.
    result = wrapped(model="gpt-test")
    assert result.usage.input_tokens == 1


def test_sync_stream_records_after_consumption() -> None:
    def create(**kwargs):
        return FakeSyncStream(
            [
                SimpleNamespace(delta="a"),
                SimpleNamespace(delta="b", usage=SimpleNamespace(input_tokens=11, output_tokens=7)),
            ]
        )

    wrapped = instrument_callable(create, name="openai.responses.create")

    with trace_agent("stream") as trace:
        stream = wrapped(model="gpt-test", stream=True)
        assert len(trace.events) == 0  # deferred: nothing recorded before consumption
        chunks = list(stream)
        assert len(chunks) == 2

    assert len(trace.events) == 1
    assert trace.events[0].input_tokens == 11
    assert trace.events[0].output_tokens == 7


def test_stream_without_usage_records_zero_tokens() -> None:
    def create(**kwargs):
        return FakeSyncStream([SimpleNamespace(delta="a"), SimpleNamespace(delta="b")])

    wrapped = instrument_callable(create, name="nousage")

    with trace_agent("nousage") as trace:
        list(wrapped(stream=True))

    assert len(trace.events) == 1
    assert trace.events[0].input_tokens == 0
    assert trace.events[0].output_tokens == 0


def test_sync_stream_mid_error_records_once_and_propagates() -> None:
    class FakeErrorStream:
        def __iter__(self):
            def gen():
                yield SimpleNamespace(delta="a")
                raise RuntimeError("boom")

            return gen()

    def create(**kwargs):
        return FakeErrorStream()

    wrapped = instrument_callable(create, name="err")

    with trace_agent("err") as trace:
        with pytest.raises(RuntimeError):
            for _ in wrapped(stream=True):
                pass

    assert len(trace.events) == 1
    assert trace.events[0].status == "error"
    assert "boom" in (trace.events[0].error or "")


def test_sync_stream_context_manager_and_early_close() -> None:
    stream_obj = FakeCtxStream([SimpleNamespace(delta=str(i)) for i in range(5)])

    def create(**kwargs):
        return stream_obj

    wrapped = instrument_callable(create, name="close")

    with trace_agent("close") as trace:
        stream = wrapped(stream=True)
        iterator = iter(stream)
        next(iterator)  # consume one chunk, then close early
        stream.close()

    assert stream_obj.closed is True
    assert len(trace.events) == 1


def test_async_stream_records_after_consumption() -> None:
    async def create(**kwargs):
        return FakeAsyncStream(
            [
                SimpleNamespace(delta="a"),
                SimpleNamespace(delta="b", usage=SimpleNamespace(input_tokens=3, output_tokens=9)),
            ]
        )

    wrapped = instrument_callable(create, name="astream")

    async def run() -> None:
        with trace_agent("astream") as trace:
            stream = await wrapped(stream=True)
            assert len(trace.events) == 0
            chunks = [chunk async for chunk in stream]
            assert len(chunks) == 2
        assert len(trace.events) == 1
        assert trace.events[0].input_tokens == 3
        assert trace.events[0].output_tokens == 9

    asyncio.run(run())


def test_responses_stream_captures_nested_final_usage() -> None:
    # The locked Responses streaming API delivers final usage on the terminal
    # ResponseCompletedEvent under event.response.usage, not event.usage.
    completed = SimpleNamespace(
        type="response.completed",
        response=SimpleNamespace(usage=SimpleNamespace(input_tokens=21, output_tokens=13)),
    )

    def create(**kwargs):
        return FakeSyncStream([SimpleNamespace(delta="a"), completed])

    wrapped = instrument_callable(create, name="openai.responses.create")

    with trace_agent("nested") as trace:
        list(wrapped(stream=True))

    assert len(trace.events) == 1
    assert trace.events[0].input_tokens == 21
    assert trace.events[0].output_tokens == 13


def test_stream_created_without_trace_is_not_recorded_in_later_trace() -> None:
    def create(**kwargs):
        return FakeSyncStream(
            [SimpleNamespace(delta="a", usage=SimpleNamespace(input_tokens=5, output_tokens=5))]
        )

    wrapped = instrument_callable(create, name="openai.responses.create")

    stream = wrapped(stream=True)  # invoked with no active trace
    with trace_agent("later") as trace:
        list(stream)  # consumed inside an unrelated later trace

    assert len(trace.events) == 0


def test_stream_records_into_invocation_trace_not_consumption_trace() -> None:
    def create(**kwargs):
        return FakeSyncStream(
            [SimpleNamespace(delta="a", usage=SimpleNamespace(input_tokens=4, output_tokens=6))]
        )

    wrapped = instrument_callable(create, name="openai.responses.create")

    with trace_agent("A") as trace_a:
        stream = wrapped(stream=True)  # ownership captured for trace A
    with trace_agent("B") as trace_b:
        list(stream)  # consumed under trace B

    assert len(trace_a.events) == 1
    assert trace_a.events[0].input_tokens == 4
    assert len(trace_b.events) == 0


def test_async_stream_records_into_invocation_trace_not_consumption_trace() -> None:
    async def create(**kwargs):
        return FakeAsyncStream(
            [SimpleNamespace(delta="a", usage=SimpleNamespace(input_tokens=7, output_tokens=2))]
        )

    wrapped = instrument_callable(create, name="astream")

    async def run() -> None:
        with trace_agent("A") as trace_a:
            stream = await wrapped(stream=True)
        with trace_agent("B") as trace_b:
            _ = [chunk async for chunk in stream]
        assert len(trace_a.events) == 1
        assert trace_a.events[0].input_tokens == 7
        assert len(trace_b.events) == 0

    asyncio.run(run())
