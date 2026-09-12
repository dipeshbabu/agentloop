from __future__ import annotations

import math
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from agentloop.events import AgentEvent
from agentloop.operations import LEGACY_OPERATION_KINDS, normalize_operation_kind
from agentloop.otel_ids import to_span_id, to_trace_id
from agentloop.otel_semantics import (
    INPUT_USAGE,
    OUTPUT_USAGE,
    evaluation_result,
    extended_kind,
    normalize_metadata,
    usage_count,
)
from agentloop.schema import TraceValidationError
from agentloop.tokens import PROVIDER, UNAVAILABLE
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
        if not spans and _native_run_id(resource_attrs):
            key = to_trace_id(_native_run_id(resource_attrs))
            groups.setdefault(key, [])
            group_resource[key] = resource_attrs
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
    traces = [
        _build_trace(spans, group_resource.get(key, {}), name=name) for key, spans in groups.items()
    ]
    by_id = dict(zip(groups, traces))
    events_by_trace = {
        key: {event.metadata.get("otel_span_id"): event for event in trace.events}
        for key, trace in by_id.items()
    }
    if isinstance(payload, dict):
        for resource in payload.get("resourceLogs", []):
            for scope in resource.get("scopeLogs", []):
                for record in scope.get("logRecords", []):
                    key = _trace_key(record)
                    if key not in by_id:
                        trace = AgentTrace(
                            name=name or "otel_log_trace",
                            run_id="run_" + key if key else "run_otel_logs",
                            metadata={"source": "otel", "execution_data_present": False},
                            elapsed_ms=0,
                        )
                        by_id[key] = trace
                        traces.append(trace)
                    trace = by_id[key]
                    event = events_by_trace.get(key, {}).get(record.get("spanId"))
                    metadata = event.metadata if event is not None else trace.metadata
                    metadata.setdefault("otel_log_records", []).append(
                        {
                            "record": deepcopy(record),
                            "resource": deepcopy(resource.get("resource", {})),
                            "scope": deepcopy(scope.get("scope", {})),
                        }
                    )
                    result = evaluation_result(
                        _attributes(record), source=str(record.get("eventName") or "otel_log")
                    )
                    if result:
                        metadata.setdefault("evaluation_results", []).append(result)
    return traces


