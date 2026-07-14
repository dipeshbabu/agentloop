"""Map arbitrary AgentLoop identifiers to valid OpenTelemetry trace/span ids.

OTLP requires trace ids to be 32 lowercase hex characters and span ids 16, with
a non-zero value. AgentLoop lets callers (and the API/local constructors) pick
custom run/event ids that may contain a prefix, non-hex characters, separators,
or all-zero values — the smoke script even uses a hyphenated run id. Exporting
those verbatim produced invalid ids that conforming OTLP consumers reject.

These helpers produce a valid, deterministic id for any native identifier while
leaving an already-valid id untouched, so:

- ids the tracer/API already emit as hex keep working;
- a valid OTLP id imported into AgentLoop round-trips back out unchanged;
- arbitrary Unicode/path-like/short/long/all-zero ids map deterministically and
  distinct native ids do not collide (SHA-256 of the original string).

The exporters keep the original native id in span attributes so a remapped id
stays diagnosable.
"""

from __future__ import annotations

import hashlib

_HEX_DIGITS = frozenset("0123456789abcdef")
_TRACE_WIDTH = 32
_SPAN_WIDTH = 16
_TRACE_PREFIXES = ("run_", "trace_")
_SPAN_PREFIXES = ("span_", "evt_")


def is_valid_trace_id(value: str) -> bool:
    """True if ``value`` is a valid OTLP trace id (32 lowercase hex, non-zero)."""
    return _is_valid(value, _TRACE_WIDTH)


def is_valid_span_id(value: str) -> bool:
    """True if ``value`` is a valid OTLP span id (16 lowercase hex, non-zero)."""
    return _is_valid(value, _SPAN_WIDTH)


def to_trace_id(native: str) -> str:
    """Return a valid 32-char lowercase-hex OTLP trace id for ``native``."""
    return _to_otel_id(native, _TRACE_WIDTH, _TRACE_PREFIXES)


def to_span_id(native: str) -> str:
    """Return a valid 16-char lowercase-hex OTLP span id for ``native``."""
    return _to_otel_id(native, _SPAN_WIDTH, _SPAN_PREFIXES)


def _is_valid(value: str, width: int) -> bool:
    return (
        len(value) == width
        and _HEX_DIGITS.issuperset(value)
        and set(value) != {"0"}
    )


def _to_otel_id(native: str, width: int, prefixes: tuple[str, ...]) -> str:
    stripped = _strip_prefix(str(native), prefixes).lower()
    # Already a valid id (e.g. a real OTLP id, or one round-tripped through
    # import) — preserve it exactly.
    if _is_valid(stripped, width):
        return stripped
    # A shorter non-zero hex id (the tracer emits uuid4().hex[:16]) is padded on
    # the left, matching the historical, collision-free mapping for fixed-width
    # native ids.
    if 0 < len(stripped) <= width and _HEX_DIGITS.issuperset(stripped) and set(stripped) != {"0"}:
        return stripped.rjust(width, "0")
    # Anything else (non-hex, too long, empty, or all-zero) is hashed
    # deterministically from the original string so distinct ids stay distinct.
    return _hash_id(native, width)


def _strip_prefix(value: str, prefixes: tuple[str, ...]) -> str:
    for prefix in prefixes:
        if value.startswith(prefix):
            return value[len(prefix):]
    return value


def _hash_id(native: str, width: int) -> str:
    digest = hashlib.sha256(str(native).encode("utf-8")).hexdigest()[:width]
    # SHA-256 can't realistically be all-zero, but OTLP forbids it, so guard.
    return ("1" + digest[1:]) if set(digest) == {"0"} else digest
