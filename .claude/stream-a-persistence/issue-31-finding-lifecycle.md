# Issue #31 — Add finding lifecycle operations and preserve status during re-diagnosis

- **Priority:** P2 · **Effort:** L · **Labels:** enhancement, python
- **Link:** https://github.com/dipeshbabu/agentloop/issues/31
- **Depends on:** #22 — the schema change here **must** ship as a versioned migration.

## Problem

Findings have a `status` column and can be filtered by it, but there is no store method,
API endpoint, CLI command, or dashboard action to *change* status. Worse, re-running
diagnosis **deletes and recreates** findings, resetting any status. So queues never close
and any triage decision is lost on re-diagnosis.

## Key files

- `agentloop/store.py:211` (SQLite) / `529` (Postgres) — status defaults to `detected`.
- `agentloop/store.py:339` / `654` — `save_diagnosis()` deletes prior findings and reinserts.
- `agentloop/store.py:404-405` / `727-728` — queue queries exclude resolved findings.
- Dashboard displays status but has no transition control.

## Approach

1. **Define a small state machine.** e.g. `detected → accepted → resolved`, plus `dismissed`
   and `reopened`; document the initial state and reopen behavior. Keep it minimal.
2. **Stable finding identity.** Give findings a deterministic identity (e.g. hash of
   project + trace/run + finding signature) so re-diagnosis can *upsert* and preserve
   review state instead of delete+reinsert. This is the core of the fix.
3. **Change `save_diagnosis()` to upsert/supersede** rather than delete+recreate: unchanged
   findings keep their reviewed status; changed/removed findings follow a documented
   supersession/archive policy (not silent reset).
4. **Add project-scoped lifecycle store methods** (both backends): update status within the
   owning project only; distinguish not-found from forbidden/conflict. Record new status +
   update time (add actor/reason if an audit model is adopted). Ship the columns via a #22
   migration.
5. **Expose the same transition model** through HTTP (`server.py`), `client.py`, `cli.py`,
   and the dashboard.
6. **Keep queue/summary counts live** — transitions must immediately affect queue exclusion
   and counts.

## Acceptance criteria (from the issue)

- [ ] Supported states and allowed transitions documented, including initial state and reopen behavior.
- [ ] Store methods update a finding only within its owning project and distinguish not-found from forbidden/conflicting.
- [ ] API, client, CLI, and dashboard expose the same transition model.
- [ ] A transition records at least new status and update time (actor/reason if audit model adopted).
- [ ] Re-diagnosing an unchanged finding preserves its reviewed state.
- [ ] Changed/removed findings follow a documented supersession/archive policy, not silent reset.
- [ ] Finding identity is stable enough for retries and concurrent updates.
- [ ] Queue and summary counts reflect transitions immediately.
- [ ] SQLite and Postgres contract tests cover valid/invalid transitions, retry idempotency, concurrency, re-diagnosis, and project isolation.
- [ ] Any schema change is delivered through the versioned migration work (#22).

## Testing

- Contract suite (both backends): each valid transition, rejected invalid transition,
  cross-project isolation, retry idempotency, concurrent update, and the key case —
  re-diagnose an unchanged trace and assert reviewed status survives.

## Compatibility / risk

- The delete+reinsert → upsert change alters finding IDs; ensure the dashboard/API don't
  assume the old identity scheme.
- Do not start until #22's migration system exists.
