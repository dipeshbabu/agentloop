"""Shared dispatch contract fixtures, separate from tracing conformance."""

from dataclasses import dataclass

from agentloop.harness import Harness
from agentloop.integrations.langgraph_harness import LangGraphHarness


class ProtocolGraph:
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


class ProtocolSignalError(Exception):
    def __init__(self, original):
        self.original = original


@dataclass
class EnforcementContract:
    name: str

    def bind(self, config, function, kind, *, boundary="model", **kwargs):
        if self.name == "python":
            run = Harness(config).start_run()
            return run.wrap(function, boundary=boundary, **kwargs), lambda: run
        adapter = LangGraphHarness(config, boundaries={boundary, "completion"})
        protected = getattr(adapter, boundary)(function, **kwargs)
        graph = adapter.runnable(ProtocolGraph(protected))
        method = {
            "sync": "invoke",
            "async": "ainvoke",
            "generator": "stream",
            "async_generator": "astream",
        }[kind]
        return getattr(graph, method), lambda: adapter.last_run
