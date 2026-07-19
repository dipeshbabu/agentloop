# Stream C — Trace schema & interop

## Scope

The native trace schema, HTTP payload validation, and turning malformed input into proper
4xx errors. Work centers on **`agentloop/server.py`**, **`agentloop/tracer.py`**,
**`agentloop/events.py`**, and the `from_dict()` conversion path, plus the adapters that
build traces (`integrations/vercel_ai.py`, `otel.py`).

Issues: **#13** (versioned trace schema + 4xx validation). *(This stream previously also held
#17, "valid OTLP identifiers," which was fixed and merged in PR #37 — done.)*

## Approach for the stream as a whole

Only one open issue remains, but it is an **L**: introducing a versioned schema is a
compatibility-defining decision. Read the acceptance criteria carefully and settle the
**unknown-field policy** (preserve / ignore / reject) and the **0.4 back-compat path** before
writing code — those choices ripple through every adapter and both stores.

## Stream-specific rules

- The native trace JSON is a **compatibility surface**. Existing 0.4 traces must remain
  readable via a documented path.
- Validation must run at the boundary (HTTP) and produce structured 4xx responses — core
  conversion should raise typed domain errors that the server maps to 4xx, keeping core code
  framework-free (no FastAPI types leaking inward).
- The same schema contract must be shared by CLI import, HTTP ingestion, Vercel/OTLP
  adapters, both stores, and round-trip tests — don't validate in one place only.

## Cross-stream coordination

- ~~**#19 (Stream A)** also edits `server.py` (list endpoints vs. this issue's ingest
  endpoints). Rebase frequently if both are in flight.~~ **Resolved**: #19 shipped and
  is on `main` now, not "in flight" — no concurrent rebasing needed. It added
  `DEFAULT_PAGE_SIZE`/`MAX_PAGE_SIZE`/`InvalidCursorError` imports from
  `agentloop.store` and an `InvalidCursorError` → 400 mapping in `server.py`'s list
  endpoints. Different endpoints than #13 touches, but look at that error-mapping
  pattern before inventing a separate one for #13's ingest-side 4xx errors.
- The OTLP ID rules already added by PR #37 (`agentloop/otel_ids.py`) are the model for
  "valid, documented, round-trippable" — mirror that rigor for the schema.

## Definition of done for the stream

#13's acceptance criteria met; a published, versioned schema + documented compatibility
policy; no malformed payload can produce an unhandled 500; changelog + contributor docs updated.

See [`../SHARED_CONVENTIONS.md`](../SHARED_CONVENTIONS.md).
