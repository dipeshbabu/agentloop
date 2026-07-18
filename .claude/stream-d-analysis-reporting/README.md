# Stream D — Analysis & reporting correctness

## Scope

Correctness of the gates and money numbers: quality-fixture evaluation and model cost
estimation. Work lands in **`agentloop/quality.py`**, **`agentloop/replay.py`**,
**`agentloop/costs.py`**, and **`agentloop/metrics.py`**, feeding `ci.py`/`value.py` reports.

Issues: **#12** (fail closed on empty/invalid/failing quality fixtures), **#20**
(provider-aware, explicit cost estimates).

## Approach for the stream as a whole

The two issues are independent — either order. Both are about **not silently presenting a
wrong-but-green result**:

- #12: a green check must mean "actually correct," not "no fixtures ran."
- #20: a cost number must be measured or explicitly flagged as an estimate — never a hidden
  fabricated rate.

Do **#20 first** if you want the smaller win (contained to `costs.py`/`metrics.py`); **#12**
touches gating semantics across replay/CI/API/CLI/dashboard and is the more careful change.

## Stream-specific rules

- These changes flip previously-passing paths to failing/warning. That's the point — but call
  it out loudly in the changelog and provide an explicit, named opt-out (report-only mode)
  where the issue allows one.
- Range-check scores/thresholds and validate scorer/fixture shapes before scoring.
- Keep core analysis offline and framework-free (no network to fetch prices — pricing is
  local/configurable).

## Cross-stream coordination

- #12 touches the same replay/CI surfaces referenced by the `agentloop-performance.yml`
  workflow — verify the workflow path still behaves.
- #20's "unknown cost" behavior interacts with replay gates; define it, don't coerce unknowns
  to zero/default.

## Definition of done for the stream

Both issues' acceptance criteria met; new/changed gate semantics covered by tests for CLI
exit codes, report JSON, HTTP input, and the workflow path; changelog documents the
behavior flips.

See [`../SHARED_CONVENTIONS.md`](../SHARED_CONVENTIONS.md).
