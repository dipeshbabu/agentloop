"""Structural OTLP bounds and payload minimization before the existing parser."""

from __future__ import annotations

import re
from copy import deepcopy
from hashlib import sha256
from typing import Any

from agentloop.interoperability.validation import ImportLimits, ImportValidationError
from agentloop.interventions import canonical_json
from agentloop.otel import _attributes
from agentloop.otel_semantics import INPUT_USAGE, OUTPUT_USAGE

_STRUCTURAL_STRINGS = frozenset(
    {
        "gen_ai.operation.name",
        "gen_ai.provider.name",
        "gen_ai.system",
        "gen_ai.request.model",
        "gen_ai.response.model",
        "gen_ai.conversation.id",
        "gen_ai.tool.name",
        "gen_ai.tool.call.id",
        "openinference.span.kind",
        "service.name",
        "service.version",
        "service.instance.id",
        "telemetry.sdk.name",
        "telemetry.sdk.version",
        "telemetry.sdk.language",
        "agent.name",
        "agent.version",
        "session.id",
        "trajectory.id",
        "atif.schema_version",
        "llm.model_name",
        "llm.provider",
        "llm.response.model_name",
        "llm.request.model_name",
        "embedding.model_name",
        "tool.name",
        "tool.id",
        "tool_call.id",
        "mcp.method.name",
        "mcp.session.id",
        "rpc.system",
        "http.request.method",
        "http.method",
        "error.type",
        "exception.type",
        "gen_ai.evaluation.name",
        "gen_ai.evaluation.score.label",
        "evaluation.name",
        "evaluation.label",
        "annotation.name",
        "annotation.label",
        "agentloop.event_type",
        "agentloop.operation_kind",
        "agentloop.model",
        "agentloop.name",
        "agentloop.run_id",
        "agentloop.native_event_id",
        "agentloop.native_parent_id",
        "agentloop.token_provenance",
        "agentloop.trace.name",
        "agentloop.trace.started_at",
        "agentloop.trace.ended_at",
    }
)


def fail(field: str, reason: str, code: str = "invalid_otlp") -> None:
    raise ImportValidationError(code, field, reason)


def object_value(value: Any, field: str) -> dict:
    if not isinstance(value, dict):
        fail(field, "must be an object")
    return value


def array(value: Any, field: str) -> list:
    if not isinstance(value, list):
        fail(field, "must be an array")
    return value


def wire_id(value: Any, field: str, length: int, *, missing: bool = False) -> str | None:
    if missing and value in (None, ""):
        return None
    if (
        not isinstance(value, str)
        or not re.fullmatch(r"[0-9a-fA-F]{" + str(length) + "}", value)
        or int(value, 16) == 0
    ):
        fail(field, "must be a nonzero hexadecimal OTLP identifier")
    return value.lower()


def attributes(value: Any, field: str) -> dict:
    """Validate the standard list form and the existing parser's object form."""
    if value is None:
        return {}
    if isinstance(value, dict):
        if any(not key or len(key.encode()) > 512 for key in value):
            fail(field, "attribute keys must be bounded and nonempty")
        return _attributes({"attributes": value})
    items = array(value, field)
    seen = set()
    for item in items:
        object_value(item, field)
        key = item.get("key")
        if not isinstance(key, str) or not key or len(key.encode()) > 512 or key in seen:
            fail(field, "attribute keys must be bounded and unique")
        seen.add(key)
        _any_value(item.get("value"), field + ".value")
    return _attributes({"attributes": items})


def _any_value(value: Any, field: str) -> None:
    value = object_value(value, field)
    if not value:
        return
    kinds = {
        "stringValue",
        "boolValue",
        "intValue",
        "doubleValue",
        "bytesValue",
        "arrayValue",
        "kvlistValue",
    }
    if len(value) != 1 or not set(value) <= kinds:
        fail(field, "unsupported or ambiguous AnyValue")
    key, item = next(iter(value.items()))
    if key in {"stringValue", "bytesValue"} and not isinstance(item, str):
        fail(field, "string/bytes attributes must be strings")
    if key == "boolValue" and type(item) is not bool:
        fail(field, "boolean attributes must be boolean")
    if key == "doubleValue" and type(item) not in {int, float}:
        fail(field, "numeric attributes must be numbers")
    if key == "arrayValue":
        for child in array(object_value(item, field).get("values", []), field):
            _any_value(child, field)
    if key == "kvlistValue":
        attributes(object_value(item, field).get("values", []), field)


