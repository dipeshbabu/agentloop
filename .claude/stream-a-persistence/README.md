# Stream A — Persistence layer & data APIs

## Scope

This stream owns the storage backends and the list/query APIs built on them. Almost all
work lands in **`agentloop/store.py`** (which holds both `SQLiteTraceStore` and
`PostgresTraceStore`), with touch points in `agentloop/server.py`, `agentloop/client.py`,
`agentloop/cli.py`, and `dashboard/app.py` for the API/CLI/dashboard surfaces.

Issues: **#22** (Postgres contract tests + migrations), **#10** (idempotent/atomic
ingestion), **#19** (pagination), **#31** (finding lifecycle).

## Approach for the stream as a whole

1. **Do #22 first.** It creates two things the rest of the stream depends on: a shared
   backend **contract-test suite** that runs against both SQLite and Postgres, and a
   **versioned migration system** to replace the implicit `CREATE TABLE IF NOT EXISTS`
   schema evolution. Without these, #10/#19/#31 cannot add constraints or columns safely
   and cannot prove parity across backends.
2. **Then #10** (idempotent + atomic save). This defines usage/upsert semantics that #19
   and #31 both build on.
3. **Then #19** (pagination) and **#31** (finding lifecycle) — these are largely
   independent of each other but both sit on the #22 migration rails. #31 adds a schema
   change, so it *must* ship as a migration from #22.

Do the four issues in that order if one person owns the stream. If parallelized, #22 is a
hard prerequisite for the others.

## Stream-specific rules

- **Everything is dual-backend.** Every method you touch exists twice in `store.py`
  (SQLite ~lines 200–500, Postgres ~500–800). Change both and cover both with the shared
  contract suite from #22.
- **Schema changes go through migrations only** (once #22 lands). Never add ad-hoc DDL to
  `init()`.
- **The store is a compatibility surface.** New constraints must have a documented upgrade
  path for existing databases. Do not silently drop or reset stored rows.
- Watch dialect differences: JSONB vs. TEXT JSON, `RETURNING`, row counts, timestamp
  types, `ON CONFLICT` support, autocommit vs. explicit transactions.
- Never let test data or logs print database credentials.

## Order & dependencies

```
#22 (migrations + contract suite)  ← do first, unblocks the rest
   ├── #10 (idempotent/atomic save)
   ├── #19 (pagination)            ← also touches server.py list endpoints; coordinate with #13 (Stream C)
   └── #31 (finding lifecycle)     ← schema change, must be a #22 migration
```

## Definition of done for the stream

All four issues' acceptance criteria met; shared contract tests green on **both** SQLite
and Postgres in CI; a documented migration path for every schema change; production docs
(`docs/PRODUCTION.md`) updated with backup/upgrade/rollback where #22 requires it.

See [`../SHARED_CONVENTIONS.md`](../SHARED_CONVENTIONS.md) for the base workflow and the
both-backend / compatibility rules.
