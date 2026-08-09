from __future__ import annotations

import inspect
import time
from collections.abc import AsyncGenerator, Callable, Generator, Iterator
from contextlib import contextmanager
from functools import wraps
from typing import Any, TypeVar, overload

from agentloop.events import format_exception_detail, new_event_id, utc_now_iso
from agentloop.tracer import (
    AgentTrace,
    _current_event_id,
    _current_trace,
    current_event_id,
    current_trace,
    record_model_call,
    record_tool_call,
    trace_agent,
    trace_model_call,
    trace_tool_call,
)

F = TypeVar("F", bound=Callable[..., Any])


def _call_metadata(args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, Any]:
    metadata: dict[str, Any] = {"integration": "agentloop.decorator"}
    if args:
        metadata["arg_count"] = len(args)
    if kwargs:
        metadata["kwarg_keys"] = sorted(str(key) for key in kwargs.keys())
    return metadata


@contextmanager
def _activate_trace_context(trace: AgentTrace, event_id: str | None) -> Iterator[None]:
    """Bind a captured trace/span only while a generator operation is executing."""

    trace_token = _current_trace.set(trace)
    event_token = _current_event_id.set(event_id)
    try:
        yield
    finally:
        _current_event_id.reset(event_token)
        _current_trace.reset(trace_token)


def _finalize_owned_trace(trace: AgentTrace) -> None:
    """Finish and optionally export a root trace created for a generator."""

    trace.finish()
    from agentloop.runtime import finalize_trace, should_auto_export

    if should_auto_export():
        trace.finalize_result = finalize_trace(trace)


class _GeneratorSpan:
    """One trace event whose context is activated separately for every resume."""

    def __init__(
        self,
        *,
        span_name: str,
        run_name: str,
        kind: str,
        model: str | None,
        root: bool,
        metadata: dict[str, Any],
    ) -> None:
        self._span_name = span_name
        self._run_name = run_name
        self._kind = kind
        self._model = model
        self._metadata = metadata
        self._trace = current_trace()
        self._parent_id = current_event_id()
        self._owns_trace = root and self._trace is None
        if self._trace is None and not self._owns_trace:
            raise RuntimeError("No active AgentLoop trace. Use `with trace_agent(...):` first.")

        self._event_id: str | None = None
        self._started_at: str | None = None
        self._start_perf: float | None = None
        self._started = False
        self._done = False

    @property
    def done(self) -> bool:
        return self._done

    @contextmanager
    def activate(self) -> Iterator[None]:
        if self._done:
            raise RuntimeError("Generator trace span is already finalized.")
        self._start()
        trace, event_id, _, _ = self._started_state()
        with _activate_trace_context(trace, event_id):
            yield

    def finish(self) -> None:
        self._finalize(status="ok", error=None)

    def fail(self, exc: BaseException) -> None:
        self._finalize(status="error", error=format_exception_detail(exc))

    def _start(self) -> None:
        if self._started:
            return
        if self._trace is None:
            self._trace = AgentTrace(
                name=self._run_name,
                metadata={"entrypoint": self._span_name},
            )
            self._trace._timing_active = True
        self._event_id = new_event_id()
        self._started_at = utc_now_iso()
        self._start_perf = time.perf_counter()
        self._started = True

    def _started_state(self) -> tuple[AgentTrace, str, str, float]:
        trace = self._trace
        event_id = self._event_id
        started_at = self._started_at
        start_perf = self._start_perf
        if trace is None or event_id is None or started_at is None or start_perf is None:
            raise RuntimeError("Generator trace span failed to initialize.")
        return trace, event_id, started_at, start_perf

    def _finalize(self, *, status: str, error: str | None) -> None:
        if self._done:
            return
        self._done = True
        if not self._started:
            return

        trace, event_id, started_at, start_perf = self._started_state()
        ended_at = utc_now_iso()
        duration_ms = max(0.0, (time.perf_counter() - start_perf) * 1000)
        try:
            # Bind the captured parent while adding the completed event. Binding the
            # generator's own event id here would accidentally make a root span its own parent.
            with _activate_trace_context(trace, self._parent_id):
                if self._kind == "model":
                    record_model_call(
                        self._span_name,
                        started_at=started_at,
                        ended_at=ended_at,
                        duration_ms=duration_ms,
                        model=self._model,
                        status=status,
                        error=error,
                        metadata=self._metadata,
                        event_id=event_id,
                        parent_id=self._parent_id,
                    )
                else:
                    record_tool_call(
                        self._span_name,
                        started_at=started_at,
                        ended_at=ended_at,
                        duration_ms=duration_ms,
                        status=status,
                        error=error,
                        metadata=self._metadata,
                        event_id=event_id,
                        parent_id=self._parent_id,
                    )
        finally:
            if self._owns_trace:
                _finalize_owned_trace(trace)


def _resume_generator(
    generator: Generator[Any, Any, Any],
    span: _GeneratorSpan,
    operation: Callable[..., Any],
    *args: Any,
) -> tuple[bool, Any]:
    try:
        with span.activate():
            return False, operation(*args)
    except StopIteration as stop:
        span.finish()
        return True, stop.value
    except BaseException as exc:  # noqa: BLE001 - record then propagate unchanged
        span.fail(exc)
        raise


