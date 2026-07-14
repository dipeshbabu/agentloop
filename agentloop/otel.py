from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from agentloop.events import AgentEvent
from agentloop.tracer import AgentTrace
from agentloop.version import __version__

_NANOSECONDS_PER_MILLISECOND = 1_000_000


def trace_from_otel(
    payload: dict[str, Any] | list[dict[str, Any]], name: str | None = None
) -> AgentTrace:
    """Convert OTLP/GenAI-style JSON spans into an AgentLoop trace.

    This accepts the standard OTLP JSON shape (`resourceSpans -> scopeSpans -> spans`),
    a dictionary with a top-level `spans` list, or a raw list of span dictionaries.
    """

    spans = list(_iter_spans(payload))
    run_id = _run_id_from_spans(spans)
    trace = AgentTrace(name=name or _trace_name(spans), run_id=run_id, metadata={"source": "otel"})
    for span in spans:
        trace.add_event(_event_from_span(span, run_id))
    bounds = _trace_bounds_ns(spans)
    if bounds is not None:
        started_ns, ended_ns = bounds
        trace.started_at = _iso_from_ns(started_ns)
        trace.ended_at = _iso_from_ns(ended_ns)
        trace.elapsed_ms = (ended_ns - started_ns) / _NANOSECONDS_PER_MILLISECOND
    return trace


def trace_to_otel(trace: AgentTrace) -> dict[str, Any]:
    """Export an AgentLoop trace as dependency-free OTLP-like GenAI JSON."""

    spans = [_span_from_event(trace, event) for event in trace.events]
    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        _attribute("service.name", "agentloop"),
                        _attribute("agentloop.trace.name", trace.name),
                        _attribute("agentloop.run_id", trace.run_id),
                    ]
                },
                "scopeSpans": [
                    {
                        "scope": {"name": "agentloop", "version": __version__},
                        "spans": spans,
                    }
                ],
            }
        ]
    }


def _iter_spans(payload: dict[str, Any] | list[dict[str, Any]]) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return payload
    if "spans" in payload and isinstance(payload["spans"], list):
        return payload["spans"]

    spans: list[dict[str, Any]] = []
    for resource_span in payload.get("resourceSpans", []):
        for scope_span in resource_span.get("scopeSpans", []):
            spans.extend(scope_span.get("spans", []))
    return spans


def _run_id_from_spans(spans: list[dict[str, Any]]) -> str:
    for span in spans:
        trace_id = str(span.get("traceId") or span.get("trace_id") or "")
        if trace_id:
            return "run_" + trace_id[-16:].lower()
    return "run_otel_import"


def _trace_name(spans: list[dict[str, Any]]) -> str:
    for span in spans:
        attrs = _attributes(span)
        workflow = attrs.get("gen_ai.workflow.name") or attrs.get("agentloop.trace.name")
        if workflow:
            return str(workflow)
    return "otel_trace"


def _event_from_span(span: dict[str, Any], run_id: str) -> AgentEvent:
    attrs = _attributes(span)
    operation = str(attrs.get("gen_ai.operation.name") or attrs.get("agentloop.event_type") or "")
    event_type = _event_type(operation, span.get("name"))
    started_ns = _int_or_none(span.get("startTimeUnixNano") or span.get("start_time_unix_nano"))
    ended_ns = _int_or_none(span.get("endTimeUnixNano") or span.get("end_time_unix_nano"))
    duration_ms = _duration_ms(started_ns, ended_ns, span)
    span_id = str(span.get("spanId") or span.get("span_id") or "")
    parent_id = str(span.get("parentSpanId") or span.get("parent_span_id") or "") or None
    status = _status(span)
    error = _error(span)

    metadata = {
        key: value
        for key, value in attrs.items()
        if not key.startswith("gen_ai.usage.") and key not in _direct_attribute_keys()
    }
    metadata["otel_span_id"] = span_id
    metadata["otel_trace_id"] = span.get("traceId") or span.get("trace_id")

    return AgentEvent(
        event_id="span_" + span_id if span_id else "",
        run_id=run_id,
        event_type=event_type,
        name=str(
            attrs.get("gen_ai.tool.name")
            or attrs.get("agentloop.name")
            or span.get("name")
            or event_type
        ),
        started_at=_iso_from_ns(started_ns),
        ended_at=_iso_from_ns(ended_ns),
        duration_ms=duration_ms,
        parent_id="span_" + parent_id if parent_id else None,
        model=_first_string(attrs, "gen_ai.request.model", "gen_ai.response.model"),
        input_tokens=int(
            attrs.get("gen_ai.usage.input_tokens") or attrs.get("llm.usage.prompt_tokens") or 0
        ),
        output_tokens=int(
            attrs.get("gen_ai.usage.output_tokens") or attrs.get("llm.usage.completion_tokens") or 0
        ),
        status=status,
        error=error,
        metadata=metadata,
    )


