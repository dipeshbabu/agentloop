# Stream C — Trace schema & interop

> **Status: 🟢 Resolved on branch `stream-c-schema-interop` (pending PR review/merge).**
> #13 (versioned schema + 4xx validation), #40 (batched OTLP trace boundaries), and
> #63 (native identity/metadata across OTLP round trips) are all implemented with tests
> and docs on this branch. #17's OTLP identifier work was already complete. Once the PR
> merges and the issues close on GitHub, move this folder into [`../done/`](../done/).

## Scope

The native trace schema, HTTP payload validation, and turning malformed input into proper
4xx errors. Work centers on **`agentloop/server.py`**, **`agentloop/tracer.py`**,
**`agentloop/events.py`**, and the `from_dict()` conversion path, plus the adapters that
build traces (`integrations/vercel_ai.py`, `otel.py`).

Open: **#13** (versioned trace schema + 4xx validation), **#40** (batched OTLP trace
boundaries), and **#63** (AgentLoop identity and metadata across OTLP round trips).
Completed: **#17** (valid OTLP identifiers, merged in PR #37).

## Approach for the stream as a whole

#13 is an **L** compatibility-defining decision. Read its acceptance criteria carefully
and settle the **unknown-field policy** (preserve / ignore / reject) and the **0.4
back-compat path** before changing the native schema.

#40 and #63 are separate defects but both center on `agentloop/otel.py`. Implement them
as separate PRs and sequence them: #40 establishes the multi-trace import contract, while
#63 preserves native identity/metadata within each imported trace. Both must remain
compatible with #13's schema policy.

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
- #40 and #63 edit the same import/export helpers. Do not develop them concurrently
  without rebasing and rerunning the combined round-trip suite.

## Definition of done for the stream

#13, #40, and #63 meet their acceptance criteria; a published, versioned schema and
documented compatibility policy exist; malformed payloads cannot produce an unhandled
500; multi-trace batches preserve boundaries; repeated OTLP round trips preserve native
identity and metadata; and changelog/contributor docs are updated.

See [`../SHARED_CONVENTIONS.md`](../SHARED_CONVENTIONS.md).
