from __future__ import annotations

import json
import time
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Iterator

from agentloop.costs import format_cost_usd
from agentloop.events import (
    AgentEvent,
    format_exception_detail,
    new_event_id,
    new_run_id,
    utc_now_iso,
)
from agentloop.metrics import build_report
from agentloop.schema import SCHEMA_VERSION, validate_trace_dict

_current_trace: ContextVar["AgentTrace | None"] = ContextVar(
    "agentloop_current_trace", default=None
)
_current_event_id: ContextVar[str | None] = ContextVar("agentloop_current_event_id", default=None)


def _count_tokens(text: str | None) -> int:
    if not text:
        return 0
    return max(1, len(text.split()))


class AgentTrace:
    def __init__(
        self,
        name: str,
        run_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        *,
        started_at: str | None = None,
        ended_at: str | None = None,
        elapsed_ms: float | None = None,
    ):
        self.name = name
        self.run_id = run_id or new_run_id()
        self.metadata = metadata or {}
        self.events: list[AgentEvent] = []
        self.started_at = started_at or utc_now_iso()
        self.ended_at = ended_at
        self.elapsed_ms = float(elapsed_ms) if elapsed_ms is not None else None
        self._start_perf = time.perf_counter()
        self._timing_active = False
        self.finalize_result: dict[str, Any] | None = None

    def add_event(self, event: AgentEvent) -> None:
        self.events.append(event)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "name": self.name,
            "run_id": self.run_id,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "elapsed_ms": self.elapsed_ms,
            "metadata": self.metadata,
            "events": [event.to_dict() for event in self.events],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AgentTrace":
        """Deserialize a trace, validating the shared schema contract.

        Raises :class:`agentloop.schema.TraceValidationError` (a ``ValueError``)
        when the envelope or any event violates the schema. Traces without a
        ``schema_version`` (0.4-era files) are accepted for backward
        compatibility.
        """

        validate_trace_dict(data)
        trace = cls(
            name=data["name"],
            run_id=data["run_id"],
            metadata=data.get("metadata", {}),
            started_at=data.get("started_at"),
            ended_at=data.get("ended_at"),
            elapsed_ms=data.get("elapsed_ms"),
        )
        trace.events = [
            AgentEvent.from_dict(item, index=index)
            for index, item in enumerate(data.get("events", []))
        ]
        return trace

    def finish(self) -> None:
        """Finalize trace timing once, using a monotonic clock when available."""

        if self.ended_at is not None and self.elapsed_ms is not None:
            self._timing_active = False
            return

        if self.ended_at is None:
            self.ended_at = utc_now_iso()
        if self.elapsed_ms is None:
            if self._timing_active:
                self.elapsed_ms = max(0.0, (time.perf_counter() - self._start_perf) * 1000)
            else:
                from agentloop.timing import elapsed_runtime_ms

                self.elapsed_ms = elapsed_runtime_ms(self)
        self._timing_active = False

    @classmethod
    def from_json(cls, path: str | Path) -> "AgentTrace":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    def export_json(self, path: str | Path) -> Path:
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        return out

    def report(self) -> dict[str, Any]:
        return build_report(self)

    def print_report(self) -> None:
        report = self.report()
        print(f"AgentLoop Report: {self.name}")
        print(f"Run ID: {self.run_id}")
        print(f"Total runtime: {report['total_runtime_ms'] / 1000:.2f}s")
        print(
            "Estimated cost: "
            + format_cost_usd(
                report.get("estimated_cost_usd"), report.get("cost_status", "complete")
            )
        )
        print(f"Model time: {report['model_time_ms'] / 1000:.2f}s")
        print(f"Tool time: {report['tool_time_ms'] / 1000:.2f}s")
        print(f"Retry time: {report['retry_time_ms'] / 1000:.2f}s")
        print(f"Input tokens: {report['input_tokens']}")
        print(f"Output tokens: {report['output_tokens']}")
        print(f"Repeated context ratio: {report['repeated_context_ratio']:.1%}")
        print("\nRecommendations:")
        for rec in report["recommendations"]:
            print(f"- {rec['title']}: {rec['description']}")


def current_trace() -> AgentTrace | None:
    """Return the active AgentLoop trace, if one is running in this context."""

    return _current_trace.get()


def current_event_id() -> str | None:
    """Return the event id currently being recorded under, if any."""

    return _current_event_id.get()


@contextmanager
def trace_agent(name: str, metadata: dict[str, Any] | None = None) -> Iterator[AgentTrace]:
    trace = AgentTrace(name=name, metadata=metadata)
    trace._timing_active = True
    token = _current_trace.set(trace)
    event_token = _current_event_id.set(None)
    try:
        yield trace
    finally:
        trace.finish()
        _current_event_id.reset(event_token)
        _current_trace.reset(token)
        from agentloop.runtime import finalize_trace, should_auto_export

        if should_auto_export():
            trace.finalize_result = finalize_trace(trace)


