# Issue #15 — Make OpenAI instrumentation idempotent and stream-aware

- **Priority:** P1 · **Effort:** M–L · **Labels:** bug, python

- **Link:** https://github.com/dipeshbabu/agentloop/issues/15

## Problem

The OpenAI client mutator wraps a method **unconditionally** (so instrumenting twice
double-records), and the generic wrapper records the event in `finally` **as soon as a
streaming call returns its iterator** — before the stream is consumed. So streaming latency
measures only iterator creation and final usage is lost.

## Reproduction

Instrument one client twice + one request → 2 events. A streaming call records its event
before iteration and never updates tokens after consumption (`stream_tokens: [0, 0]`).

## Key files

- `agentloop/integrations/openai.py:135-160` — `instrument_openai_client()` assigns a fresh wrapper unconditionally.
- `agentloop/integrations/openai.py:82-104`, `112-132` — sync/async wrappers record in `finally` around the call.

## Approach

1. **Idempotent wrapping.** Set a marker attribute on wrapped callables/resources; if
   present, return as-is. Repeated instrumentation → one event per request.
2. **Detect streaming responses** (sync + async) without a hard SDK dependency — duck-type
   the iterator/context-manager protocols. Keep the SDK import lazy.
3. **Finalize on stream completion, not creation.** Wrap the returned stream so latency,
   usage, status, and errors are recorded when iteration **closes, fails, or is cancelled** —
   not when the iterator is created. Capture final usage when the SDK provides it.
4. **Preserve types & semantics.** The wrapped stream must still be the SDK's iterator /
   context manager; return types and context-manager behavior unchanged.
5. **Define no-active-trace and early-close behavior** so application calls never break.

## Acceptance criteria (from the issue)

- [ ] Instrumenting the same client/callable more than once records one event per request.
- [ ] Non-streaming sync and async behavior remains unchanged.
- [ ] Streaming duration covers consumption, and final usage is captured when the SDK provides it.
- [ ] Mid-stream errors and cancellation record an error once and propagate unchanged.
- [ ] Early close has defined behavior.
- [ ] Tests use protocol-compatible fakes for sync streams, async streams, context managers, repeated instrumentation, and absent usage metadata.

## Testing

- `tests/test_openai_integration.py`: fake sync stream, fake async stream, context-manager
  stream, double-instrumentation, mid-stream raise, cancellation, and missing-usage cases.

## Compatibility / risk

- Wrapping the stream object is the delicate part — a fake that mimics the SDK's
  `__iter__`/`__aiter__`/`__enter__` protocols is essential; do not import the real SDK in
  tests.