def _build_trace(
    spans: list[dict[str, Any]], resource_attrs: dict[str, Any], *, name: str | None
) -> AgentTrace:
    run_id = _native_run_id(resource_attrs) or _run_id_from_spans(spans)
    trace_name = name or _native_trace_name(resource_attrs) or _trace_name(spans)
    native_metadata = resource_attrs.get("agentloop.trace.metadata")
    trace = AgentTrace(
        name=trace_name,
        run_id=run_id,
        metadata=deepcopy(native_metadata)
        if isinstance(native_metadata, dict)
        else {"source": "otel"},
    )
    for span in spans:
        trace.add_event(_event_from_span(span, run_id))
    bounds = _trace_bounds_ns(spans)
    if bounds is not None:
        started_ns, ended_ns = bounds
        trace.started_at = _iso_from_ns(started_ns)
        trace.ended_at = _iso_from_ns(ended_ns)
        trace.elapsed_ms = (ended_ns - started_ns) / _NANOSECONDS_PER_MILLISECOND
    if "agentloop.trace.started_at" in resource_attrs:
        trace.started_at = resource_attrs["agentloop.trace.started_at"]
        trace.ended_at = resource_attrs.get("agentloop.trace.ended_at")
        trace.elapsed_ms = resource_attrs.get("agentloop.trace.elapsed_ms")
        if trace.elapsed_ms is not None:
            trace.elapsed_ms = _nonnegative_timing(trace.elapsed_ms, "agentloop.trace.elapsed_ms")
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
                        _attribute("agentloop.trace.metadata", trace.metadata),
                        _attribute("agentloop.trace.started_at", trace.started_at),
                        _attribute("agentloop.trace.ended_at", trace.ended_at),
                        _attribute("agentloop.trace.elapsed_ms", trace.elapsed_ms),
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
            for span in scope_span.get("spans", []):
                spans.append(
                    {
                        **span,
                        "_resource_attributes": resource_attrs,
                        "_scope": scope_span.get("scope", {}),
                        "_schema_url": scope_span.get("schemaUrl", resource_span.get("schemaUrl")),
                    }
                )
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
    event_type = str(attrs.get("agentloop.event_type") or _event_type(operation, span.get("name")))
    kind = extended_kind(attrs)
    if kind is not None and "agentloop.event_type" not in attrs:
        event_type = "model_call" if kind == "model" else "tool_call"
    started_ns = _int_or_none(span.get("startTimeUnixNano") or span.get("start_time_unix_nano"))
    ended_ns = _int_or_none(span.get("endTimeUnixNano") or span.get("end_time_unix_nano"))
    duration_ms = _duration_ms(started_ns, ended_ns, span)
    if "agentloop.duration_ms" in attrs:
        duration_ms = _nonnegative_timing(attrs["agentloop.duration_ms"], "agentloop.duration_ms")
    span_id = str(span.get("spanId") or span.get("span_id") or "")
    parent_span_id = str(span.get("parentSpanId") or span.get("parent_span_id") or "") or None
    status = _status(span)
    error = _error(span)

    metadata = _user_metadata(attrs, span, span_id)
    if "operation_kind" not in metadata:
        if "agentloop.operation_kind" in attrs:
            metadata["operation_kind"] = attrs["agentloop.operation_kind"]
        elif "agentloop.event_type" not in attrs:
            metadata["operation_kind"] = kind or _import_operation_kind(operation, event_type)
    normalize_metadata(attrs, metadata)
    _preserve_span_details(span, attrs, metadata)

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
            or attrs.get("tool.name")
            or attrs.get("agentloop.name")
            or span.get("name")
            or event_type
        ),
        started_at=_iso_from_ns(started_ns),
        ended_at=_iso_from_ns(ended_ns),
        duration_ms=duration_ms,
        parent_id=parent_id,
        model=_first_string(
            attrs,
            "gen_ai.response.model",
            "gen_ai.request.model",
            "llm.response.model_name",
            "llm.model_name",
            "llm.request.model_name",
            "embedding.model_name",
        ),
        input_tokens=usage_count(attrs, INPUT_USAGE),
        output_tokens=usage_count(attrs, OUTPUT_USAGE),
        token_provenance=_token_provenance(attrs),
        status=status,
        error=error,
        metadata=metadata,
    )


def _span_from_event(trace: AgentTrace, event: AgentEvent) -> dict[str, Any]:
    start_ns = _ns_from_iso(event.started_at)
    end_ns = _ns_from_iso(event.ended_at)
    attrs = [
        _attribute(
            "gen_ai.operation.name", _operation_name(event.event_type, event.operation_kind)
        ),
        _attribute("agentloop.event_type", event.event_type),
        _attribute("agentloop.name", event.name),
        _attribute("agentloop.duration_ms", event.duration_ms),
        _attribute("agentloop.run_id", trace.run_id),
        _attribute("gen_ai.usage.input_tokens", event.input_tokens),
        _attribute("gen_ai.usage.output_tokens", event.output_tokens),
    ]
    if "operation_kind" in event.metadata:
        attrs.append(_attribute("agentloop.operation_kind", event.metadata["operation_kind"]))
    preserved_fields = sorted(
        set(event.metadata) & (_reserved_metadata_keys() - {"otel_span_id", "otel_trace_id"})
    )
    if preserved_fields:
        attrs.append(_attribute("agentloop.preserved_span_fields", preserved_fields))
    if event.model:
        attrs.append(_attribute("gen_ai.request.model", event.model))
    attrs.append(_attribute("agentloop.token_provenance", event.token_provenance))
    for key, value in sorted((event.metadata or {}).items()):
        if key in _reserved_metadata_keys():
            # Transport diagnostics (otel_span_id/otel_trace_id) are re-derived on
            # import; re-exporting them would let user metadata grow each round trip.
            continue
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
        "kind": event.metadata.get("otel_span_kind", "SPAN_KIND_INTERNAL"),
        "startTimeUnixNano": str(start_ns),
        "endTimeUnixNano": str(end_ns),
        "attributes": attrs,
        "status": {"code": "STATUS_CODE_OK" if event.status == "ok" else "STATUS_CODE_ERROR"},
    }
    if event.parent_id:
        span["parentSpanId"] = to_span_id(event.parent_id)
    if event.error:
        span["status"]["message"] = event.error
    for source, target in (
        ("otel_links", "links"),
        ("otel_events", "events"),
        ("otel_trace_state", "traceState"),
        ("otel_span_flags", "flags"),
    ):
        if source in event.metadata:
            span[target] = deepcopy(event.metadata[source])
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
        elif "agentloop.event_type" not in attrs and key == "gen_ai.operation.name":
            passthrough[key] = value
        elif "agentloop.event_type" in attrs and key in _USAGE_ATTRIBUTE_KEYS:
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


