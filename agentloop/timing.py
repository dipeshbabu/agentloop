from __future__ import annotations

import math
import time
from datetime import datetime, timezone
from typing import Any, Iterable


def timestamp_ms(value: Any) -> float | None:
    """Parse an ISO-8601 timestamp into milliseconds since the Unix epoch."""

    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    result = parsed.timestamp() * 1000
    return result if math.isfinite(result) else None


def duration_ms(value: Any) -> float:
    """Return a finite, non-negative duration for an event-like object."""

    try:
        result = float(getattr(value, "duration_ms", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return result if math.isfinite(result) and result >= 0 else 0.0


def event_interval_ms(event: Any) -> tuple[float, float] | None:
    """Return a trustworthy timestamp interval for an event, when available.

    Older adapters sometimes filled both timestamps with the current time while
    carrying the real duration in ``duration_ms``. Treat a large disagreement
    between those values as missing timing data so legacy traces can use the
    cumulative-duration fallback instead of reporting an almost-zero runtime.
    """

    started = timestamp_ms(getattr(event, "started_at", None))
    ended = timestamp_ms(getattr(event, "ended_at", None))
    if started is None or ended is None or ended < started:
        return None

    recorded_duration = duration_ms(event)
    observed_duration = ended - started
    if recorded_duration > 0:
        tolerance = max(5.0, recorded_duration * 0.10)
        if abs(observed_duration - recorded_duration) > tolerance:
            return None
    return started, ended


def event_time_bounds(events: Iterable[Any]) -> tuple[float, float] | None:
    """Return the earliest reliable start and latest reliable end.

    A positive-duration event without trustworthy timestamps makes the complete
    trace envelope unknowable, so callers should fall back to cumulative work.
    Zero-duration events without timestamps do not affect either result.
    """

    earliest: float | None = None
    latest: float | None = None
    for event in events:
        interval = event_interval_ms(event)
        if interval is None:
            if duration_ms(event) > 0:
                return None
            continue
        started, ended = interval
        earliest = started if earliest is None else min(earliest, started)
        latest = ended if latest is None else max(latest, ended)
    if earliest is None or latest is None:
        return None
    return earliest, latest


def cumulative_span_time_ms(events: Iterable[Any]) -> float:
    """Return total instrumented work, including overlap and nesting."""

    return sum(duration_ms(event) for event in events)


def elapsed_runtime_ms(trace: Any) -> float:
    """Resolve end-to-end trace duration with backward-compatible fallbacks."""

    explicit = _non_negative_float(getattr(trace, "elapsed_ms", None))
    if explicit is not None:
        return explicit

    if getattr(trace, "_timing_active", False):
        started_perf = _non_negative_float(getattr(trace, "_start_perf", None))
        if started_perf is not None:
            return max(0.0, (time.perf_counter() - started_perf) * 1000)

    trace_started = timestamp_ms(getattr(trace, "started_at", None))
    trace_ended = timestamp_ms(getattr(trace, "ended_at", None))
    if trace_started is not None and trace_ended is not None and trace_ended >= trace_started:
        return trace_ended - trace_started

    events = list(getattr(trace, "events", []))
    bounds = event_time_bounds(events)
    if bounds is not None:
        earliest, latest = bounds
        if trace_started is not None and trace_started <= latest:
            earliest = min(earliest, trace_started)
        return max(0.0, latest - earliest)

    return cumulative_span_time_ms(events)


def _non_negative_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(result) or result < 0:
        return None
    return result
