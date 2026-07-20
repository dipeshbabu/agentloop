from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from agentloop.events import AgentEvent
from agentloop.otel_ids import to_span_id, to_trace_id
from agentloop.schema import TraceValidationError
from agentloop.tracer import AgentTrace
from agentloop.version import __version__

# Prefix under which AgentLoop exports user event metadata as span attributes.
_METADATA_PREFIX = "agentloop.metadata."

_NANOSECONDS_PER_MILLISECOND = 1_000_000


def trace_from_otel(
    payload: dict[str, Any] | list[dict[str, Any]], name: str | None = None
) -> AgentTrace:
    """Convert a single-trace OTLP/GenAI-style JSON payload into an AgentLoop trace.

    This accepts the standard OTLP JSON shape (`resourceSpans -> scopeSpans -> spans`),
    a dictionary with a top-level `spans` list, or a raw list of span dictionaries.

    The payload must describe **one** trace. OTLP exporters routinely batch spans
    from several traces into one request; passing such a batch here raises
    :class:`agentloop.schema.TraceValidationError` rather than silently collapsing
    the traces into one. Use :func:`traces_from_otel` for batches. An empty payload
    yields an empty trace, preserving the historical single-trace behavior.
    """

    traces = traces_from_otel(payload, name=name)
    if len(traces) > 1:
        raise TraceValidationError(
            "traceId",
            f"payload contains {len(traces)} distinct traces; use traces_from_otel() "
            "to import a batch",
        )
    if not traces:
        return _build_trace([], {}, name=name)
    return traces[0]


def traces_from_otel(
    payload: dict[str, Any] | list[dict[str, Any]], name: str | None = None
) -> list[AgentTrace]:
    """Convert an OTLP/GenAI-style batch into one AgentLoop trace per trace id.

    Spans are grouped by their ``traceId`` in first-seen order regardless of how
    they are interleaved in the payload, so a batch of unrelated traces keeps its
    boundaries. Parent links are resolved within each trace only. Spans with no
    trace id are grouped together into a single trace. Accepts the same input
    shapes as :func:`trace_from_otel`.
    """

    groups: dict[str, list[dict[str, Any]]] = {}
    group_resource: dict[str, dict[str, Any]] = {}
    for resource_attrs, spans in _iter_resource_blocks(payload):
        block_trace_keys = {_trace_key(span) for span in spans}
        block_is_single_trace = len(block_trace_keys) == 1
        for span in spans:
            key = _trace_key(span)
            groups.setdefault(key, []).append(span)
            # Only adopt a resource block's native identity when that block holds
            # exactly one trace; a multi-trace block cannot name every trace.
            if key not in group_resource:
                group_resource[key] = resource_attrs if block_is_single_trace else {}
            elif not block_is_single_trace or group_resource[key] != resource_attrs:
                group_resource[key] = {}
    return [
        _build_trace(spans, group_resource.get(key, {}), name=name) for key, spans in groups.items()
    ]


def _build_trace(
    spans: list[dict[str, Any]], resource_attrs: dict[str, Any], *, name: str | None
) -> AgentTrace:
    run_id = _native_run_id(resource_attrs) or _run_id_from_spans(spans)
    trace_name = name or _native_trace_name(resource_attrs) or _trace_name(spans)
    trace = AgentTrace(name=trace_name, run_id=run_id, metadata={"source": "otel"})
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


def _iter_resource_blocks(
    payload: dict[str, Any] | list[dict[str, Any]],
) -> list[tuple[dict[str, Any], list[dict[str, Any]]]]:
    """Return ``(resource_attributes, spans)`` blocks, preserving payload order.

    Resource attributes carry the AgentLoop-native identity that
    :func:`trace_to_otel` writes (trace name and run id). Raw span lists and
    top-level ``spans`` payloads have no resource, so their attributes are empty.
    """

    if isinstance(payload, list):
        return [({}, list(payload))]
    if "spans" in payload and isinstance(payload["spans"], list):
        return [(_resource_attributes(payload.get("resource")), list(payload["spans"]))]

    blocks: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    for resource_span in payload.get("resourceSpans", []):
        resource_attrs = _resource_attributes(resource_span.get("resource"))
        spans: list[dict[str, Any]] = []
        for scope_span in resource_span.get("scopeSpans", []):
            spans.extend(scope_span.get("spans", []))
        blocks.append((resource_attrs, spans))
    return blocks


def _resource_attributes(resource: Any) -> dict[str, Any]:
    if not isinstance(resource, dict):
        return {}
    return _attributes(resource)


