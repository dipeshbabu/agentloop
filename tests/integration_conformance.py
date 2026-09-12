"""Internal protocol harness for integration adapters; no framework SDK imports."""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Callable

import pytest

from agentloop.integrations.crewai import instrument_task
from agentloop.integrations.langgraph import instrument_state_graph
from agentloop.integrations.openai import instrument_openai_client

CAPABILITIES = frozenset({"sync", "async", "sync_stream", "async_stream", "model_usage"})


@dataclass(frozen=True)
class AdapterContract:
    name: str
    wrap: Callable[[Callable[..., Any], int], Callable[..., Any]]
    capabilities: dict[str, bool | str]
    without_trace: str
    event_type: str

    def require(self, capability: str) -> None:
        assert set(self.capabilities) == CAPABILITIES
        value = self.capabilities[capability]
        if value is not True:
            assert isinstance(value, str) and value.strip(), "unsupported capability needs a reason"
            pytest.skip(f"{self.name}: {capability} unsupported: {value}")


def _openai(fn, repetitions):
    client = SimpleNamespace(responses=SimpleNamespace(create=fn))
    for _ in range(repetitions):
        assert instrument_openai_client(client) is client
    return client.responses.create


def _langgraph(fn, repetitions):
    class Builder:
        def __init__(self):
            self.nodes = {}

        def add_node(self, name, action):
            self.nodes[name] = action
            return self

    graph = Builder()
    for _ in range(repetitions):
        assert instrument_state_graph(graph) is graph
    graph.add_node("conformance", fn)
    return graph.nodes["conformance"]


def _crewai(fn, repetitions):
    method = "execute_async" if inspect.iscoroutinefunction(fn) else "execute"
    task = SimpleNamespace(name="conformance", **{method: fn})
    for _ in range(repetitions):
        assert instrument_task(task) is task
    return getattr(task, method)


ADAPTERS = (
    AdapterContract(
        "openai-client", _openai, dict.fromkeys(CAPABILITIES, True), "passthrough", "model_call"
    ),
    AdapterContract(
        "langgraph-builder-nodes",
        _langgraph,
        {
            "sync": True,
            "async": True,
            "sync_stream": "builder node wrappers trace function calls, not generator consumption",
            "async_stream": "builder node wrappers do not trace async-generator consumption",
            "model_usage": "node wrappers record tool spans; model usage belongs to a model adapter",
        },
        "requires_active_trace",
        "tool_call",
    ),
    AdapterContract(
        "crewai-tasks",
        _crewai,
        {
            "sync": True,
            "async": True,
            "sync_stream": "task wrappers record method completion, not iterator lifecycles",
            "async_stream": "task wrappers do not trace async-iterator consumption",
            "model_usage": "task wrappers record tool spans; model usage belongs to a model adapter",
        },
        "requires_active_trace",
        "tool_call",
    ),
)


def single_event(trace, contract, *, status="ok"):
    assert len(trace.events) == 1, "one invocation must emit exactly one event"
    event = trace.events[0]
    assert event.event_type == contract.event_type
    assert event.status == status
    return event


class SyncStream:
    def __init__(self, chunks, *, error=None, close_error=None):
        self.chunks = chunks
        self.index = 0
        self.error = error
        self.close_error = close_error
        self.close_count = 0
        self.close_result = object()

    def __iter__(self):
        return self

    def __next__(self):
        if self.error is not None and self.index == 1:
            raise self.error
        if self.index == len(self.chunks):
            raise StopIteration
        chunk = self.chunks[self.index]
        self.index += 1
        return chunk

    def close(self):
        self.close_count += 1
        if self.close_error is not None:
            raise self.close_error
        return self.close_result


class AsyncStream:
    def __init__(self, chunks, *, error=None, close_error=None):
        self.source = SyncStream(chunks, error=error, close_error=close_error)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self.source)
        except StopIteration:
            raise StopAsyncIteration from None

    async def aclose(self):
        return self.source.close()
