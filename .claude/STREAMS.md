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

## Stream map

| Stream | Folder | Owns | Issues |
|---|---|---|---|
| A — Persistence & data APIs | `stream-a-persistence/` | `store.py`, list endpoints, migrations | #22, #10, #19, #31 |
| B — Tracing runtime & integrations | `stream-b-runtime-integrations/` | `runtime.py`, `integrations/` | #11, #15, #16, #28 |
| C — Trace schema & interop | `stream-c-schema-interop/` | HTTP trace model, validation | #13 |
| D — Analysis & reporting correctness | `stream-d-analysis-reporting/` | `costs.py`, `quality.py`, replay gates | #12, #20 |
| E — Dashboard robustness | `stream-e-dashboard/` | `dashboard/` (Streamlit) | #21 |
| F — Docker & deployment | `stream-f-docker-deployment/` | `Dockerfile`, `docker-compose.yml`, docker CI | #23, #24 |
| G — Release & repo governance | `stream-g-release-governance/` | release workflow, packaging, GitHub settings | #26, #25, #27 |

## Recommended global sequencing

1. **Stream G first-movers (#26, #27)** and **Stream F (#23)** are the cheapest, highest-
   leverage unblockers — do them early.
2. **Stream A #22** (migrations + Postgres test harness) is a prerequisite that makes
   #10, #19, #31 — and the schema change in #31 — safe. Start it before the other
   Stream A work and before any schema-touching work elsewhere.
3. Streams B, C, D, E are largely independent and can run in parallel.

## Cross-stream coordination

- **#19 (A)** and **#13 (C)** both edit `agentloop/server.py`, but different endpoints
  (list vs. ingest). Rebase often if run concurrently.
- **#19 (A)** and **#21 (E)** both touch how the dashboard renders trace/finding lists.
- **#31 (A)** schema change must land on top of **#22 (A)**'s migration system.
- **#24 (F)** edits `.github/workflows/ci.yml`; **#25 (G)** edits `release.yml` — separate
  files, safe together, but both are CI changes so review them as a pair.
- Anything that changes trace JSON, HTTP shapes, env vars, or DB records is a
  **compatibility surface** — see `SHARED_CONVENTIONS.md`.
