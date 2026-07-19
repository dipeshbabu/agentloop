# Stream D — Analysis & reporting correctness

> **Status: 🟡 Partial.** #12 is closed and shipped (fail-closed quality fixtures — see
> `agentloop/quality.py`'s `QualityValidationError` and its 422 mapping in
> `agentloop/server.py`). **#20 is still open** — that's the only remaining work in this
> stream. The "do #20 first if you want the smaller win" framing below no longer
> applies; #12 is done, so just do #20.

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

- ~~#12 touches the same replay/CI surfaces referenced by the `agentloop-performance.yml`
  workflow — verify the workflow path still behaves.~~ **Resolved**: #12 shipped; the
  workflow path was verified as part of that PR.
- #20's "unknown cost" behavior interacts with replay gates; define it, don't coerce unknowns
  to zero/default. *(Still applies — #20 is the remaining work here.)*

## Definition of done for the stream

Both issues' acceptance criteria met; new/changed gate semantics covered by tests for CLI
exit codes, report JSON, HTTP input, and the workflow path; changelog documents the
behavior flips.

See [`../SHARED_CONVENTIONS.md`](../SHARED_CONVENTIONS.md).
