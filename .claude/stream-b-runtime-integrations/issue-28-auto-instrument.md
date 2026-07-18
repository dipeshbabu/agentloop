# Issue #28 — Rename or implement the auto-instrument command

- **Priority:** P2 · **Effort:** S · **Labels:** enhancement, good first issue, python
- **Link:** https://github.com/dipeshbabu/agentloop/issues/28
- Good first issue — smallest item in the stream.

## Problem

`auto_instrument()` only checks whether optional packages are importable and appends their
names to `result.enabled`. It does **not** wrap/patch/register anything. And the CLI command
runs in a short-lived separate process, so it can't instrument the caller's app anyway — yet
it reports integrations as "enabled." Accepted `**kwargs` are unused.

## Key files

- `agentloop/autoinstrument.py:29-55` — only calls `_module_exists()`, updates `enabled`/`skipped`; unused `**kwargs`.
- `agentloop/cli.py:514-521` — exposes it as `agentloop auto-instrument`.
- Tests assert only that the return shape is structured, not that anything is traced.

## Approach — pick one contract

**Recommended (matches the "detection-only" reality, small):**
1. Rename the capability to **integration detection / readiness**; report `available`
   instead of `enabled`. Update the CLI command name/help, `result` schema, `doctor` output,
   README, and `docs/INTEGRATIONS.md` to consistent terms.
2. Avoid unnecessary heavy imports where practical (detect via `importlib.util.find_spec`
   rather than importing the framework).
3. Remove the unused `**kwargs` (or give them documented behavior).
4. Keep repeated calls idempotent; report unsupported/partial capabilities precisely.
5. Add migration guidance for users of the existing public function/command.

**Alternative (larger):** implement a real in-process startup API that performs documented
registration/wrapping, called from application startup (a standalone CLI cannot mutate
another process). If chosen, an end-to-end test must show a trace actually recorded.

## Acceptance criteria (from the issue)

- [ ] Command/API naming accurately describes observable behavior.
- [ ] A detection-only path checks availability without unnecessary heavy imports where practical.
- [ ] A real instrumentation path, if implemented, records a trace in an end-to-end test.
- [ ] Repeated calls are idempotent.
- [ ] Unsupported frameworks and partial capabilities reported precisely.
- [ ] Unused arguments removed or given documented behavior.
- [ ] README, integration guide, doctor output, CLI help, and result schema use consistent terms.
- [ ] Migration guidance covers users of the existing public function and command.

## Testing

- `tests/test_doctor_autoinstrument.py`: detection reports `available` correctly with a
  package present/absent (monkeypatch `find_spec`); idempotent repeated calls.

## Compatibility / risk

- Renaming the command and result fields is a CLI/output compatibility change — document the
  old→new mapping in the changelog and keep a deprecation note if feasible.
