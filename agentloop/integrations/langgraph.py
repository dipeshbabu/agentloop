from __future__ import annotations

import inspect
from collections.abc import Callable
from functools import wraps
from typing import Any, TypeVar

from agentloop import trace_agent, trace_tool_call

F = TypeVar("F", bound=Callable[..., Any])


def _merge_metadata(metadata: dict[str, Any] | None, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    merged = {"integration": "langgraph"}
    if metadata:
        merged.update(metadata)
    if extra:
        merged.update(extra)
    return merged


def trace_node(name: str | None = None, metadata: dict[str, Any] | None = None) -> Callable[[F], F]:
    """Wrap a LangGraph node function as an AgentLoop node span.

    This is dependency-free: it works for normal callables, async callables, or
    LangGraph node functions without importing LangGraph inside AgentLoop.
    """

    def decorator(fn: F) -> F:
        step_name = name or getattr(fn, "__name__", "langgraph_node")
        step_metadata = _merge_metadata(metadata, {"node_name": step_name})

        if inspect.iscoroutinefunction(fn):

            @wraps(fn)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                with trace_tool_call(step_name, metadata=step_metadata):
                    return await fn(*args, **kwargs)

            return async_wrapper  # type: ignore[return-value]

        @wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            with trace_tool_call(step_name, metadata=step_metadata):
                return fn(*args, **kwargs)

        return wrapper  # type: ignore[return-value]

    return decorator


def instrument_state_graph(graph: Any, metadata: dict[str, Any] | None = None) -> Any:
    """Monkey-patch a LangGraph StateGraph-like builder so add_node auto-wraps nodes.

    Usage:
        builder = StateGraph(State)
        instrument_state_graph(builder)
        builder.add_node("retrieve", retrieve_fn)  # automatically traced

    The function intentionally accepts `Any` so AgentLoop does not need LangGraph
    as a dependency. It expects the object to expose an `add_node(name, action, ...)`
    method, which matches LangGraph's builder style.
    """

    if getattr(graph, "_agentloop_instrumented", False):
        return graph
    original_add_node = getattr(graph, "add_node", None)
    if original_add_node is None or not callable(original_add_node):
        raise TypeError("Expected a LangGraph-like object with callable add_node(...).")

    @wraps(original_add_node)
    def add_node(name: str, action: Any = None, *args: Any, **kwargs: Any) -> Any:
        wrapped_action = action
        if callable(action):
            wrapped_action = trace_node(str(name), _merge_metadata(metadata, {"auto_instrumented": True}))(action)
        return original_add_node(name, wrapped_action, *args, **kwargs)

    setattr(graph, "add_node", add_node)
    setattr(graph, "_agentloop_instrumented", True)
    return graph


class TracedRunnable:
    """Small wrapper for compiled LangGraph apps/runnables.

    It creates an AgentLoop trace around `.invoke(...)`, `.ainvoke(...)`, `.stream(...)`,
    and `.astream(...)` while delegating every other attribute to the wrapped app.
    """

    def __init__(self, app: Any, name: str = "langgraph_app", metadata: dict[str, Any] | None = None):
        self.app = app
        self.name = name
        self.metadata = _merge_metadata(metadata, {"runnable": True})
        self.last_trace = None

    def __getattr__(self, item: str) -> Any:
        return getattr(self.app, item)

    def invoke(self, *args: Any, **kwargs: Any) -> Any:
        with trace_agent(self.name, metadata=self.metadata) as trace:
            result = self.app.invoke(*args, **kwargs)
        self.last_trace = trace
        return result

    async def ainvoke(self, *args: Any, **kwargs: Any) -> Any:
        with trace_agent(self.name, metadata=self.metadata) as trace:
            result = await self.app.ainvoke(*args, **kwargs)
        self.last_trace = trace
        return result

    def stream(self, *args: Any, **kwargs: Any) -> Any:
        with trace_agent(self.name, metadata=self.metadata) as trace:
            for item in self.app.stream(*args, **kwargs):
                yield item
        self.last_trace = trace

    async def astream(self, *args: Any, **kwargs: Any) -> Any:
        with trace_agent(self.name, metadata=self.metadata) as trace:
            async for item in self.app.astream(*args, **kwargs):
                yield item
        self.last_trace = trace

    def export_last_trace(self, path: str) -> None:
        if self.last_trace is None:
            raise RuntimeError("No trace recorded yet. Call invoke/ainvoke/stream/astream first.")
        self.last_trace.export_json(path)


def trace_runnable(app: Any, name: str = "langgraph_app", metadata: dict[str, Any] | None = None) -> TracedRunnable:
    """Wrap a compiled LangGraph app/runnable with AgentLoop tracing."""

    return TracedRunnable(app=app, name=name, metadata=metadata)
