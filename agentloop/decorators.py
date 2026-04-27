from __future__ import annotations

import inspect
from functools import wraps
from typing import Any, Callable, TypeVar, overload

from agentloop.tracer import current_trace, trace_agent, trace_model_call, trace_tool_call

F = TypeVar("F", bound=Callable[..., Any])


def _call_metadata(args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, Any]:
    metadata: dict[str, Any] = {"integration": "agentloop.decorator"}
    if args:
        metadata["arg_count"] = len(args)
    if kwargs:
        metadata["kwarg_keys"] = sorted(str(key) for key in kwargs.keys())
    return metadata


@overload
def traceable(fn: F) -> F: ...


@overload
def traceable(
    fn: None = None,
    *,
    name: str | None = None,
    kind: str = "tool",
    model: str | None = None,
    root: bool = False,
    agent_name: str | None = None,
) -> Callable[[F], F]: ...


def traceable(
    fn: F | None = None,
    *,
    name: str | None = None,
    kind: str = "tool",
    model: str | None = None,
    root: bool = False,
    agent_name: str | None = None,
) -> F | Callable[[F], F]:
    """Trace a normal Python function without changing its implementation.

    Examples:

    ```python
    import agentloop

    @agentloop.traceable(root=True, agent_name="research_agent")
    def run_agent(question: str):
        return plan(question)

    @agentloop.traceable(kind="model", model="gpt-4.1-mini")
    def plan(question: str):
        return client.responses.create(...)

    @agentloop.traceable(kind="tool")
    def search_web(query: str):
        return search(query)
    ```

    `root=True` starts an AgentLoop run when no active run exists. Nested calls are
    recorded as spans inside that run. This gives teams a low-friction migration
    path when they do not use LangGraph, CrewAI, or another supported framework.
    """

    normalized_kind = kind.lower().replace("-", "_")
    if normalized_kind not in {"tool", "model"}:
        raise ValueError("kind must be 'tool' or 'model'")

    def decorator(func: F) -> F:
        span_name = name or getattr(func, "__qualname__", getattr(func, "__name__", "agentloop.call"))
        run_name = agent_name or span_name

        if inspect.iscoroutinefunction(func):

            @wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                if root and current_trace() is None:
                    with trace_agent(run_name, metadata={"entrypoint": span_name}):
                        return await _run_async_span(func, span_name, normalized_kind, model, args, kwargs)
                return await _run_async_span(func, span_name, normalized_kind, model, args, kwargs)

            return async_wrapper  # type: ignore[return-value]

        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            if root and current_trace() is None:
                with trace_agent(run_name, metadata={"entrypoint": span_name}):
                    return _run_sync_span(func, span_name, normalized_kind, model, args, kwargs)
            return _run_sync_span(func, span_name, normalized_kind, model, args, kwargs)

        return wrapper  # type: ignore[return-value]

    if fn is not None:
        return decorator(fn)
    return decorator


async def _run_async_span(
    func: Callable[..., Any],
    span_name: str,
    kind: str,
    model: str | None,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> Any:
    metadata = _call_metadata(args, kwargs)
    if kind == "model":
        with trace_model_call(span_name, model=model, metadata=metadata):
            return await func(*args, **kwargs)
    with trace_tool_call(span_name, metadata=metadata):
        return await func(*args, **kwargs)


def _run_sync_span(
    func: Callable[..., Any],
    span_name: str,
    kind: str,
    model: str | None,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> Any:
    metadata = _call_metadata(args, kwargs)
    if kind == "model":
        with trace_model_call(span_name, model=model, metadata=metadata):
            return func(*args, **kwargs)
    with trace_tool_call(span_name, metadata=metadata):
        return func(*args, **kwargs)


def trace_model(*, name: str | None = None, model: str | None = None, root: bool = False, agent_name: str | None = None) -> Callable[[F], F]:
    """Convenience decorator for model-call-like functions."""

    return traceable(name=name, kind="model", model=model, root=root, agent_name=agent_name)


def trace_tool(*, name: str | None = None, root: bool = False, agent_name: str | None = None) -> Callable[[F], F]:
    """Convenience decorator for tool-call-like functions."""

    return traceable(name=name, kind="tool", root=root, agent_name=agent_name)