def _trace_key(span: dict[str, Any]) -> str:
    return str(span.get("traceId") or span.get("trace_id") or "").lower()


def _native_run_id(resource_attrs: dict[str, Any]) -> str | None:
    run_id = resource_attrs.get("agentloop.run_id")
    return str(run_id) if run_id else None


def _native_trace_name(resource_attrs: dict[str, Any]) -> str | None:
    name = resource_attrs.get("agentloop.trace.name")
    return str(name) if name else None


def _run_id_from_spans(spans: list[dict[str, Any]]) -> str:
    for span in spans:
        trace_id = str(span.get("traceId") or span.get("trace_id") or "")
        if trace_id:
            # Preserve the full trace id (not just the last 16 chars) so a valid
            # OTLP id round-trips back out unchanged via to_trace_id().
            return "run_" + trace_id.lower()
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
    parent_span_id = str(span.get("parentSpanId") or span.get("parent_span_id") or "") or None
    status = _status(span)
    error = _error(span)

    metadata = _user_metadata(attrs, span, span_id)

    # Restore AgentLoop-native ids when the exporter preserved them, so repeated
    # round trips keep stable event and parent identity (issue #63). Fall back to
    # the OTLP span ids for third-party payloads.
    native_event_id = attrs.get("agentloop.native_event_id")
    event_id = str(native_event_id) if native_event_id else ("span_" + span_id if span_id else "")
    native_parent_id = attrs.get("agentloop.native_parent_id")
    if native_parent_id:
        parent_id = str(native_parent_id)
    elif parent_span_id:
        parent_id = "span_" + parent_span_id
    else:
        parent_id = None

    return AgentEvent(
        event_id=event_id,
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
        parent_id=parent_id,
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
        if key in _reserved_metadata_keys():
            # Transport diagnostics (otel_span_id/otel_trace_id) are re-derived on
            # import; re-exporting them would let user metadata grow each round trip.
            continue
        if isinstance(value, str | int | float | bool):
            attrs.append(_attribute(f"{_METADATA_PREFIX}{key}", value))

    # Keep the original native ids in attributes so a remapped (non-hex/custom)
    # id stays diagnosable; the run id is already carried as agentloop.run_id.
    attrs.append(_attribute("agentloop.native_event_id", event.event_id))
    if event.parent_id:
        attrs.append(_attribute("agentloop.native_parent_id", event.parent_id))

    span = {
        "traceId": to_trace_id(trace.run_id),
        "spanId": to_span_id(event.event_id),
        "name": event.name,
        "kind": "SPAN_KIND_INTERNAL",
        "startTimeUnixNano": str(start_ns),
        "endTimeUnixNano": str(end_ns),
        "attributes": attrs,
        "status": {"code": "STATUS_CODE_OK" if event.status == "ok" else "STATUS_CODE_ERROR"},
    }
    if event.parent_id:
        span["parentSpanId"] = to_span_id(event.parent_id)
    return span


def _user_metadata(attrs: dict[str, Any], span: dict[str, Any], span_id: str) -> dict[str, Any]:
    """Build user event metadata from span attributes (issue #63).

    Decodes the ``agentloop.metadata.`` namespace exactly once, preserves genuine
    third-party attributes, and drops AgentLoop transport/native-id and gen_ai
    bookkeeping keys so they never leak into or accumulate in user metadata.
    """

    decoded: dict[str, Any] = {}
    passthrough: dict[str, Any] = {}
    for key, value in attrs.items():
        if key.startswith(_METADATA_PREFIX):
            decoded[key[len(_METADATA_PREFIX) :]] = value
        elif key.startswith("gen_ai.usage."):
            continue
        elif key in _direct_attribute_keys() or key in _transport_attribute_keys():
            continue
        else:
            passthrough[key] = value  # third-party attribute, preserved as-is
    # Native AgentLoop metadata wins over any colliding third-party attribute.
    metadata = {**passthrough, **decoded}
    # Transport diagnostics: available on the imported event but never re-exported.
    metadata["otel_span_id"] = span_id
    metadata["otel_trace_id"] = span.get("traceId") or span.get("trace_id")
    return metadata


def _transport_attribute_keys() -> set[str]:
    return {
        "agentloop.run_id",
        "agentloop.native_event_id",
        "agentloop.native_parent_id",
    }


def _reserved_metadata_keys() -> set[str]:
    return {"otel_span_id", "otel_trace_id"}


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
