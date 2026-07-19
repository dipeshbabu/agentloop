# AgentLoop work streams

This directory organizes the open GitHub issues into **independent work streams**.
Each stream owns a distinct set of files/subsystems, so different contributors (or
agents) can take different streams with minimal merge conflicts. Every stream folder
contains:

- a `README.md` — how to approach the stream as a whole (scope, order, conventions,
  cross-stream coordination, definition of done); this is the file to hand to an agent
  before it starts on any issue in the stream; and
- one `issue-<n>-*.md` plan per issue — the concrete, grounded plan for that work item.

Read [`SHARED_CONVENTIONS.md`](SHARED_CONVENTIONS.md) first. It carries the dev workflow,
the priority/effort legend, and the project-wide rules (changelog, both-backend tests,
compatibility surfaces) that every plan assumes.

## Status (as of 2026-07-19)

Streams **A, B, F, and G are done** — every issue in scope is closed and shipped
(A/B/F/G's original issues went out in the `v0.5.0` release; Stream G also grew two
follow-on pieces of work, #52 and repo-hardening, both closed — see the Stream G
README). Their `README.md`/`issue-*.md` files stay in this directory as the historical
record of what was built and why; **do not re-derive plans from them for new work** —
read the current code instead, since it has moved on from what those plans describe.

**Stream D is partially done**: #12 is closed; #20 is still open.

**Streams C and E have not been started**: #13 and #21 are both still open.

If you're picking up new work, start from the "What's actually left" table below, not
the full stream map — it's the accurate, current picture.

| Stream | Status | Remaining issues |
|---|---|---|
| A — Persistence & data APIs | ✅ Done | none |
| B — Tracing runtime & integrations | ✅ Done | none |
| C — Trace schema & interop | 🔴 Not started | #13 |
| D — Analysis & reporting correctness | 🟡 Partial | #20 (#12 done) |
| E — Dashboard robustness | 🔴 Not started | #21 |
| F — Docker & deployment | ✅ Done | none |
| G — Release & repo governance | ✅ Done | none |

## Priority & effort legend

**Priority (P0–P2)** — taken verbatim from each issue's `## Priority` line (set by the
issue author).

- **P0** — release blocker. A correct release cannot ship until this is done.
- **P1** — high. Real correctness / reliability / deployment problem; fix soon.
- **P2** — medium. Real but lower urgency (usability, lifecycle, robustness).

**Effort (S / M / L)** — our estimate of scope, not from the repo.

- **S** — small: one file/config, little design; short sitting.
- **M** — medium: one subsystem, some design, a few files; careful tests.
- **L** — large: cross-cutting design touching multiple subsystems; discuss before coding.

## Stream map (original scope, for history)

| Stream | Folder | Owns | Issues |
|---|---|---|---|
| A — Persistence & data APIs | `stream-a-persistence/` | `store.py`, list endpoints, migrations | #22, #10, #19, #31 — all closed |
| B — Tracing runtime & integrations | `stream-b-runtime-integrations/` | `runtime.py`, `integrations/` | #11, #15, #16, #28 — all closed |
| C — Trace schema & interop | `stream-c-schema-interop/` | HTTP trace model, validation | #13 — open |
| D — Analysis & reporting correctness | `stream-d-analysis-reporting/` | `costs.py`, `quality.py`, replay gates | #12 closed, #20 open |
| E — Dashboard robustness | `stream-e-dashboard/` | `dashboard/` (Streamlit) | #21 — open |
| F — Docker & deployment | `stream-f-docker-deployment/` | `Dockerfile`, `docker-compose.yml`, docker CI | #23, #24 — all closed |
| G — Release & repo governance | `stream-g-release-governance/` | release workflow, packaging, GitHub settings | #26, #25, #27 — all closed, plus #52 (release automation) |

## What's actually left

