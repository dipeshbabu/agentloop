# Native trace schema & compatibility

AgentLoop's serialized trace JSON is a **public compatibility surface**: the CLI,
the HTTP API, both persistence backends, and the OTLP/Vercel adapters all read
and write it through one contract defined in [`agentloop/schema.py`](../agentloop/schema.py).
This document is the reference for that contract and how it evolves.

## Versioning

Every trace serialized by `AgentTrace.to_dict()` / `export_json()` carries a
`schema_version` string:

```json
{
  "schema_version": "1.0",
  "name": "research-agent",
  "run_id": "run_1a2b3c…",
  "started_at": "2026-01-01T00:00:00+00:00",
  "ended_at": "2026-01-01T00:00:07+00:00",
  "elapsed_ms": 7000.0,
  "metadata": {},
  "events": [ … ]
}
```

The version is `MAJOR.MINOR`:

- **MAJOR** increments only for a breaking change to the serialized shape.
  A reader rejects a trace whose MAJOR is greater than the version it supports
  (`schema_version` newer than this build → `422` / `TraceValidationError`).
- **MINOR** increments for backward-compatible additions. Older readers keep
  working because unknown fields are ignored (see below).

Current version: **`1.0`**.

## Event fields

Each event object has these fields. Required fields must be present and non-null.

| Field | Type | Rule |
|---|---|---|
| `event_id` | string | required, non-empty, unique within the trace |
| `run_id` | string | required, must equal the trace's `run_id` |
| `event_type` | string | required, non-empty |
| `name` | string | required, non-empty |
| `started_at` / `ended_at` | string | required, non-empty (ISO-8601) |
| `duration_ms` | number | required, finite, ≥ 0 |
| `input_tokens` / `output_tokens` | integer | ≥ 0 |
| `status` | string | one of `ok`, `error` |
| `parent_id`, `model`, `input_text`, `output_text`, `error` | string \| null | optional |
| `metadata` | object | JSON object |

Validation failures raise `agentloop.schema.TraceValidationError`, which carries
the offending `field` path (for example `events[2].duration_ms`) and a `reason`.
Core code stays framework-free; the HTTP server maps this error to a `422`
response `{"detail": {"field": …, "reason": …}}`.

## Compatibility policy

### Backward compatibility (reading older traces)

Traces written before this schema existed (the 0.4 era) have **no
`schema_version`**. Readers treat a missing version as compatible and load them
unchanged — there is no migration step. This is covered by round-trip tests.

### Forward compatibility (unknown fields)

**Policy: ignore.** Unknown top-level trace fields and unknown event fields from
a newer MINOR version are **dropped on read**, never rejected. This lets a newer
producer add fields without breaking an older reader.

If you need custom data to survive a round trip, put it in `metadata` — that
object is always preserved end to end. Transport bookkeeping keys that AgentLoop
itself sets during OTLP interop (for example `otel_span_id`) are reserved and are
not re-exported as user metadata.

## OTLP interop

The OTLP adapter ([`agentloop/otel.py`](../agentloop/otel.py)) exposes two import
entry points with an explicit single-vs-batch contract:

- **`trace_from_otel(payload, name=None) -> AgentTrace`** — for a payload that
  describes exactly one trace. If the payload contains spans from more than one
  `traceId` (a batch), it raises `TraceValidationError` rather than collapsing
  the traces. An empty payload yields an empty trace.
- **`traces_from_otel(payload, name=None) -> list[AgentTrace]`** — for batches.
  Spans are grouped by `traceId` (in first-seen order, regardless of how they are
  interleaved) into one trace each. Spans without a trace id are grouped into a
  single trace. Parent links are resolved within a trace only, so a repeated span
  id in two traces never links across the boundary.

Both accept the standard OTLP shape (`resourceSpans → scopeSpans → spans`), a
`{"spans": [...]}` object, or a raw list of span dicts.

### Identity & metadata round trips

`trace_to_otel()` writes the native trace name and run id as **resource**
attributes (`agentloop.trace.name`, `agentloop.run_id`) and the native
event/parent ids as span attributes (`agentloop.native_event_id`,
`agentloop.native_parent_id`). On import, `trace_from_otel()` reads these back to
restore AgentLoop-native identity, so a native trace preserves its name, run id,
event ids, parent structure, and event metadata across one **and repeated** OTLP
round trips. Event metadata is exported under the `agentloop.metadata.` namespace
and decoded exactly once on import. Third-party resource/span attributes on a
non-AgentLoop OTLP payload are preserved as event metadata without collision.
