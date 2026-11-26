"""Opt-in, keyed content fingerprints for a step's input and output.

A span can carry a small record under ``agentloop.content`` in its metadata:

```json
{
  "schema": "agentloop.content/v1",
  "scope": "3f9a0c4e1b2d7a65",
  "input": {"digest": "…32 hex…"},
  "output": {"digest": "…32 hex…", "items": ["…32 hex…", "…"]},
  "required": false
}
```

Nothing is recorded unless the application calls :func:`record_content` (or
:func:`content_metadata`) for a span. No text is recorded. Each digest is an
HMAC-SHA-256, truncated to 128 bits, under a random key that belongs to one
in-memory trace and is never written anywhere. Two digests with the same
``scope`` are equal exactly when the normalized values were equal (up to a
negligible collision chance). Digests are not comparable across traces or
scopes, and they cannot be reversed by hashing candidate texts, because the key
is gone once the trace object is.

What a reader of the trace still learns is equality: which recorded values in
one trace were the same. A trace loaded from a file gets a new key, so records
added after loading have a different ``scope`` and are never compared with the
original ones.

Normalization is deliberately conservative, so that values a reader would call
different never share a digest: text is Unicode NFC, line endings become
``\\n``, trailing whitespace is removed from each line and leading and trailing
blank space from the whole. Indentation and case are kept. Other values must be
JSON-like (``None``, booleans, numbers, strings, lists, tuples and string-keyed
dicts) and are digested as canonical JSON. A list or tuple of at most
:data:`MAX_ITEMS` values also records one digest per item, so a rule can tell
when a step returned nothing it had not already returned.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import secrets
import unicodedata
from collections.abc import Mapping
from typing import Any

CONTENT_KEY = "agentloop.content"
CONTENT_SCHEMA = "agentloop.content/v1"
#: Most per-item digests recorded for one list value.
MAX_ITEMS = 64

_MISSING: Any = object()
_DIGEST = re.compile(r"^[0-9a-f]{32}$")
_SCOPE = re.compile(r"^[0-9a-f]{16}$")
_TRAILING_SPACE = re.compile(r"[ \t\f\v]+\n")


def _normalized_text(value: str) -> str:
    text = unicodedata.normalize("NFC", value).replace("\r\n", "\n").replace("\r", "\n")
    return _TRAILING_SPACE.sub("\n", text + "\n").strip()


def _json_ready(value: Any, depth: int = 0) -> Any:
    if depth > 32:
        raise TypeError("content values may nest at most 32 levels")
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise TypeError("content values must not contain NaN or infinity")
        return value
    if isinstance(value, str):
        return _normalized_text(value)
    if isinstance(value, list | tuple):
        return [_json_ready(item, depth + 1) for item in value]
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("content mappings must have string keys")
        return {key: _json_ready(item, depth + 1) for key, item in value.items()}
    raise TypeError(
        f"cannot fingerprint {type(value).__name__}; pass text or a JSON-like value instead"
    )


def _canonical(value: Any) -> bytes:
    ready = _json_ready(value)
    if isinstance(ready, str):
        # Plain text and a JSON string with the same characters stay distinct.
        return b"t:" + ready.encode("utf-8")
    return b"j:" + json.dumps(
        ready, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


class _Keyring:
    """One trace's secret key and public scope. Never serialized."""

    __slots__ = ("key", "scope", "pending")

    def __init__(self) -> None:
        self.key = secrets.token_bytes(32)
        self.scope = secrets.token_hex(8)
        self.pending: dict[str, dict[str, Any]] = {}

    def digest(self, value: Any) -> str:
        return hmac.new(self.key, _canonical(value), hashlib.sha256).hexdigest()[:32]

    def side(self, value: Any) -> dict[str, Any]:
        result: dict[str, Any] = {"digest": self.digest(value)}
        if isinstance(value, list | tuple) and len(value) <= MAX_ITEMS:
            result["items"] = [self.digest(item) for item in value]
        return result


def _keyring(trace: Any) -> _Keyring:
    keyring = getattr(trace, "_content_keyring", None)
    if keyring is None:
        keyring = _Keyring()
        trace._content_keyring = keyring
    return keyring


