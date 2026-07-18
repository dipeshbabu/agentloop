# Issue #19 — Add pagination and bounded queries to trace and finding list APIs

- **Priority:** P1 (hosted) · **Effort:** L · **Labels:** enhancement, python
- **Link:** https://github.com/dipeshbabu/agentloop/issues/19
- **Coordinate with:** #13 (Stream C — same `server.py`, different endpoints) and #21
  (Stream E — dashboard list rendering).

## Problem

Trace and finding list methods load **every** matching row, API endpoints return the whole
collection, and the dashboard renders it in one dataframe. No limit, cursor, or bound. As
retention grows this causes large DB reads, high memory, slow responses, and proxy timeouts.

## Key files

- `agentloop/store.py:304-315` (SQLite) / `605-616` (Postgres) — trace queries, no `LIMIT`.
- `agentloop/store.py:378-398` / `685-705` — finding queries, unbounded.
- `agentloop/server.py:138-218` — `GET /traces`, `GET /findings`, optimization queue: no paging params.
- `agentloop/client.py`, `agentloop/cli.py`, `dashboard/app.py` — consumers assume one full list.

## Approach

1. **Storage:** add `limit` + cursor params to the list methods; apply `LIMIT` in SQL and a
   deterministic `ORDER BY` with a stable tie-breaker (e.g. `created_at, id`). Prefer
   **keyset/cursor** pagination over `OFFSET` for Postgres. Add supporting indexes via a
   #22 migration (project + order + filter columns).
2. **API:** add a documented `page_size` (with a safe max) and an opaque continuation
   `cursor`/`next` token to `GET /traces`, `GET /findings`, and the optimization queue
   endpoints. Responses return the page plus the next cursor.
3. **Optimization queue:** give it an explicit bounded window or push the clustering into a
   DB aggregation instead of loading full finding history into Python.
4. **Consumers:** `client`/`cli` can request a limit or iterate pages; dashboard loads an
   initial page and supports "load more"/navigation.
5. **Compatibility path:** keep a documented default behavior for existing callers (e.g.
   default page size) rather than silently truncating.

## Acceptance criteria (from the issue)

- [ ] Trace and finding APIs accept a documented page size with a safe maximum.
- [ ] Responses include a stable continuation cursor/token and deterministic tie-breaking order.
- [ ] Store queries apply `LIMIT` in SQL and have supporting project/order/filter indexes.
- [ ] Optimization queue has an explicit bounded window or database aggregation strategy.
- [ ] CLI can iterate pages or request a limit; dashboard loads an initial page and supports navigation.
- [ ] Existing callers receive a documented compatibility path.
- [ ] SQLite and Postgres tests cover empty, single-page, multi-page, ties, filters, deleted rows, and invalid cursors.
- [ ] Performance tests demonstrate bounded memory/response size on a representative large dataset.

## Testing

- Contract suite (both backends): empty, single page, multi-page, tie ordering, filters,
  rows deleted between pages, invalid/expired cursor → clear error.
- A perf-style test asserting bounded response size on a large seeded dataset.

## Compatibility / risk

- Adding paging changes the shape of `GET /traces` / `GET /findings` responses — document
  in changelog; keep an un-paginated compatibility default if feasible.
- Overlaps `server.py` with #13; coordinate ordering of the two PRs.
