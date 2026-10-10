"""Trusted offline drivers for existing explicit wrapped-call interfaces."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from typing import Any

from agentloop.harness import BOUNDARIES, Harness, HarnessConfig


class ProtocolGraph:
    """The same four-entrypoint fake protocol used by enforcement conformance."""

    def __init__(self, function):
        self.function = function

    def invoke(self, *args, **kwargs):
        return self.function(*args, **kwargs)

    async def ainvoke(self, *args, **kwargs):
        return await self.function(*args, **kwargs)

    def stream(self, *args, **kwargs):
        return (yield from self.function(*args, **kwargs))

    def astream(self, *args, **kwargs):
        return self.function(*args, **kwargs)


class OfflineDriver:
    """Invoke explicit adapters; never discover or run a vendor transport."""

    def __init__(self, adapter_id: str):
        self.adapter_id = adapter_id

    def unavailable_reason(self) -> str | None:
        if self.adapter_id == "python-wrapped":
            return None
        if self.adapter_id == "langgraph-1.2.11":
            try:
                installed = version("langgraph")
            except PackageNotFoundError:
                installed = None
            return None if installed == "1.2.11" else "pinned_langgraph_runtime_unavailable"
        return "observation_adapter_has_no_runtime_probe_driver"

    def bind(
        self, config: HarnessConfig, function: Any, kind: str, *, boundary="tool", repeated=False
    ):
        if self.adapter_id == "python-wrapped":
            run = Harness(config).start_run("offline-bench")
            wrapped = run.wrap(function, boundary=boundary)
            if repeated:
                repeated_wrapper = run.wrap(wrapped, boundary=boundary)
                if repeated_wrapper is not wrapped:
                    raise ValueError("repeated wrapping changed the wrapper")
            return wrapped, lambda: run
        if self.unavailable_reason() is not None:
            raise RuntimeError("offline adapter unavailable")
        from agentloop.integrations.langgraph_harness import LangGraphHarness

        adapter = LangGraphHarness(config, boundaries=BOUNDARIES)
        # These probes exercise explicit model/tool callable boundaries. They do
        # not pretend a protocol fake is a complete compiled graph scheduler.
        wrapped = getattr(adapter, boundary)(function)
        if repeated and getattr(adapter, boundary)(wrapped) is not wrapped:
            raise ValueError("repeated wrapping changed the wrapper")
        graph = adapter.runnable(ProtocolGraph(wrapped))
        captured = []
        method = {
            "sync": "invoke",
            "async": "ainvoke",
            "generator": "stream",
            "async_generator": "astream",
        }[kind]
        original = getattr(graph, method)
        if kind == "async":

            async def call(*args, **kwargs):
                try:
                    return await original(*args, **kwargs)
                finally:
                    captured.append(adapter.last_run)
        elif kind == "async_generator":

            async def call(*args, **kwargs):
                stream = original(*args, **kwargs)
                try:
                    async for item in stream:
                        yield item
                finally:
                    await stream.aclose()
                    captured.append(adapter.last_run)
        else:
            call = original
        return call, lambda: captured[-1] if captured else adapter.last_run