def _span_from_event(trace: AgentTrace, event: AgentEvent) -> dict[str, Any]:
    start_ns = _ns_from_iso(event.started_at)
    end_ns = _ns_from_iso(event.ended_at)
    attrs = [
        _attribute("gen_ai.operation.name", _operation_name(event.event_type)),
        _attribute("agentloop.event_type", event.event_type),
        _attribute("agentloop.name", event.name),
        _attribute("agentloop.run_id", trace.run_id),
        _attribute("gen_ai.usage.input_tokens", event.input_tokens),
        _attribute("gen_ai.usage.output_tokens", event.output_tokens),
    ]
    if event.model:
        attrs.append(_attribute("gen_ai.request.model", event.model))
    for key, value in sorted((event.metadata or {}).items()):
        if isinstance(value, str | int | float | bool):
            attrs.append(_attribute(f"agentloop.metadata.{key}", value))

    span = {
        "traceId": trace.run_id.replace("run_", "").rjust(32, "0")[-32:],
        "spanId": event.event_id.replace("span_", "").replace("evt_", "").rjust(16, "0")[-16:],
        "name": event.name,
        "kind": "SPAN_KIND_INTERNAL",
        "startTimeUnixNano": str(start_ns),
        "endTimeUnixNano": str(end_ns),
        "attributes": attrs,
        "status": {"code": "STATUS_CODE_OK" if event.status == "ok" else "STATUS_CODE_ERROR"},
    }
    if event.parent_id:
        span["parentSpanId"] = (
            event.parent_id.replace("span_", "").replace("evt_", "").rjust(16, "0")[-16:]
        )
    return span


def _event_type(operation: str, name: Any) -> str:
    op = operation.lower()
    span_name = str(name or "").lower()
    if op in {"chat", "text_completion", "embeddings", "generate_content"}:
        return "model_call"
    if op in {"execute_tool"} or "tool" in span_name:
        return "tool_call"
    if op == "retry" or "retry" in span_name:
        return "retry"
    return "tool_call" if op in {"invoke_agent", "invoke_workflow"} else "model_call"


def _operation_name(event_type: str) -> str:
    if event_type == "model_call":
        return "chat"
    if event_type == "tool_call":
        return "execute_tool"
    return event_type


def _attributes(span: dict[str, Any]) -> dict[str, Any]:
    raw = span.get("attributes", {})
    if isinstance(raw, dict):
        return raw.copy()
    attrs: dict[str, Any] = {}
    for item in raw or []:
        key = item.get("key")
        if key:
            attrs[key] = _attribute_value(item.get("value", {}))
    return attrs


def _attribute_value(value: dict[str, Any]) -> Any:
    if not isinstance(value, dict):
        return value
    for key in ("stringValue", "intValue", "doubleValue", "boolValue"):
        if key in value:
            return value[key]
    if "arrayValue" in value:
        values = value.get("arrayValue", {}).get("values", [])
        return [_attribute_value(item) for item in values]
    return None


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


def _duration_ms(started_ns: int | None, ended_ns: int | None, span: dict[str, Any]) -> float:
    if started_ns is not None and ended_ns is not None and ended_ns >= started_ns:
        return (ended_ns - started_ns) / _NANOSECONDS_PER_MILLISECOND
    return float(span.get("duration_ms") or span.get("durationMs") or 0.0)


def _trace_bounds_ns(spans: list[dict[str, Any]]) -> tuple[int, int] | None:
    if not spans:
        return None
    bounds: list[tuple[int, int]] = []
    for span in spans:
        started_ns = _int_or_none(span.get("startTimeUnixNano") or span.get("start_time_unix_nano"))
        ended_ns = _int_or_none(span.get("endTimeUnixNano") or span.get("end_time_unix_nano"))
        if started_ns is None or ended_ns is None or ended_ns < started_ns:
            return None
        bounds.append((started_ns, ended_ns))
    return min(started for started, _ in bounds), max(ended for _, ended in bounds)


def _iso_from_ns(value: int | None) -> str:
    if value is None:
        return datetime.now(timezone.utc).isoformat()
    return datetime.fromtimestamp(value / 1_000_000_000, tz=timezone.utc).isoformat()


def _ns_from_iso(value: str) -> int:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return int(parsed.timestamp() * 1_000_000_000)


def _int_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def _first_string(attrs: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = attrs.get(key)
        if value:
            return str(value)
    return None


def _status(span: dict[str, Any]) -> str:
    status = span.get("status", {})
    if isinstance(status, dict) and str(status.get("code", "")).endswith("ERROR"):
        return "error"
    return "ok"


def _error(span: dict[str, Any]) -> str | None:
    status = span.get("status", {})
    if isinstance(status, dict):
        return status.get("message")
    return None


def _direct_attribute_keys() -> set[str]:
    return {
        "agentloop.event_type",
        "agentloop.name",
        "agentloop.trace.name",
        "gen_ai.operation.name",
        "gen_ai.request.model",
        "gen_ai.response.model",
        "gen_ai.tool.name",
    }
