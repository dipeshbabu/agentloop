from __future__ import annotations

import asyncio
from types import SimpleNamespace

from agentloop.integrations.openai import instrument_callable, instrument_openai_client
from agentloop.tracer import trace_agent


class FakeResponses:
    def create(self, **kwargs):
        return SimpleNamespace(usage=SimpleNamespace(input_tokens=10, output_tokens=5), kwargs=kwargs)


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
        client.chat.completions.create(model="gpt-chat", messages=[{"role": "user", "content": "hi"}])
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