def minimized_value(value: Any, key: str, *, capture_content: bool) -> Any:
    """Retain structural/numeric attributes; content opt-in never captures secrets."""
    compact = key.lower().replace("_", "").replace("-", "")
    sensitive = any(
        word in compact
        for word in (
            "password",
            "passwd",
            "authorization",
            "apikey",
            "secret",
            "credential",
            "bearer",
            "cookie",
            "privatekey",
        )
    ) or (
        "token" in compact
        and not any(
            word in compact
            for word in (
                "tokencount",
                "usage",
                "inputtokens",
                "outputtokens",
                "prompttokens",
                "completiontokens",
                "cachedtokens",
            )
        )
    )
    private = sensitive or any(
        word in compact
        for word in (
            "reasoningcontent",
            "chainofthought",
            "tokenids",
            "logprobs",
            "exceptionmessage",
            "exceptionstacktrace",
        )
    )
    if private or isinstance(value, str) and not (key in _STRUCTURAL_STRINGS or capture_content):
        encoded = canonical_json(value).encode()
        return {
            "capture": "omitted",
            "sha256": sha256(encoded).hexdigest(),
            "size_bytes": len(encoded),
        }
    if isinstance(value, dict):
        return {
            name: minimized_value(item, name, capture_content=capture_content)
            for name, item in value.items()
        }
    if isinstance(value, list):
        return [minimized_value(item, key, capture_content=capture_content) for item in value]
    return deepcopy(value)


def minimized_attributes(owner: dict, *, capture_content: bool) -> None:
    raw = owner.get("attributes")
    decoded = attributes(raw, "attributes")
    clean = {
        key: minimized_value(value, key, capture_content=capture_content)
        for key, value in decoded.items()
    }
    # Attribute object form is already supported by the core parser and avoids
    # reimplementing protobuf AnyValue serialization.
    if raw is not None:
        owner["attributes"] = clean


def flatten(
    payload: Any, limits: ImportLimits, *, capture_content: bool
) -> tuple[dict, list[dict], list[dict]]:
    """Return a validated/minimized payload plus source span and log witnesses."""
    object_value(payload, "record")
    _numeric_string_bounds(payload, limits)
    if not ("resourceSpans" in payload or "resourceLogs" in payload or "spans" in payload):
        fail("record", "OTLP resource spans/logs or a spans array are required")
    clean = deepcopy(payload)
    spans, logs = [], []
    root_omissions = len(set(payload) - {"resourceSpans", "resourceLogs", "spans", "resource"})
    resources = clean.get("resourceSpans", [])
    if "spans" in clean:
        resources = [
            {"resource": clean.get("resource", {}), "scopeSpans": [{"spans": clean["spans"]}]}
        ]
        clean = {"resourceSpans": resources, "resourceLogs": clean.get("resourceLogs", [])}
    for field, scope_field, items_field, destination in (
        ("resourceSpans", "scopeSpans", "spans", spans),
        ("resourceLogs", "scopeLogs", "logRecords", logs),
    ):
        blocks = resources if field == "resourceSpans" else clean.get(field, [])
        for resource in array(blocks, field):
            object_value(resource, field)
            context = deepcopy(resource.get("resource", {}))
            object_value(context, "resource")
            attributes(context.get("attributes"), "resource.attributes")
            if "resource" in resource:
                minimized_attributes(resource["resource"], capture_content=capture_content)
            for scope in array(resource.get(scope_field, []), scope_field):
                object_value(scope, scope_field)
                scope_info = object_value(scope.get("scope", {}), "scope")
                attributes(scope_info.get("attributes"), "scope.attributes")
                source_scope = deepcopy(scope_info)
                minimized_attributes(scope_info, capture_content=capture_content)
                for key in list(scope_info):
                    if key not in {"name", "version", "attributes"}:
                        scope_info[key] = minimized_value(
                            scope_info[key], key, capture_content=capture_content
                        )
                for ordinal, item in enumerate(array(scope.get(items_field, []), items_field)):
                    object_value(item, items_field)
                    source = deepcopy(item)
                    for canonical, alias in (
                        ("traceId", "trace_id"),
                        ("spanId", "span_id"),
                        ("parentSpanId", "parent_span_id"),
                        ("startTimeUnixNano", "start_time_unix_nano"),
                        ("endTimeUnixNano", "end_time_unix_nano"),
                    ):
                        if alias in item:
                            if canonical in item and item[canonical] != item[alias]:
                                fail(canonical, "contradictory source field aliases")
                            item[canonical] = item[alias]
                    if items_field == "spans":
                        for field_name in ("startTimeUnixNano", "endTimeUnixNano"):
                            value = item.get(field_name)
                            if value is not None:
                                if type(value) not in {int, str}:
                                    fail(
                                        field_name, "timestamp must be an unsigned decimal integer"
                                    )
                                try:
                                    number = int(value)
                                except ValueError:
                                    fail(
                                        field_name, "timestamp must be an unsigned decimal integer"
                                    )
                                if number < 0:
                                    fail(
                                        field_name, "timestamp must be an unsigned decimal integer"
                                    )
                                item[field_name] = str(number)
                    trace_id = wire_id(item.get("traceId"), "traceId", 32, missing=True)
                    span_id = wire_id(item.get("spanId"), "spanId", 16, missing=True)
                    if items_field == "spans":
                        wire_id(item.get("parentSpanId"), "parentSpanId", 16, missing=True)
                        for link in array(item.get("links", []), "links"):
                            object_value(link, "links")
                            wire_id(link.get("traceId"), "links.traceId", 32)
                            wire_id(link.get("spanId"), "links.spanId", 16)
                            minimized_attributes(link, capture_content=capture_content)
                        for event in array(item.get("events", []), "events"):
                            object_value(event, "events")
                            minimized_attributes(event, capture_content=capture_content)
                            for key in list(event):
                                if key not in {
                                    "name",
                                    "timeUnixNano",
                                    "attributes",
                                    "droppedAttributesCount",
                                }:
                                    event[key] = minimized_value(
                                        event[key], key, capture_content=capture_content
                                    )
                        if "status" in item:
                            status = object_value(item["status"], "status")
                            code = status.get("code")
                            if code is not None and (
                                type(code) not in {int, str}
                                or code
                                not in {
                                    0,
                                    1,
                                    2,
                                    "STATUS_CODE_UNSET",
                                    "STATUS_CODE_OK",
                                    "STATUS_CODE_ERROR",
                                }
                            ):
                                fail("status.code", "unsupported status code")
                            status.pop("message", None)
                    else:
                        if "body" in item:
                            _any_value(item["body"], "body")
                            item["body"] = (
                                {"stringValue": "[omitted]"}
                                if not capture_content
                                else item["body"]
                            )
                    original_attrs = attributes(source.get("attributes"), "attributes")
                    minimized_attributes(item, capture_content=capture_content)
                    if len(canonical_json(item).encode()) > limits.max_metadata_bytes:
                        fail(
                            "span.metadata",
                            "metadata exceeds configured bounds",
                            "record_limit_exceeded",
                        )
                    destination.append(
                        {
                            "span": item,
                            "trace_id": trace_id,
                            "span_id": span_id,
                            "source_sha256": sha256(
                                canonical_json(
                                    {
                                        "item": source,
                                        "resource": context,
                                        "scope": source_scope,
                                        "schema_url": scope.get(
                                            "schemaUrl", resource.get("schemaUrl")
                                        ),
                                    }
                                ).encode()
                            ).hexdigest(),
                            "attributes": original_attrs,
                            "resource": attributes(
                                context.get("attributes"), "resource.attributes"
                            ),
                            "scope": source_scope,
                            "ordinal": ordinal,
                            "unrepresented_fields": root_omissions
                            + len(
                                set(resource) - {"resource", "scopeSpans", "scopeLogs", "schemaUrl"}
                            )
                            + (
                                len(
                                    set(source)
                                    - {
                                        "traceId",
                                        "spanId",
                                        "parentSpanId",
                                        "trace_id",
                                        "span_id",
                                        "parent_span_id",
                                        "startTimeUnixNano",
                                        "endTimeUnixNano",
                                        "start_time_unix_nano",
                                        "end_time_unix_nano",
                                        "name",
                                        "kind",
                                        "traceState",
                                        "flags",
                                        "status",
                                        "attributes",
                                        "events",
                                        "links",
                                        "droppedAttributesCount",
                                        "droppedEventsCount",
                                        "droppedLinksCount",
                                    }
                                )
                                if items_field == "spans"
                                else 0
                            ),
                        }
                    )
    return clean, spans, logs


