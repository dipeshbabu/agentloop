from __future__ import annotations

import json
import time
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Iterator

from agentloop.events import AgentEvent, new_event_id, new_run_id, utc_now_iso
from agentloop.metrics import build_report

_current_trace: ContextVar["AgentTrace | None"] = ContextVar(
    "agentloop_current_trace", default=None
)


def _count_tokens(text: str | None) -> int:
    if not text:
        return 0
    return max(1, len(text.split()))


class AgentTrace:
    def __init__(
        self, name: str, run_id: str | None = None, metadata: dict[str, Any] | None = None
    ):
        self.name = name
        self.run_id = run_id or new_run_id()
        self.metadata = metadata or {}
        self.events: list[AgentEvent] = []
        self.started_at = utc_now_iso()
        self._start_perf = time.perf_counter()
        self.finalize_result: dict[str, Any] | None = None

    def add_event(self, event: AgentEvent) -> None:
        self.events.append(event)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "run_id": self.run_id,
            "started_at": self.started_at,
            "metadata": self.metadata,
            "events": [event.to_dict() for event in self.events],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AgentTrace":
        trace = cls(name=data["name"], run_id=data["run_id"], metadata=data.get("metadata", {}))
        trace.started_at = data.get("started_at", trace.started_at)
        trace.events = [AgentEvent.from_dict(item) for item in data.get("events", [])]
        return trace

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
        print(f"Estimated cost: ${report['estimated_cost_usd']:.4f}")
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


@contextmanager
def trace_agent(name: str, metadata: dict[str, Any] | None = None) -> Iterator[AgentTrace]:
    trace = AgentTrace(name=name, metadata=metadata)
    token = _current_trace.set(trace)
    try:
        yield trace
    finally:
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
) -> None:
    """Record a completed model call into the active trace.

    Integrations use this when token counts are only known after a framework call
    returns. User code should usually prefer `trace_model_call(...)`.
    """

    trace = _require_trace()
    trace.add_event(
        AgentEvent(
            event_id=new_event_id(),
            run_id=trace.run_id,
            event_type="model_call",
            name=name,
            started_at=started_at,
            ended_at=ended_at or utc_now_iso(),
            duration_ms=duration_ms,
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
) -> None:
    """Record a completed tool/framework step into the active trace."""

    trace = _require_trace()
    trace.add_event(
        AgentEvent(
            event_id=new_event_id(),
            run_id=trace.run_id,
            event_type="tool_call",
            name=name,
            started_at=started_at,
            ended_at=ended_at or utc_now_iso(),
            duration_ms=duration_ms,
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
    started_at = utc_now_iso()
    start = time.perf_counter()
    status = "ok"
    error = None
    try:
        yield
    except Exception as exc:
        status = "error"
        error = str(exc)
        raise
    finally:
        record_model_call(
            name,
            started_at=started_at,
            duration_ms=(time.perf_counter() - start) * 1000,
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
        )


@contextmanager
def trace_tool_call(name: str, metadata: dict[str, Any] | None = None) -> Iterator[None]:
    _require_trace()
    started_at = utc_now_iso()
    start = time.perf_counter()
    status = "ok"
    error = None
    try:
        yield
    except Exception as exc:
        status = "error"
        error = str(exc)
        raise
    finally:
        record_tool_call(
            name,
            started_at=started_at,
            duration_ms=(time.perf_counter() - start) * 1000,
            status=status,
            error=error,
            metadata=metadata or {},
        )


@contextmanager
def trace_retry(name: str, metadata: dict[str, Any] | None = None) -> Iterator[None]:
    trace = _require_trace()
    started_at = utc_now_iso()
    start = time.perf_counter()
    try:
        yield
    finally:
        trace.add_event(
            AgentEvent(
                event_id=new_event_id(),
                run_id=trace.run_id,
                event_type="retry",
                name=name,
                started_at=started_at,
                ended_at=utc_now_iso(),
                duration_ms=(time.perf_counter() - start) * 1000,
                metadata=metadata or {},
            )
        )


def _require_trace() -> AgentTrace:
    trace = _current_trace.get()
    if trace is None:
        raise RuntimeError("No active AgentLoop trace. Use `with trace_agent(...):` first.")
    return trace