Only three issues remain, across two streams that never started and one that's half
done. They don't touch any file that A/B/F/G changed, so there's no rebasing concern —
just build on top of current `main`, not on the (now superseded) assumptions in the
"Recommended global sequencing" section below.

1. **#13 (Stream C)** — versioned trace schema + 4xx validation. Largest of the three
   (**L**). Touches `agentloop/server.py`'s *ingest* endpoints — Stream A's pagination
   work (#19, done) already changed that file's *list* endpoints (`GET /traces`,
   `GET /findings`) and added `DEFAULT_PAGE_SIZE`/`MAX_PAGE_SIZE`/`InvalidCursorError`
   imports from `agentloop.store`. Different endpoints, but read the current
   `server.py` before planning #13's error-handling shape so its 4xx conventions sit
   next to, not in tension with, the `InvalidCursorError` → 400 mapping pagination
   already established there.
2. **#20 (Stream D)** — provider-aware, explicit cost estimates. Contained to
   `costs.py`/`metrics.py`, independent of everything else that's shipped.
3. **#21 (Stream E)** — handle invalid dashboard inputs without crashing. Stream A's
   pagination and finding-lifecycle work already added a page-size control and a
   status-transition form to `dashboard/app.py`'s Traces and Optimization Queue pages
   (`ALLOWED_FINDING_TRANSITIONS`, `FindingNotFoundError`, `FindingTransitionError`
   imported from `agentloop.store`). #21 should harden those same input paths — a
   malformed page-size or an invalid status choice should already be structurally
   impossible given Streamlit's typed widgets, but confirm that during #21 rather than
   assuming it; the point of #21 is exactly this class of bug.

## Recommended global sequencing (historical — A/B/F/G already followed this)

1. **Stream G first-movers (#26, #27)** and **Stream F (#23)** are the cheapest, highest-
   leverage unblockers — do them early. *(Done.)*
2. **Stream A #22** (migrations + Postgres test harness) is a prerequisite that makes
   #10, #19, #31 — and the schema change in #31 — safe. Start it before the other
   Stream A work and before any schema-touching work elsewhere. *(Done — see
   `agentloop/migrations.py` and `tests/test_store_contract.py`.)*
3. Streams B, C, D, E are largely independent and can run in parallel. *(B done; C, D's
   #20, and E are the only ones left, and remain independent of each other.)*

## Cross-stream coordination

- ~~**#19 (A)** and **#13 (C)** both edit `agentloop/server.py`, but different endpoints
  (list vs. ingest). Rebase often if run concurrently.~~ **Resolved**: #19 shipped.
  #13 now starts from the post-#19 `server.py` — no concurrent rebasing needed, just
  build on current `main` (see "What's actually left" above for the specific overlap
  to be aware of).
- ~~**#19 (A)** and **#21 (E)** both touch how the dashboard renders trace/finding
  lists.~~ **Resolved**: #19 shipped, including the dashboard pagination/status-
  transition UI. #21 builds on top of that UI rather than coordinating around it in
  flight — see "What's actually left" above.
- ~~**#31 (A)** schema change must land on top of **#22 (A)**'s migration system.~~
  **Resolved**: both shipped, in the correct order.
- ~~**#24 (F)** edits `.github/workflows/ci.yml`; **#25 (G)** edits `release.yml` —
  separate files, safe together, but both are CI changes so review them as a pair.~~
  **Resolved**: both shipped. Note for future CI/release workflow changes: `ci.yml` and
  `release.yml` have since gained more cross-references (`release.yml` calls `ci.yml`
  via `workflow_call`; the release-automation workflows added by #52 dispatch `ci.yml`,
  `codeql.yml`, and `dependency-review.yml` directly) — anyone editing any of these four
  workflow files now should check the others, not just `ci.yml`/`release.yml`.
- Anything that changes trace JSON, HTTP shapes, env vars, or DB records is a
  **compatibility surface** — see `SHARED_CONVENTIONS.md`. This still applies to #13
  and #20.
