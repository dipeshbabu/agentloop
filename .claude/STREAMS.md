# AgentLoop work streams

This directory groups GitHub issues into **independent work streams** so different
contributors can work with minimal merge conflicts. GitHub is the live source of truth:
before starting an item, verify that the issue is still open and read its latest comments.

Each stream folder contains:

- a `README.md` describing ownership, coordination, and the stream-wide definition of
  done; and
- `issue-<n>-*.md` plans for the original scoped issues. Follow-up issues discovered by
  later audits are listed in the current backlog below even when they do not yet have a
  separate local plan file.

Read [`SHARED_CONVENTIONS.md`](SHARED_CONVENTIONS.md) first. It defines the development
workflow, priority/effort legend, changelog rules, dual-backend expectations, and
compatibility surfaces that every stream assumes.

## Status snapshot (2026-07-19)

The original work in Streams A, B, F, and G shipped in `v0.5.0`, but later audits
reopened incomplete acceptance criteria and found follow-up defects. Preserve the old
plans as implementation history; use this table and the live GitHub issue state when
choosing new work.

| Stream | Status | Open issues | Completed scope |
|---|---|---|---|
| A — Persistence & data APIs | 🟡 Follow-up | #19, #31 | #10, #22 |
| B — Tracing runtime & integrations | 🟡 Follow-up | #60, #61, #62, #64 | #11, #15, #16, #28 |
| C — Trace schema & interop | 🔴 Open | #13, #40, #63 | #17 |
| D — Analysis & reporting correctness | 🔴 Open | #12, #20, #41 | none |
| E — Dashboard robustness | 🔴 Open | #21 | none |
| F — Docker & deployment | ✅ Done | none | #23, #24 |
| G — Release & repo governance | 🟡 Follow-up | #54 | #25, #26, #27, #52 and repository hardening |

## Current active backlog

### Stream A — Persistence & data APIs

- **#19** — add pagination and bounded queries throughout stores, HTTP APIs, client,
  CLI, optimization queues, and dashboard consumers.
- **#31** — expose a durable finding lifecycle and preserve reviewed state during
  re-diagnosis.

Both issues were reopened after the initial implementation failed parts of their full
acceptance criteria. Start from the current migrations and dual-backend contract suite;
do not reuse the old implementation assumptions without checking the code.

### Stream B — Tracing runtime & integrations

- **#60** — keep generator and async-generator traces active for the iterator lifecycle.
- **#61** — record async cancellation as a non-successful outcome across native tracing
  and integrations.
- **#62** — isolate project and administrator credentials in the public client.
- **#64** — propagate OpenAI Agents span errors into AgentLoop events.

#60 and #61 overlap in tracing/decorator lifecycle code. #61 and #64 also share error
outcome semantics. Keep each issue in a separate PR, but sequence or rebase them rather
than editing the same wrappers concurrently. #62 is largely independent.

### Stream C — Trace schema & interop

- **#13** — define a versioned native trace schema and return structured 4xx errors for
  invalid payloads.
- **#40** — preserve trace boundaries when importing batched OTLP payloads.
- **#63** — preserve AgentLoop identity and metadata across OTLP round trips.

#40 and #63 both center on `agentloop/otel.py`, so they should not be implemented on
parallel branches without coordination. #13 defines the broader serialization contract;
each OTLP change must remain compatible with it.

### Stream D — Analysis & reporting correctness

- **#12** — fail closed for empty, invalid, or failing quality fixtures. This issue was
  reopened because supplied failures can still be ignored by default replay gates.
- **#20** — make cost estimates provider-aware and explicit for unknown models.
- **#41** — make large savings selection optimal or expose approximation semantics.

#20 and #41 both affect reported value and optimization results. Keep their machine-
readable output contracts consistent if they are developed near each other.

### Stream E — Dashboard robustness

- **#21** — validate dashboard inputs inline without crashing Streamlit reruns.

This work must build on the current pagination and finding-lifecycle UI rather than the
pre-`v0.5.0` dashboard described in the original issue plan.

### Stream F — Docker & deployment

No open issues are currently assigned. #23 and #24 are complete.

### Stream G — Release & repo governance

- **#54** — distribute the CLI through a chosen standalone or isolated installation
  mechanism and integrate it with releases.

The PyPI, release-validation, automation, and repository-hardening work remains complete.
Any release workflow change must still preserve the branch-protection/check-dispatch
contract documented in the Stream G README and `docs/RELEASING.md`.

## Priority & effort legend

**Priority (P0–P2)** is taken from each issue's `## Priority` section.

- **P0** — release blocker. A correct release cannot ship until this is done.
- **P1** — high. A real correctness, reliability, security, or deployment problem.
- **P2** — medium. Real but lower urgency, commonly usability or lifecycle work.

**Effort (S / M / L)** is a local planning estimate, not an issue label.

- **S** — one file or configuration area with limited design work.
- **M** — one subsystem, several files, and careful tests.
- **L** — a cross-cutting compatibility or architecture decision; discuss before coding.

## Current stream ownership

| Stream | Folder | Primary ownership |
|---|---|---|
| A — Persistence & data APIs | `stream-a-persistence/` | `store.py`, migrations, list/lifecycle API surfaces |
| B — Tracing runtime & integrations | `stream-b-runtime-integrations/` | `tracer.py`, `decorators.py`, `client.py`, `integrations/` |
| C — Trace schema & interop | `stream-c-schema-interop/` | native schema, HTTP validation, OTLP import/export |
| D — Analysis & reporting correctness | `stream-d-analysis-reporting/` | quality/replay gates, costs, savings and value reporting |
| E — Dashboard robustness | `stream-e-dashboard/` | `dashboard/` (Streamlit) |
| F — Docker & deployment | `stream-f-docker-deployment/` | Dockerfile, Compose, container CI |
| G — Release & repo governance | `stream-g-release-governance/` | packaging, release workflows, distribution, GitHub settings |

## Cross-stream coordination

- **#19 (A)** and **#13 (C)** both edit `agentloop/server.py`. Keep pagination/list
  behavior separate from ingest validation, and rebase before merging either PR.
- **#19/#31 (A)** and **#21 (E)** touch the same dashboard list and lifecycle surfaces.
  Land backend/API contracts before dashboard code that consumes them.
- **#61/#64 (B)** and **#63 (C)** all affect error/status metadata crossing integration
  and OTLP boundaries. Use one documented status/error representation.
- Release workflow changes must be reviewed across `bump-version.yml`, `ci.yml`,
  `codeql.yml`, `dependency-review.yml`, `agentloop-performance.yml`, `tag-release.yml`,
  and `release.yml`; these files now depend on each other's triggers and check names.
- Anything that changes trace JSON, HTTP shapes, environment variables, or database
  records is a **compatibility surface** governed by `SHARED_CONVENTIONS.md`.
