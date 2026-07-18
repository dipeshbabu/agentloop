# Issue #22 — Add Postgres contract tests and versioned database migrations

- **Priority:** P1 · **Effort:** L · **Labels:** enhancement, python, github-actions
- **Link:** https://github.com/dipeshbabu/agentloop/issues/22
- **Do first in Stream A** — unblocks #10, #19, #31.

## Problem

`store.py` maintains separate SQLite and Postgres implementations, but tests exercise only
SQLite, and both backends embed schema DDL directly in `init()` via repeated
`CREATE TABLE IF NOT EXISTS`. That cannot safely evolve existing columns/constraints/
indexes, and dialect/transaction/JSONB/timestamp regressions can merge while CI stays green.

## Key files

- `agentloop/store.py:502` — `PostgresTraceStore` start; DDL embedded in each backend's `init()`.
- `tests/test_store.py:16-79` — all persistence tests instantiate `SQLiteTraceStore`; none use Postgres.
- `docs/ROADMAP.md:53` — Postgres parity + migrations already named a reliability priority.
- `.github/workflows/ci.yml` — needs a Postgres service for the new tests.

## Approach

1. **Extract a shared contract suite.** Parametrize a new test module over a
   `store` fixture that yields both a SQLite store and a Postgres store, so one set of
   assertions runs against both. Cover: init, API keys, trace upsert/conflicts, idempotent
   usage, diagnosis/findings, filters, queue ordering, transactions.
2. **Add a Postgres service in CI.** Use a pinned `postgres:<version>` service container in
   the test job, wait for readiness (`pg_isready` / `/readyz`-style loop), no external
   credentials. Gate Postgres-only tests on the service being present so local SQLite-only
   runs still work.
3. **Introduce a lightweight migration system.** A `schema_migrations` (version) table plus
   an ordered list of migration steps that each backend applies in sequence. Prefer a small
   in-repo mechanism over adding Alembic unless justified (keep deps inward — see
   conventions). Migrations must be atomic where the backend supports it and fail with
   actionable recovery guidance.
4. **Replace implicit schema evolution.** Move the current DDL into migration `0001`
   (baseline). A fresh install runs all migrations; an existing DB runs only the pending
   ones. Add a fresh-vs-upgraded equivalence test.
5. **Document ops.** Add backup, upgrade, rollback, and compatibility procedures to
   `docs/PRODUCTION.md`.

## Acceptance criteria (from the issue)

- [ ] Shared contract tests cover init, API keys, trace upsert/conflicts, idempotent usage, diagnosis/findings, filters, queue ordering, and transactions on both backends.
- [ ] CI starts a pinned Postgres version and waits for readiness without external credentials.
- [ ] A schema-version table and ordered migrations replace implicit schema evolution.
- [ ] Fresh install and upgrade from every supported public schema reach equivalent structures/data.
- [ ] Migrations are atomic where supported and fail with actionable recovery guidance.
- [ ] Production docs include backup, upgrade, rollback, and compatibility procedures.
- [ ] Connection failures and concurrent initialization are tested.
- [ ] Test data and logs never expose database credentials.

## Testing

- New `tests/test_store_contract.py` parametrized over both backends.
- Concurrent-`init()` test and a connection-failure test.
- Equivalence test: DB built fresh vs. DB migrated from baseline produce identical schema.

## Compatibility / risk

- The baseline migration must reproduce the *current* schema exactly so existing SQLite/
  Postgres databases are recognized as already-at-baseline, not re-created.
- This is the foundation for #31's schema change — land the migration rails first.
