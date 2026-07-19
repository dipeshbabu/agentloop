# Stream B — Tracing runtime & SDK integrations

> **Status: 🟡 Follow-up required.** The original four issues shipped in `v0.5.0`,
> but #60, #61, #62, and #64 are open follow-up defects in tracing lifecycle,
> credential isolation, and integration error propagation.

## Scope

The trace finalization runtime, decorators, public client, and optional framework/SDK
adapters. Work lands in **`agentloop/runtime.py`**, **`agentloop/tracer.py`**,
**`agentloop/decorators.py`**, **`agentloop/client.py`**, and
**`agentloop/integrations/`** (with `agentloop/cli.py`, `agentloop/doctor.py`, and
`docs/INTEGRATIONS.md` touch points).

Completed: **#11** (independent finalization side effects), **#15** (idempotent and
stream-aware OpenAI), **#16** (per-trace OpenAI Agents processor state), **#28**
(auto-instrumentation). Open: **#60** (generator lifecycle), **#61** (async
cancellation), **#62** (client credential isolation), and **#64** (OpenAI Agents errors).

## Current follow-up work

- **#60** and **#61** overlap in native tracing, decorators, and LangGraph wrappers.
  Sequence or rebase their separate PRs so generator cleanup and cancellation semantics
  agree.
- **#64** is focused on the OpenAI Agents processor, but it must use the same failed-
  outcome contract as #61.
- **#62** is largely independent. Ordinary client calls must never carry the admin key,
  and admin operations must not carry the project key.

## Historical approach for the original scope

These four are **independent** of each other — take them in any order. Suggested order by
value/difficulty: **#28** (small, mostly naming) → **#11** (contained runtime fix) → **#16**
(per-trace state) → **#15** (streaming lifecycle is the subtlest).

## Stream-specific rules

- **Integrations import their SDK lazily** and are tested with **protocol-compatible fakes**,
  never a live OpenAI/Agents SDK. Follow the existing test doubles in
  `tests/test_openai_integration.py` and `tests/test_otel_interop.py`.
- **Preserve wrapped behavior:** return values, exceptions, sync/async semantics, and
  method signatures must pass through unchanged. This is the hardest constraint in #15.
- **Idempotent wrapping:** instrumenting the same object twice must be a no-op (#15, and the
  general pattern for #16/#28). Use a marker attribute on wrapped callables/resources.
- **Degrade gracefully** when optional usage metadata is absent, and decide what happens for
  calls made with no active trace.
- Core code stays framework-free — keep FastAPI/SDK types at the boundary.

## Order & dependencies

Keep every open issue in a self-contained PR. #60 and #61 share lifecycle code and should
not be edited concurrently without coordination. #61 and #64 share status/error semantics.
#62 can proceed independently.

## Definition of done for the stream

Every original and follow-up issue's acceptance criteria are met; tests use fakes and
cover sync, async, generator, error, close, and cancellation paths where relevant;
credentials are endpoint-scoped; `docs/INTEGRATIONS.md` reflects user-visible behavior;
and the changelog is updated.

See [`../SHARED_CONVENTIONS.md`](../SHARED_CONVENTIONS.md).
