from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agentloop.otel import trace_from_otel
from agentloop.otel_ids import to_span_id, to_trace_id
from agentloop.tracer import AgentTrace


class AgentLoopTracingProcessor:
    """OpenAI Agents SDK-compatible tracing processor.

    The OpenAI Agents SDK calls `on_trace_start`, `on_trace_end`, `on_span_start`,
    and `on_span_end` on registered processors. This class avoids importing the SDK
    so AgentLoop can expose the integration without adding a hard dependency.
    """

    def __init__(self, out_dir: str | Path | None = None, *, retain_exported: bool = True):
        self.out_dir = Path(out_dir) if out_dir is not None else None
        # Retain exported AgentTrace objects on ``exported_traces``. Long-lived
        # processors that never read that list can set ``retain_exported=False``
        # so completed traces are not held in memory.
        self.retain_exported = retain_exported
        self._traces: dict[str, Any] = {}
        # Trace ids started but not yet ended, in start order. Used to attribute
        # spans that arrive without a readable trace id to a single open trace.
        self._active_trace_ids: list[str] = []
        # Spans are grouped by their owning trace id and released when that trace
        # ends, so memory does not grow with the number of completed traces.
        self._spans_by_trace: dict[str, list[Any]] = {}
        self.exported_traces: list[AgentTrace] = []

    def on_trace_start(self, trace: Any) -> None:
        trace_id = str(_read(trace, "trace_id", "id") or "trace_unknown")
        self._traces[trace_id] = trace
        if trace_id not in self._active_trace_ids:
            self._active_trace_ids.append(trace_id)

    def on_trace_end(self, trace: Any) -> None:
        trace_id = str(_read(trace, "trace_id", "id") or "trace_unknown")
        if not self._is_known(trace_id):
            # Unknown trace id: already exported (duplicate on_trace_end) or never
            # observed. Nothing to export and nothing to release.
            return

        try:
            agent_trace = self._build_trace(trace, trace_id)
            if self.retain_exported:
                self.exported_traces.append(agent_trace)
            if self.out_dir is not None:
                self.out_dir.mkdir(parents=True, exist_ok=True)
                agent_trace.export_json(self.out_dir / f"{agent_trace.run_id}.json")
        finally:
            # Release state for the completed trace even if build/export fails, so a
            # failed export cannot re-introduce the completed-trace memory leak.
            self._traces.pop(trace_id, None)
            self._spans_by_trace.pop(trace_id, None)
            if trace_id in self._active_trace_ids:
                self._active_trace_ids.remove(trace_id)

    def on_span_start(self, span: Any) -> None:
        return None

    def on_span_end(self, span: Any) -> None:
        trace_id = _span_trace_id(span)
        if trace_id is None:
            # No readable trace id: attribute the span to the most recently started
            # open trace. If none is open, drop it rather than copy it into an
            # unrelated trace that ends later.
            trace_id = self._current_active_trace_id()
            if trace_id is None:
                return
        self._spans_by_trace.setdefault(trace_id, []).append(span)

    def shutdown(self) -> None:
        # Release all retained state; further callbacks start from empty.
        self._traces.clear()
        self._active_trace_ids.clear()
        self._spans_by_trace.clear()

    def force_flush(self) -> None:
        # Spans are exported when their trace ends; nothing is buffered to flush.
        return None

    def _current_active_trace_id(self) -> str | None:
        return self._active_trace_ids[-1] if self._active_trace_ids else None

    def _is_known(self, trace_id: str) -> bool:
        return (
            trace_id in self._traces
            or trace_id in self._spans_by_trace
            or trace_id in self._active_trace_ids
        )

    def _build_trace(self, trace: Any, trace_id: str | None = None) -> AgentTrace:
        if trace_id is None:
            trace_id = str(_read(trace, "trace_id", "id") or "trace_unknown")
        name = str(_read(trace, "name", "workflow_name") or "openai_agents_trace")
        spans = [_span_to_otel(span, trace_id) for span in self._spans_by_trace.get(trace_id, [])]
        return trace_from_otel({"spans": spans}, name=name)