def _close_generator(generator: Generator[Any, Any, Any], span: _GeneratorSpan) -> None:
    if span.done:
        generator.close()
        return
    try:
        with span.activate():
            generator.close()
    except BaseException as exc:  # noqa: BLE001 - record then propagate unchanged
        span.fail(exc)
        raise
    span.finish()


def _delegate_generator(
    generator: Generator[Any, Any, Any],
    span: _GeneratorSpan,
) -> Generator[Any, Any, Any]:
    done, value = _resume_generator(generator, span, next, generator)
    if done:
        return value

    while True:
        try:
            sent = yield value
        except GeneratorExit:
            _close_generator(generator, span)
            raise
        except BaseException as exc:  # noqa: BLE001 - forward caller exceptions unchanged
            done, value = _resume_generator(generator, span, generator.throw, exc)
        else:
            done, value = _resume_generator(generator, span, generator.send, sent)
        if done:
            return value


async def _resume_async_generator(
    generator: AsyncGenerator[Any, Any],
    span: _GeneratorSpan,
    operation: Callable[..., Any],
    *args: Any,
) -> tuple[bool, Any]:
    try:
        with span.activate():
            return False, await operation(*args)
    except StopAsyncIteration:
        span.finish()
        return True, None
    except BaseException as exc:  # noqa: BLE001 - record then propagate unchanged
        span.fail(exc)
        raise


async def _close_async_generator(
    generator: AsyncGenerator[Any, Any],
    span: _GeneratorSpan,
) -> None:
    if span.done:
        await generator.aclose()
        return
    try:
        with span.activate():
            await generator.aclose()
    except BaseException as exc:  # noqa: BLE001 - record then propagate unchanged
        span.fail(exc)
        raise
    span.finish()


async def _delegate_async_generator(
    generator: AsyncGenerator[Any, Any],
    span: _GeneratorSpan,
) -> AsyncGenerator[Any, Any]:
    done, value = await _resume_async_generator(generator, span, generator.__anext__)
    if done:
        return

    while True:
        try:
            sent = yield value
        except GeneratorExit:
            await _close_async_generator(generator, span)
            return
        except BaseException as exc:  # noqa: BLE001 - forward caller exceptions unchanged
            done, value = await _resume_async_generator(generator, span, generator.athrow, exc)
        else:
            done, value = await _resume_async_generator(generator, span, generator.asend, sent)
        if done:
            return


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
        span_name = name or getattr(
            func, "__name__", getattr(func, "__qualname__", "agentloop.call")
        )
        run_name = agent_name or span_name

        if inspect.isasyncgenfunction(func):

            @wraps(func)
            def async_gen_wrapper(*args: Any, **kwargs: Any) -> Any:
                span = _GeneratorSpan(
                    span_name=span_name,
                    run_name=run_name,
                    kind=normalized_kind,
                    model=model,
                    root=root,
                    metadata=_call_metadata(args, kwargs),
                )
                return _delegate_async_generator(func(*args, **kwargs), span)

            return async_gen_wrapper  # type: ignore[return-value]

        if inspect.iscoroutinefunction(func):

            @wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                if root and current_trace() is None:
                    with trace_agent(run_name, metadata={"entrypoint": span_name}):
                        return await _run_async_span(
                            func, span_name, normalized_kind, model, args, kwargs
                        )
                return await _run_async_span(func, span_name, normalized_kind, model, args, kwargs)

            return async_wrapper  # type: ignore[return-value]

        if inspect.isgeneratorfunction(func):

            @wraps(func)
            def gen_wrapper(*args: Any, **kwargs: Any) -> Any:
                span = _GeneratorSpan(
                    span_name=span_name,
                    run_name=run_name,
                    kind=normalized_kind,
                    model=model,
                    root=root,
                    metadata=_call_metadata(args, kwargs),
                )
                return _delegate_generator(func(*args, **kwargs), span)

            return gen_wrapper  # type: ignore[return-value]

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


def _span_cm(
    span_name: str,
    kind: str,
    model: str | None,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> Any:
    """Return the span context manager for a regular model or tool call."""

    metadata = _call_metadata(args, kwargs)
    if kind == "model":
        return trace_model_call(span_name, model=model, metadata=metadata)
    return trace_tool_call(span_name, metadata=metadata)


async def _run_async_span(
    func: Callable[..., Any],
    span_name: str,
    kind: str,
    model: str | None,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> Any:
    with _span_cm(span_name, kind, model, args, kwargs):
        return await func(*args, **kwargs)


def _run_sync_span(
    func: Callable[..., Any],
    span_name: str,
    kind: str,
    model: str | None,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> Any:
    with _span_cm(span_name, kind, model, args, kwargs):
        return func(*args, **kwargs)


def trace_model(
    *,
    name: str | None = None,
    model: str | None = None,
    root: bool = False,
    agent_name: str | None = None,
) -> Callable[[F], F]:
    """Convenience decorator for model-call-like functions."""

    return traceable(name=name, kind="model", model=model, root=root, agent_name=agent_name)


def trace_tool(
    *, name: str | None = None, root: bool = False, agent_name: str | None = None
) -> Callable[[F], F]:
    """Convenience decorator for tool-call-like functions."""

    return traceable(name=name, kind="tool", root=root, agent_name=agent_name)
