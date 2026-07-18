# Issue #11 — Run trace finalization side effects independently and clear stale errors

- **Priority:** P1 · **Effort:** M · **Labels:** bug, python
- **Link:** https://github.com/dipeshbabu/agentloop/issues/11

## Problem

Export, local storage, and upload are wrapped in **one** `try` block in
`runtime.py`. A failure in an earlier side effect prevents later ones from running, and the
process-global `_last_error` stays set even after a later successful finalization (so
monitoring keeps reporting a recovered failure).

## Reproduction

With all three destinations enabled and an export that raises: `stored` and `uploaded` stay
`false` — neither storage nor upload is attempted. Separately, a failed export followed by a
successful export leaves `get_last_error() == "export failed"`.

## Key files

- `agentloop/runtime.py:128-155` — export/store/upload in one exception boundary; `_last_error`
  assigned only on failure.

## Approach

1. **Give each destination its own error boundary.** Run export, store, and upload in
   separate `try` blocks; collect a per-destination result and error so one failure cannot
   suppress the others when `fail_silently=True`.
2. **Tag errors by destination** in the returned `errors` list/results (which destination
   failed).
3. **Clear stale error state on full success.** After a finalization where all configured
   destinations succeed, clear/replace `_last_error`.
4. **Define non-silent mode.** When `fail_silently=False`, document deterministic ordering
   and which exception propagates, without losing already-completed results.
5. **Explicit clearing of optional config.** Provide a way to actually clear values like
   `export_dir`/`api_key` when reconfiguring (today `None` means "keep old value").

## Acceptance criteria (from the issue)

- [ ] Failure of one destination does not suppress other configured destinations when failures are silent.
- [ ] Results identify the destination for each error.
- [ ] A fully successful finalization clears prior error state.
- [ ] Non-silent mode has documented deterministic ordering and exception behavior.
- [ ] Tests cover every destination failing individually, multiple failures, recovery, and explicit configuration clearing.

## Testing

- `tests/test_runtime.py`: each destination failing alone, multiple failing, recovery clears
  `get_last_error()`, and explicit config-clearing of `export_dir`/`api_key`.

## Compatibility / risk

- The result dict shape may gain per-destination fields — keep existing keys and document
  additions in the changelog.