def _span_to_otel(span: Any, trace_id: str) -> dict[str, Any]:
    span_data = _read(span, "span_data") or _read(span, "data") or {}
    span_type = _read(span_data, "type") or span_data.__class__.__name__.lower()
    name = str(_read(span_data, "name") or _read(span, "name") or span_type or "openai_agents_span")
    model = _read(span_data, "model")
    usage = _read(span_data, "usage") or {}
    attrs = [
        _attribute("gen_ai.operation.name", _operation_from_span_type(str(span_type))),
        _attribute("agentloop.event_type", _event_type_from_span_type(str(span_type))),
        _attribute("agentloop.name", name),
    ]
    if model:
        attrs.append(_attribute("gen_ai.request.model", str(model)))
    input_tokens = (
        _read(usage, "input_tokens", "prompt_tokens")
        or _read(span_data, "input_tokens", "prompt_tokens")
        or 0
    )
    output_tokens = (
        _read(usage, "output_tokens", "completion_tokens")
        or _read(span_data, "output_tokens", "completion_tokens")
        or 0
    )
    attrs.append(_attribute("gen_ai.usage.input_tokens", int(input_tokens or 0)))
    attrs.append(_attribute("gen_ai.usage.output_tokens", int(output_tokens or 0)))

    native_span_id = str(_read(span, "span_id", "id") or "0")
    # Preserve the original native ids for diagnosability (see agentloop.otel_ids).
    attrs.append(_attribute("agentloop.native_span_id", native_span_id))
    attrs.append(_attribute("agentloop.native_trace_id", str(trace_id)))
    out = {
        "traceId": to_trace_id(trace_id),
        "spanId": to_span_id(native_span_id),
        "name": name,
        "startTimeUnixNano": str(_time_ns(_read(span, "started_at", "start_time"))),
        "endTimeUnixNano": str(_time_ns(_read(span, "ended_at", "end_time"))),
        "attributes": attrs,
        "status": {"code": "STATUS_CODE_OK"},
    }
    parent_id = _read(span, "parent_id", "parent_span_id")
    if parent_id:
        attrs.append(_attribute("agentloop.native_parent_id", str(parent_id)))
        out["parentSpanId"] = to_span_id(str(parent_id))
    return out


def _read(obj: Any, *names: str) -> Any:
    for name in names:
        if isinstance(obj, dict) and name in obj:
            return obj[name]
        if hasattr(obj, name):
            return getattr(obj, name)
    return None


def _span_trace_id(span: Any) -> str | None:
    value = _read(span, "trace_id")
    return str(value) if value else None


def _operation_from_span_type(span_type: str) -> str:
    lower = span_type.lower()
    if "generation" in lower or "response" in lower or "llm" in lower:
        return "chat"
    if "function" in lower or "tool" in lower:
        return "execute_tool"
    return "invoke_agent"


def _event_type_from_span_type(span_type: str) -> str:
    operation = _operation_from_span_type(span_type)
    return "model_call" if operation == "chat" else "tool_call"


def _attribute(key: str, value: Any) -> dict[str, Any]:
    if isinstance(value, bool):
        encoded = {"boolValue": value}
    elif isinstance(value, int):
        encoded = {"intValue": value}
    elif isinstance(value, float):
        encoded = {"doubleValue": value}
    else:
        encoded = {"stringValue": str(value)}
    return {"key": key, "value": encoded}


def _time_ns(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value * 1_000_000_000)
    try:
        from datetime import datetime

        return int(
            datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp() * 1_000_000_000
        )
    except ValueError:
        return 0


def export_trace_json(trace: AgentTrace, path: str | Path) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(trace.to_dict(), indent=2), encoding="utf-8")
    return out
