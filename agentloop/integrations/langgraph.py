from __future__ import annotations

from collections.abc import Callable
from functools import wraps
from typing import Any, TypeVar

from agentloop import trace_tool_call

F = TypeVar("F", bound=Callable[..., Any])


def trace_node(name: str | None = None, metadata: dict[str, Any] | None = None) -> Callable[[F], F]:
    """Wrap a LangGraph node function as an AgentLoop tool-like step.

    This integration is deliberately dependency-free. Users can decorate any node
    function without importing LangGraph inside AgentLoop.
    """

    def decorator(fn: F) -> F:
        step_name = name or fn.__name__

        @wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            with trace_tool_call(step_name, metadata=metadata or {"integration": "langgraph"}):
                return fn(*args, **kwargs)

        return wrapper  # type: ignore[return-value]

    return decorator
