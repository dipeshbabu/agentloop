"""Native trace schema: versioning, validation, and compatibility policy.

This module is the single source of truth for the serialized AgentLoop trace
contract. It is imported by the core deserialization path (`AgentTrace.from_dict`
/ `AgentEvent.from_dict`), which every consumer — CLI import, HTTP ingestion,
both stores, and the OTLP/Vercel adapters' round-trip tests — shares.

It has no framework dependency: validation failures raise
:class:`TraceValidationError`, and boundary code (for example the FastAPI server)
maps that to a structured 4xx response. See ``docs/TRACE_SCHEMA.md`` for the
published schema and compatibility policy.
"""

from __future__ import annotations

import math
from typing import Any

# Native trace schema version. Serialized traces carry this under
# ``schema_version``. Bump the MAJOR component only for a breaking change to the
# serialized shape; readers accept an equal-or-lower MAJOR and tolerate a missing
# value (0.4-era traces predate the field). See ``docs/TRACE_SCHEMA.md``.
SCHEMA_VERSION = "1.0"

# Statuses AgentLoop records on an event.
_VALID_STATUSES = frozenset({"ok", "error"})

# Event fields the schema knows about. Unknown structural fields are dropped on
# read (the documented forward-compatibility policy); use ``metadata`` to carry
# data that must survive a round trip.
_KNOWN_EVENT_FIELDS = frozenset(
    {
        "event_id",
        "run_id",
        "event_type",
        "name",
        "started_at",
        "ended_at",
        "duration_ms",
        "parent_id",
        "model",
        "input_tokens",
        "output_tokens",
        "input_text",
        "output_text",
        "status",
        "error",
        "metadata",
    }
)

# Event fields that must be present and non-null (they have no dataclass default).
_REQUIRED_EVENT_FIELDS = (
    "event_id",
    "run_id",
    "event_type",
    "name",
    "started_at",
    "ended_at",
    "duration_ms",
)

_REQUIRED_STRING_EVENT_FIELDS = (
    "event_id",
    "run_id",
    "event_type",
    "name",
    "started_at",
    "ended_at",
)

_OPTIONAL_STRING_EVENT_FIELDS = (
    "parent_id",
    "model",
    "input_text",
    "output_text",
    "error",
)

_NON_NEGATIVE_INT_EVENT_FIELDS = ("input_tokens", "output_tokens")


class TraceValidationError(ValueError):
    """A serialized trace or event failed schema validation.

    Carries the offending ``field`` path and a human ``reason`` so a boundary can
    surface both in a 4xx response. It subclasses :class:`ValueError` so existing
    callers that already catch ``ValueError`` (the CLI loaders, for example) keep
    working.
    """

    def __init__(self, field: str, reason: str) -> None:
        self.field = field
        self.reason = reason
        super().__init__(f"{field}: {reason}")


def schema_major(version: str) -> int:
    """Return the MAJOR component of a ``schema_version`` string, or 0 if unset."""

    if not version:
        return 0
    head = str(version).split(".", 1)[0]
    try:
        return int(head)
    except ValueError:
        raise TraceValidationError("schema_version", f"is not a valid version: {version!r}")


def check_schema_version(version: Any) -> None:
    """Reject payloads written by a newer, incompatible major schema version."""

    if version is None:
        return  # 0.4-era traces have no schema_version; tolerate their absence.
    if not isinstance(version, str):
        raise TraceValidationError("schema_version", "must be a string")
    if schema_major(version) > schema_major(SCHEMA_VERSION):
        raise TraceValidationError(
            "schema_version",
            f"trace was written with schema {version}, which is newer than the "
            f"supported {SCHEMA_VERSION}; upgrade AgentLoop to read it",
        )


def coerce_event_dict(data: Any, *, index: int) -> dict[str, Any]:
    """Validate one serialized event and return kwargs for :class:`AgentEvent`.

    Unknown fields are ignored (forward-compatibility policy). Raises
    :class:`TraceValidationError` naming ``events[<index>].<field>`` on any
    missing required field, wrong type, or out-of-range numeric value.
    """

    where = f"events[{index}]"
    if not isinstance(data, dict):
        raise TraceValidationError(where, "must be an object")

    for name in _REQUIRED_EVENT_FIELDS:
        if data.get(name) is None:
            raise TraceValidationError(f"{where}.{name}", "is required")

    clean: dict[str, Any] = {}
    for name in _REQUIRED_STRING_EVENT_FIELDS:
        value = data[name]
        if not isinstance(value, str) or not value:
            raise TraceValidationError(f"{where}.{name}", "must be a non-empty string")
        clean[name] = value

    for name in _OPTIONAL_STRING_EVENT_FIELDS:
        value = data.get(name)
        if value is not None and not isinstance(value, str):
            raise TraceValidationError(f"{where}.{name}", "must be a string or null")
        clean[name] = value

    clean["duration_ms"] = _non_negative_number(data["duration_ms"], f"{where}.duration_ms")

    for name in _NON_NEGATIVE_INT_EVENT_FIELDS:
        if name in data and data[name] is not None:
            clean[name] = _non_negative_int(data[name], f"{where}.{name}")

    status = data.get("status", "ok")
    if not isinstance(status, str) or status not in _VALID_STATUSES:
        raise TraceValidationError(
            f"{where}.status",
            f"must be one of {', '.join(sorted(_VALID_STATUSES))}",
        )
    clean["status"] = status

    metadata = data.get("metadata", {})
    if not isinstance(metadata, dict):
        raise TraceValidationError(f"{where}.metadata", "must be an object")
    clean["metadata"] = metadata

    return clean


def validate_trace_dict(data: Any) -> None:
    """Validate a serialized trace envelope before its events are constructed.

    Checks the schema version, required top-level fields, metadata shape, unique
    event ids, and event-to-trace run-id consistency. Individual event fields are
    validated by :func:`coerce_event_dict`.
    """

    if not isinstance(data, dict):
        raise TraceValidationError("trace", "must be an object")

    check_schema_version(data.get("schema_version"))

    name = data.get("name")
    if not isinstance(name, str) or not name:
        raise TraceValidationError("name", "must be a non-empty string")

    run_id = data.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise TraceValidationError("run_id", "must be a non-empty string")

    metadata = data.get("metadata", {})
    if not isinstance(metadata, dict):
        raise TraceValidationError("metadata", "must be an object")

    events = data.get("events", [])
    if not isinstance(events, list):
        raise TraceValidationError("events", "must be a list")

    seen_ids: set[str] = set()
    for index, event in enumerate(events):
        if not isinstance(event, dict):
            raise TraceValidationError(f"events[{index}]", "must be an object")
        event_id = event.get("event_id")
        if isinstance(event_id, str):
            if event_id in seen_ids:
                raise TraceValidationError(
                    f"events[{index}].event_id", f"is not unique within the trace: {event_id}"
                )
            seen_ids.add(event_id)
        event_run_id = event.get("run_id")
        if isinstance(event_run_id, str) and event_run_id != run_id:
            raise TraceValidationError(
                f"events[{index}].run_id",
                f"does not match the trace run_id {run_id!r}",
            )


def _non_negative_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TraceValidationError(field, "must be a number")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise TraceValidationError(field, "must be a finite, non-negative number")
    return number


def _non_negative_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TraceValidationError(field, "must be an integer")
    if value < 0:
        raise TraceValidationError(field, "must be non-negative")
    return value