def record_model_call(
    name: str,
    *,
    duration_ms: float,
    started_at: str,
    ended_at: str | None = None,
    model: str | None = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    input_text: str | None = None,
    output_text: str | None = None,
    status: str = "ok",
    error: str | None = None,
    metadata: dict[str, Any] | None = None,
    event_id: str | None = None,
    parent_id: str | None = None,
    trace: AgentTrace | None = None,
) -> None:
    """Record a completed model call into a trace.

    Integrations use this when token counts are only known after a framework call
    returns. User code should usually prefer `trace_model_call(...)`.

    By default the event is recorded into the trace active in the current context.
    Pass ``trace`` to record into a specific trace instead — for example an
    integration that captured trace ownership when a streaming call was invoked and
    finalizes it later, possibly in a different context. When ``trace`` is given the
    ambient event-id context is not used for the parent.
    """

    target = trace if trace is not None else _require_trace()
    resolved_parent = (
        parent_id
        if parent_id is not None
        else (None if trace is not None else _current_event_id.get())
    )
    target.add_event(
        AgentEvent(
            event_id=event_id or new_event_id(),
            run_id=target.run_id,
            event_type="model_call",
            name=name,
            started_at=started_at,
            ended_at=ended_at or utc_now_iso(),
            duration_ms=duration_ms,
            parent_id=resolved_parent,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            input_text=input_text,
            output_text=output_text,
            status=status,
            error=error,
            metadata=metadata or {},
        )
    )


def record_tool_call(
    name: str,
    *,
    duration_ms: float,
    started_at: str,
    ended_at: str | None = None,
    status: str = "ok",
    error: str | None = None,
    metadata: dict[str, Any] | None = None,
    event_id: str | None = None,
    parent_id: str | None = None,
) -> None:
    """Record a completed tool/framework step into the active trace."""

    trace = _require_trace()
    trace.add_event(
        AgentEvent(
            event_id=event_id or new_event_id(),
            run_id=trace.run_id,
            event_type="tool_call",
            name=name,
            started_at=started_at,
            ended_at=ended_at or utc_now_iso(),
            duration_ms=duration_ms,
            parent_id=parent_id if parent_id is not None else _current_event_id.get(),
            status=status,
            error=error,
            metadata=metadata or {},
        )
    )


@contextmanager
def trace_model_call(
    name: str,
    model: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    input_text: str | None = None,
    output_text: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> Iterator[None]:
    _require_trace()
    event_id = new_event_id()
    parent_id = _current_event_id.get()
    event_token = _current_event_id.set(event_id)
    started_at = utc_now_iso()
    start = time.perf_counter()
    status = "ok"
    error = None
    try:
        yield
    except BaseException as exc:  # noqa: BLE001 - record then propagate unchanged
        status = "error"
        error = format_exception_detail(exc)
        raise
    finally:
        ended_at = utc_now_iso()
        duration_ms = (time.perf_counter() - start) * 1000
        _current_event_id.reset(event_token)
        record_model_call(
            name,
            started_at=started_at,
            ended_at=ended_at,
            duration_ms=duration_ms,
            model=model,
            input_tokens=input_tokens if input_tokens is not None else _count_tokens(input_text),
            output_tokens=output_tokens
            if output_tokens is not None
            else _count_tokens(output_text),
            input_text=input_text,
            output_text=output_text,
            status=status,
            error=error,
            metadata=metadata or {},
            event_id=event_id,
            parent_id=parent_id,
        )


@contextmanager
def trace_tool_call(name: str, metadata: dict[str, Any] | None = None) -> Iterator[None]:
    _require_trace()
    event_id = new_event_id()
    parent_id = _current_event_id.get()
    event_token = _current_event_id.set(event_id)
    started_at = utc_now_iso()
    start = time.perf_counter()
    status = "ok"
    error = None
    try:
        yield
    except BaseException as exc:  # noqa: BLE001 - record then propagate unchanged
        status = "error"
        error = format_exception_detail(exc)
        raise
    finally:
        ended_at = utc_now_iso()
        duration_ms = (time.perf_counter() - start) * 1000
        _current_event_id.reset(event_token)
        record_tool_call(
            name,
            started_at=started_at,
            ended_at=ended_at,
            duration_ms=duration_ms,
            status=status,
            error=error,
            metadata=metadata or {},
            event_id=event_id,
            parent_id=parent_id,
        )


@contextmanager
def trace_retry(name: str, metadata: dict[str, Any] | None = None) -> Iterator[None]:
    trace = _require_trace()
    event_id = new_event_id()
    parent_id = _current_event_id.get()
    event_token = _current_event_id.set(event_id)
    started_at = utc_now_iso()
    start = time.perf_counter()
    try:
        yield
    finally:
        ended_at = utc_now_iso()
        duration_ms = (time.perf_counter() - start) * 1000
        _current_event_id.reset(event_token)
        trace.add_event(
            AgentEvent(
                event_id=event_id,
                run_id=trace.run_id,
                event_type="retry",
                name=name,
                started_at=started_at,
                ended_at=ended_at,
                duration_ms=duration_ms,
                parent_id=parent_id,
                metadata=metadata or {},
            )
        )


def _require_trace() -> AgentTrace:
    trace = _current_trace.get()
    if trace is None:
        raise RuntimeError("No active AgentLoop trace. Use `with trace_agent(...):` first.")
    return trace