def _record(trace: Any, input: Any, output: Any, required: bool) -> dict[str, Any]:
    if input is _MISSING and output is _MISSING:
        raise TypeError("record at least one of input or output")
    if type(required) is not bool:
        raise TypeError("required must be a boolean")
    keyring = _keyring(trace)
    record: dict[str, Any] = {"schema": CONTENT_SCHEMA, "scope": keyring.scope}
    if input is not _MISSING:
        record["input"] = keyring.side(input)
    if output is not _MISSING:
        record["output"] = keyring.side(output)
    record["required"] = required
    return record


def content_metadata(
    *,
    input: Any = _MISSING,
    output: Any = _MISSING,
    required: bool = False,
    trace: Any = None,
) -> dict[str, Any]:
    """Return ``{"agentloop.content": record}`` to merge into a span's metadata.

    Use it where an integration records a span explicitly, for example with
    ``record_model_call(..., metadata={**metadata, **content_metadata(...)})``.
    ``trace`` defaults to the active trace and must be the trace the span is
    recorded into, or the digests cannot be compared with its other spans.
    """
    from agentloop.tracer import _require_trace

    target = trace if trace is not None else _require_trace()
    return {CONTENT_KEY: _record(target, input, output, required)}


def record_content(
    *,
    input: Any = _MISSING,
    output: Any = _MISSING,
    required: bool = False,
    span_id: str | None = None,
) -> bool:
    """Fingerprint a step's input and output and attach them to one span.

    ``input`` is the value the step works on, not necessarily its whole prompt:
    for a "check the answer" call it is the draft answer, for a retrieval it is
    the query. ``output`` is the value the step produced. Pass
    ``required=True`` for a step that must run whatever it returns, such as a
    mandated check; content rules never flag such a span.

    Without ``span_id`` the record goes on the span that is active now, for use
    inside ``trace_model_call``, ``trace_tool_call`` or a traced function. With
    ``span_id`` it goes on that span of the active trace, which may already have
    been recorded. Returns ``False``, recording nothing, when there is no active
    trace or span. Unsupported value types raise ``TypeError``.
    """
    from agentloop.tracer import current_event_id, current_trace

    trace = current_trace()
    if trace is None:
        return False
    target = span_id if span_id is not None else current_event_id()
    if target is None:
        return False
    record = _record(trace, input, output, required)
    for event in reversed(trace.events):
        if event.event_id == target:
            event.metadata = {**event.metadata, CONTENT_KEY: record}
            return True
    _keyring(trace).pending[target] = record
    return True


def attach_pending(trace: Any, event: Any) -> None:
    """Move a record made while ``event`` was active onto it as it is recorded."""
    keyring = getattr(trace, "_content_keyring", None)
    if keyring is None or not keyring.pending:
        return
    record = keyring.pending.pop(event.event_id, None)
    if record is not None:
        # A copy, so the caller's metadata dict (often reused) is never edited.
        event.metadata = {**(event.metadata or {}), CONTENT_KEY: record}


def _side(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping) or not set(value) <= {"digest", "items"}:
        return None
    digest = value.get("digest")
    if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
        return None
    items = value.get("items")
    if items is not None and (
        not isinstance(items, list)
        or len(items) > MAX_ITEMS
        or any(not isinstance(item, str) or not _DIGEST.fullmatch(item) for item in items)
    ):
        return None
    return {"digest": digest, **({"items": list(items)} if items is not None else {})}


def read_content(metadata: Any) -> dict[str, Any] | None:
    """Return a span's validated content record, or ``None`` when absent or malformed."""
    if not isinstance(metadata, Mapping):
        return None
    value = metadata.get(CONTENT_KEY)
    if (
        not isinstance(value, Mapping)
        or value.get("schema") != CONTENT_SCHEMA
        or not isinstance(value.get("scope"), str)
        or not _SCOPE.fullmatch(value["scope"])
        or type(value.get("required")) is not bool
        or not set(value) <= {"schema", "scope", "input", "output", "required"}
    ):
        return None
    result: dict[str, Any] = {"scope": value["scope"], "required": value["required"]}
    for key in ("input", "output"):
        if key in value:
            side = _side(value[key])
            if side is None:
                return None
            result[key] = side
    if "input" not in result and "output" not in result:
        return None
    return result
