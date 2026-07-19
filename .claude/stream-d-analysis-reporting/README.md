# Stream D — Analysis & reporting correctness

> **Status: 🔴 Open.** #12 was reopened after the shipped implementation left replay
> fail-open paths. #20 and #41 are also open. Treat all three as active correctness
> work rather than relying on the earlier completion banner.

## Scope

Correctness of gates, money numbers, and savings selection: quality-fixture evaluation,
model cost estimation, and optimization aggregation. Work lands in
**`agentloop/quality.py`**, **`agentloop/replay.py`**, **`agentloop/costs.py`**,
**`agentloop/metrics.py`**, and the savings/optimization modules feeding `ci.py` and
`value.py` reports.

Issues: **#12** (fail closed on empty/invalid/failing quality fixtures), **#20**
(provider-aware, explicit cost estimates), **#41** (optimal savings selection or explicit
approximation semantics).

## Approach for the stream as a whole

The three issues are mostly independent. All are about **not silently presenting a
wrong-but-green or unjustifiably exact result**:

- #12: a green check must mean "actually correct," not "no fixtures ran."
- #20: a cost number must be measured or explicitly flagged as an estimate — never a hidden
  fabricated rate.
- #41: a large selection must be proven optimal or explicitly labeled as an
  approximation, including in machine-readable outputs.

Keep each issue in a separate PR. #20 and #41 both feed value/optimization reporting, so
rebase and run their combined report tests if they are developed near each other. #12
touches gating semantics across replay, CI, API, CLI, and dashboard surfaces.

## Stream-specific rules

- These changes flip previously-passing paths to failing/warning. That's the point — but call
  it out loudly in the changelog and provide an explicit, named opt-out (report-only mode)
  where the issue allows one.
- Range-check scores/thresholds and validate scorer/fixture shapes before scoring.
- Keep core analysis offline and framework-free (no network to fetch prices — pricing is
  local/configurable).

## Cross-stream coordination

- #12 touches the same replay/CI surfaces used by `agentloop-performance.yml`; its
  reopened acceptance criteria require verifying that workflow path again.
- #20's "unknown cost" behavior interacts with replay gates; define it, don't coerce unknowns
  to zero/default.
- #20 and #41 both affect reported optimization value; preserve one explicit contract for
  unknown cost and approximation metadata.

## Definition of done for the stream

All three issues' acceptance criteria are met; gate semantics are covered by tests for
CLI exit codes, report JSON, HTTP input, and the workflow path; pricing uncertainty and
savings approximation are machine-readable; and the changelog documents behavior flips.

See [`../SHARED_CONVENTIONS.md`](../SHARED_CONVENTIONS.md).
