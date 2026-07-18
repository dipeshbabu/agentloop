# Issue #13 — Define a versioned trace schema and return 4xx errors for invalid payloads

- **Priority:** P1 · **Effort:** L · **Labels:** bug, enhancement, python
- **Link:** https://github.com/dipeshbabu/agentloop/issues/13
- **Coordinate with:** #19 (Stream A) — shared `server.py`.

## Problem

`TracePayload.events` is `list[dict[str, Any]]`; the endpoint calls `AgentTrace.from_dict()`,
which passes serialized dicts straight into dataclass constructors. Missing/extra/negative/
inconsistent/wrongly-typed event fields raise **uncaught exceptions (HTTP 500)** or corrupt
metrics. Serialized traces also carry **no schema version**, so future additive fields break
old readers.

## Reproduction (both return 500 today)

- `POST /traces` with `events: [{"unexpected": true}]`
- `POST /quality-report` with a fixture whose `scorer` is a string

## Key files

- `agentloop/server.py:38` — `TracePayload.events: list[dict[str, Any]]`.
- `agentloop/server.py:120` — endpoint calls `AgentTrace.from_dict()`.
- `agentloop/tracer.py:49-53`, `agentloop/events.py:48-49` — `from_dict()` pass dicts into constructors.

## Approach

1. **Publish a versioned native schema.** Add a `schema_version` to serialized traces;
   document the current version and the compatibility policy.
2. **Typed request/event models with semantic constraints.** Validate: event-to-trace run ID
   consistency, unique identifiers, timestamps, non-negative numeric fields, supported
   statuses, JSON-compatible metadata.
3. **Structured 4xx conversion.** Have core `from_dict()` raise typed domain/validation errors;
   the server maps them to 4xx responses that name the offending field and reason (do not let
   FastAPI types leak into core).
4. **0.4 back-compat path.** Valid existing 0.4 trace files must remain readable through a
   documented compatibility shim.
5. **Unknown-field policy.** Choose and document preserve / ignore / reject for unknown future
   fields, and apply it uniformly.
6. **One shared contract** across CLI import, HTTP ingestion, Vercel/OTLP adapters, both
   stores, and round-trip tests.

## Acceptance criteria (from the issue)

- [ ] Malformed trace and quality payloads never produce an unhandled 500.
- [ ] Validation responses identify the field and reason.
- [ ] Valid existing 0.4 trace files remain readable through a documented compatibility path.
- [ ] Unknown future fields follow an explicit preserve/ignore/reject policy.
- [ ] CLI import, HTTP ingestion, Vercel/OTLP adapters, both stores, and round-trip tests use the same schema contract.
- [ ] The schema and compatibility policy are documented for contributors and integrators.

## Testing

- `tests/test_server.py`: malformed `/traces` and `/quality-report` payloads → 4xx with field
  detail (not 500).
- Round-trip: a 0.4 trace loads, and current traces export→import unchanged.
- Adapter tests (vercel/otel) build traces through the same validated path.

## Compatibility / risk

- Adding `schema_version` changes serialized output — document; keep readers tolerant of its
  absence for 0.4 files.
- Decide the unknown-field policy early; it is hard to change later.
