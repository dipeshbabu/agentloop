# Stream B — Tracing runtime & SDK integrations

## Scope

The trace finalization runtime and the optional framework/SDK adapters. Work lands in
**`agentloop/runtime.py`**, **`agentloop/integrations/openai.py`**,
**`agentloop/integrations/openai_agents.py`**, and **`agentloop/autoinstrument.py`**
(with `agentloop/cli.py`, `agentloop/doctor.py`, and `docs/INTEGRATIONS.md` touch points).

Issues: **#11** (independent finalization side effects), **#15** (idempotent + stream-aware
OpenAI), **#16** (per-trace OpenAI Agents processor state), **#28** (rename/implement
auto-instrument).

## Approach for the stream as a whole

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

No inter-issue dependencies. Each is a self-contained PR. #15 and #16 both touch the OpenAI
integration area but different files (`openai.py` vs `openai_agents.py`), so they can go in
parallel.

## Definition of done for the stream

Each issue's acceptance criteria met; new tests use fakes and cover sync + async + error +
cancellation paths where relevant; `docs/INTEGRATIONS.md` updated for any user-visible
change (#28 especially); changelog updated.

See [`../SHARED_CONVENTIONS.md`](../SHARED_CONVENTIONS.md).