def _numeric_string_bounds(payload: dict, limits: ImportLimits) -> None:
    """Bound decimal strings before the core parser calls int on wire values."""
    numeric_keys = {
        "intValue",
        "startTimeUnixNano",
        "endTimeUnixNano",
        "timeUnixNano",
        "observedTimeUnixNano",
        "start_time_unix_nano",
        "end_time_unix_nano",
        "agentloop.input_tokens",
        "agentloop.output_tokens",
        "gen_ai.usage.cache_read.input_tokens",
        "gen_ai.usage.cache_write.input_tokens",
        "llm.token_count.prompt_details.cache_read",
        "llm.token_count.prompt_details.cache_write",
        "gen_ai.usage.reasoning.output_tokens",
        "llm.token_count.completion_details.reasoning",
        "llm.token_count.total",
        *INPUT_USAGE,
        *OUTPUT_USAGE,
    }
    pending = [payload]
    while pending:
        node = pending.pop()
        if isinstance(node, dict):
            for key, value in node.items():
                if (
                    key in numeric_keys
                    and isinstance(value, str)
                    and len(value) > limits.max_number_chars
                ):
                    fail(
                        "numeric_string",
                        "numeric representation exceeds configured bounds",
                        "record_limit_exceeded",
                    )
            if isinstance(node.get("key"), str) and node["key"] in numeric_keys:
                encoded = node.get("value")
                if isinstance(encoded, dict) and any(
                    isinstance(value, str) and len(value) > limits.max_number_chars
                    for value in encoded.values()
                ):
                    fail(
                        "numeric_string",
                        "numeric representation exceeds configured bounds",
                        "record_limit_exceeded",
                    )
            pending.extend(value for value in node.values() if isinstance(value, (dict, list)))
        elif isinstance(node, list):
            pending.extend(value for value in node if isinstance(value, (dict, list)))
