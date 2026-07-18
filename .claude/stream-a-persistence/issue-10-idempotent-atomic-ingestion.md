# Issue #10 — Make trace ingestion idempotent and atomic across persistence backends

- **Priority:** P1 · **Effort:** L · **Labels:** bug, python
- **Link:** https://github.com/dipeshbabu/agentloop/issues/10
- **Depends on:** #22 (migration rails for the new uniqueness constraint).

## Problem

Saving the same `(project_id, run_id)` upserts the trace but **appends a new usage event
every time**, and trace/usage/diagnosis writes happen in separate transactions. Retries
inflate run counts, token totals, and modeled cost, and a mid-save failure can leave a
trace without its expected usage/findings.

## Reproduction

```python
store.save_trace(trace, project_id="p")
store.save_trace(trace, project_id="p")
assert len(store.list_traces("p")) == 1
print(store.usage_summary("p")["run_count"])  # currently 2, should be 1
```

## Key files

- `agentloop/store.py:261` — SQLite `save_trace()` upsert, then `record_usage()` + `save_diagnosis()` at 301-302.
- `agentloop/store.py:569`, `602-603` — Postgres equivalent.
- `agentloop/store.py:454-475`, `776-791` — both `record_usage()` always INSERT.
- `usage_events` has no uniqueness constraint on `(project_id, run_id)`.

## Approach

1. **Decide semantics** (state it in the changelog): one run ID = one *current* usage
   snapshot (upsert), matching the existing trace upsert. This is the intended fix.
2. **Add a uniqueness constraint** on `usage_events(project_id, run_id)` via a #22 migration,
   and make `record_usage()` upsert (`ON CONFLICT ... DO UPDATE` / SQLite `ON CONFLICT`)
   instead of insert, in both backends.
3. **Make the whole save atomic.** Wrap trace upsert + usage upsert + diagnosis save in a
   single transaction per `save_trace()` call in each backend, so they commit or roll back
   as one unit. Use one connection/transaction, not three.
4. **Preserve cross-project conflict behavior.** A run ID owned by another project must still
   raise the existing conflict error — verify this path is inside/around the transaction
   correctly.

## Acceptance criteria (from the issue)

- [ ] Re-saving the same project/run does not double-count usage.
- [ ] A run ID owned by another project still returns the existing conflict behavior.
- [ ] Trace, usage, and diagnosis changes commit or roll back as one logical operation.
- [ ] Failures cannot leave a trace without its expected usage/findings state.
- [ ] SQLite and Postgres share the same contract tests, including retry and injected-failure cases.
- [ ] Any schema constraint or migration is documented for existing stores.

## Testing

- Add to the #22 contract suite: double-save idempotency, cross-project conflict,
  injected failure mid-save (assert full rollback), retry after simulated lost response.

## Compatibility / risk

- Existing stores may already contain duplicate `usage_events` rows — the migration adding
  the constraint must de-duplicate first (keep latest) or it will fail to apply.
- `usage_summary` and dashboard counts change for anyone who previously double-saved;
  note it in the changelog.