def _preserve_span_details(
    span: dict[str, Any], attrs: dict[str, Any], metadata: dict[str, Any]
) -> None:
    native = "agentloop.event_type" in attrs
    preserved = attrs.get("agentloop.preserved_span_fields", [])
    for source, target in (
        ("links", "otel_links"),
        ("events", "otel_events"),
        ("traceState", "otel_trace_state"),
        ("flags", "otel_span_flags"),
        ("kind", "otel_span_kind"),
    ):
        if source in span and (not native or target in preserved):
            metadata.setdefault(target, deepcopy(span[source]))
    if not native:
        resource = span.get("_resource_attributes")
        if resource:
            metadata.setdefault("otel_resource_attributes", deepcopy(resource))
        if span.get("_scope"):
            metadata.setdefault("otel_scope", deepcopy(span["_scope"]))
        if span.get("_schema_url"):
            metadata.setdefault("otel_schema_url", span["_schema_url"])
    evaluations = []
    result = evaluation_result(attrs, source="span_attributes")
    if result:
        evaluations.append(result)
    for event in span.get("events", []):
        result = evaluation_result(_attributes(event), source=str(event.get("name", "span_event")))
        if result:
            evaluations.append(result)
    if evaluations:
        metadata.setdefault("evaluation_results", evaluations)


def _transport_attribute_keys() -> set[str]:
    return {
        "agentloop.run_id",
        "agentloop.native_event_id",
        "agentloop.native_parent_id",
        "agentloop.preserved_span_fields",
    }


def _reserved_metadata_keys() -> set[str]:
    return {
        "otel_span_id",
        "otel_trace_id",
        "otel_links",
        "otel_events",
        "otel_trace_state",
        "otel_span_flags",
        "otel_span_kind",
    }


def _event_type(operation: str, name: Any) -> str:
    op = operation.lower()
    span_name = str(name or "").lower()
    if op in {"chat", "text_completion", "embeddings", "generate_content"}:
        return "model_call"
    if op in {"execute_tool", "invoke_agent", "create_agent", "invoke_workflow", "retrieval"}:
        return "tool_call"
    if op in LEGACY_OPERATION_KINDS:
        return op
    if "tool" in span_name:
        return "tool_call"
    if op == "retry" or "retry" in span_name:
        return "retry"
    return "tool_call" if op in {"invoke_agent", "invoke_workflow"} else "model_call"


def _import_operation_kind(operation: str, event_type: str) -> str:
    mapping = {
        "chat": "model",
        "text_completion": "model",
        "embeddings": "model",
        "generate_content": "model",
        "execute_tool": "tool",
        "invoke_agent": "agent",
        "create_agent": "agent",
        "invoke_workflow": "workflow",
        "retrieval": "retriever",
        **LEGACY_OPERATION_KINDS,
    }
    if not operation:
        return LEGACY_OPERATION_KINDS.get(event_type, "unknown")
    normalized = normalize_operation_kind(operation)
    return mapping.get(
        operation.strip().lower(), operation if normalized == "unknown" else normalized
    )


