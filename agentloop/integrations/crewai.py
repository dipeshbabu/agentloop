from __future__ import annotations

import inspect
import time
from functools import wraps
from typing import Any

from agentloop.events import utc_now_iso
from agentloop.tracer import record_tool_call, trace_agent


def _patch_method(obj: Any, method_name: str, span_name: str, metadata: dict[str, Any]) -> bool:
    method = getattr(obj, method_name, None)
    if method is None or not callable(method):
        return False
    if getattr(method, "_agentloop_wrapped", False):
        return True

    if inspect.iscoroutinefunction(method):

        @wraps(method)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            started_at = utc_now_iso()
            start = time.perf_counter()
            status = "ok"
            error = None
            try:
                return await method(*args, **kwargs)
            except Exception as exc:
                status = "error"
                error = str(exc)
                raise
            finally:
                record_tool_call(
                    span_name,
                    started_at=started_at,
                    duration_ms=(time.perf_counter() - start) * 1000,
                    status=status,
                    error=error,
                    metadata=metadata,
                )

        async_wrapper._agentloop_wrapped = True  # type: ignore[attr-defined]
        setattr(obj, method_name, async_wrapper)
        return True

    @wraps(method)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        started_at = utc_now_iso()
        start = time.perf_counter()
        status = "ok"
        error = None
        try:
            return method(*args, **kwargs)
        except Exception as exc:
            status = "error"
            error = str(exc)
            raise
        finally:
            record_tool_call(
                span_name,
                started_at=started_at,
                duration_ms=(time.perf_counter() - start) * 1000,
                status=status,
                error=error,
                metadata=metadata,
            )

    wrapper._agentloop_wrapped = True  # type: ignore[attr-defined]
    setattr(obj, method_name, wrapper)
    return True


def instrument_crew(crew: Any, *, name: str | None = None) -> Any:
    """Wrap a CrewAI Crew-like object with AgentLoop tracing.

    This patches `.kickoff(...)` and `.kickoff_async(...)` when present. CrewAI is
    optional; the integration accepts any object with those methods.
    """

    run_name = name or getattr(crew, "name", None) or crew.__class__.__name__
    metadata = {"integration": "crewai", "crew_name": str(run_name)}

    kickoff = getattr(crew, "kickoff", None)
    if (
        kickoff is not None
        and callable(kickoff)
        and not getattr(kickoff, "_agentloop_wrapped", False)
    ):

        @wraps(kickoff)
        def kickoff_wrapper(*args: Any, **kwargs: Any) -> Any:
            with trace_agent(str(run_name), metadata=metadata):
                return kickoff(*args, **kwargs)

        kickoff_wrapper._agentloop_wrapped = True  # type: ignore[attr-defined]
        setattr(crew, "kickoff", kickoff_wrapper)

    kickoff_async = getattr(crew, "kickoff_async", None)
    if (
        kickoff_async is not None
        and callable(kickoff_async)
        and not getattr(kickoff_async, "_agentloop_wrapped", False)
    ):

        @wraps(kickoff_async)
        async def kickoff_async_wrapper(*args: Any, **kwargs: Any) -> Any:
            with trace_agent(str(run_name), metadata=metadata):
                return await kickoff_async(*args, **kwargs)

        kickoff_async_wrapper._agentloop_wrapped = True  # type: ignore[attr-defined]
        setattr(crew, "kickoff_async", kickoff_async_wrapper)

    return crew


def instrument_task(task: Any, *, name: str | None = None) -> Any:
    """Patch common CrewAI task execution methods as tool-call spans."""

    task_name = (
        name
        or getattr(task, "description", None)
        or getattr(task, "name", None)
        or task.__class__.__name__
    )
    metadata = {"integration": "crewai", "task_name": str(task_name)}
    for method_name in ("execute", "execute_sync", "execute_async", "run"):
        _patch_method(task, method_name, f"crewai.task.{method_name}", metadata)
    return task


def instrument_agent(agent: Any, *, name: str | None = None) -> Any:
    """Patch common CrewAI agent execution methods as tool-call spans."""

    agent_name = (
        name
        or getattr(agent, "role", None)
        or getattr(agent, "name", None)
        or agent.__class__.__name__
    )
    metadata = {"integration": "crewai", "agent_name": str(agent_name)}
    for method_name in ("execute_task", "run", "invoke"):
        _patch_method(agent, method_name, f"crewai.agent.{method_name}", metadata)
    return agent