def _operation_name(event_type: str, kind: str) -> str:
    if kind in {"agent", "workflow", "retriever"}:
        return {"agent": "invoke_agent", "workflow": "invoke_workflow", "retriever": "retrieval"}[
            kind
        ]
    if kind in {"memory", "reranker", "guardrail", "evaluator", "retry"}:
        return kind
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
    if "intValue" in value:
        raw = value["intValue"]
        if isinstance(raw, bool) or not isinstance(raw, int | str):
            raise TraceValidationError("intValue", "must be an integer or decimal integer string")
        try:
            return int(raw)
        except ValueError as exc:
            raise TraceValidationError(
                "intValue", "must be an integer or decimal integer string"
            ) from exc
    for key in ("stringValue", "doubleValue", "boolValue"):
        if key in value:
            return value[key]
    if "arrayValue" in value:
        values = value.get("arrayValue", {}).get("values", [])
        return [_attribute_value(item) for item in values]
    if "kvlistValue" in value:
        return {
            item["key"]: _attribute_value(item.get("value", {}))
            for item in value["kvlistValue"].get("values", [])
        }
    if "bytesValue" in value:
        return {"otel_bytes_base64": value["bytesValue"]}
    if value:
        return {"otel_unrecognized_value": deepcopy(value)}
    return None


def _attribute(key: str, value: Any) -> dict[str, Any]:
    if value is None:
        encoded = {}
    elif isinstance(value, dict):
        encoded = {
            "kvlistValue": {"values": [_attribute(str(name), item) for name, item in value.items()]}
        }
    elif isinstance(value, list):
        encoded = {"arrayValue": {"values": [_attribute("", item)["value"] for item in value]}}
    elif isinstance(value, bool):
        encoded = {"boolValue": value}
    elif isinstance(value, int):
        encoded = {"intValue": str(value)}
    elif isinstance(value, float):
        encoded = {"doubleValue": value}
    else:
        encoded = {"stringValue": str(value)}
    return {"key": key, "value": encoded}


def _nonnegative_timing(value: Any, field: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(value)
        or value < 0
    ):
        raise TraceValidationError(field, "must be a finite nonnegative number")
    return float(value)


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
    if isinstance(status, dict) and (
        status.get("code") in (2, "2") or str(status.get("code", "")).endswith("ERROR")
    ):
        return "error"
    return "ok"


def _error(span: dict[str, Any]) -> str | None:
    status = span.get("status", {})
    if isinstance(status, dict):
        if status.get("message"):
            return str(status["message"])
    for event in span.get("events", []):
        if event.get("name") == "exception":
            return _first_string(_attributes(event), "exception.message", "exception.type")
    return None


_USAGE_ATTRIBUTE_KEYS = INPUT_USAGE + OUTPUT_USAGE


def _token_provenance(attrs: dict[str, Any]) -> str | None:
    """Resolve a span's token provenance on import.

    An AgentLoop-produced span carries its native provenance verbatim, so a round
    trip preserves it. A third-party span does not, and is classified from what
    it actually carried: standard ``gen_ai``/``llm`` usage attributes are emitted
    by the instrumented SDK from provider usage, so they count as
    ``provider``-reported; a span with no usage attributes has no counts to
    report, which is ``unavailable`` rather than a measured zero.
    """

    native = attrs.get("agentloop.token_provenance")
    if "agentloop.token_provenance" in attrs and (native is None or isinstance(native, str)):
        return native
    return PROVIDER if any(key in attrs for key in _USAGE_ATTRIBUTE_KEYS) else UNAVAILABLE


def _direct_attribute_keys() -> set[str]:
    return {
        "agentloop.event_type",
        "agentloop.operation_kind",
        "agentloop.name",
        "agentloop.duration_ms",
        "agentloop.token_provenance",
        "agentloop.trace.name",
        "agentloop.trace.metadata",
        "gen_ai.operation.name",
        "gen_ai.request.model",
        "gen_ai.response.model",
        "gen_ai.tool.name",
    }
