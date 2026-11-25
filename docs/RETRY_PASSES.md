# Retry and loop passes

Retries look like waste in a trace, because they're time spent on work that
didn't stick. Many are the opposite: a timeout that succeeds on the second
attempt is the retry doing its job. `agentloop.retry_passes` separates
legitimate retries from wasteful ones. It proposes bounded guard limits only
for the wasteful ones, and checks every proposal against runs that completed
before anyone turns it on.

```python
from agentloop.retry_passes import classify_retries, propose_limits, shadow_policy

classify_retries(trace)["groups"]
candidate = propose_limits([
    {"trace": stuck_run, "completed": False},
    {"trace": good_run, "completed": True},
])
config = shadow_policy(candidate)   # a HarnessConfig in shadow mode
```

## Why each retry happened

Consecutive attempts of the same step form a group. A step is the same name,
parent and input key, and an attempt only counts as a retry after a failed
one. Each group gets a cause, from the span's `retry_reason`, `reason` or
`error_type`, or failing that from its error text:

| Cause | Examples | Verdict |
| --- | --- | --- |
| `transport` | `timeout`, `rate_limited`, `ConnectionResetError`, 429, 503 | `legitimate` |
| `semantic` | `insufficient_answer`, `verification_failed` | `legitimate` |
| `schema` | `invalid_output_schema`, `JSONDecodeError` | `candidate` from the second retry: a cheaper repair may do |
| `tool` | `invalid_arguments`, `not_found` | `candidate`: the same input failed again |
| unknown or mixed | | `unknown` |

A retried tool that isn't declared `idempotent: true` in `agentloop.effects` is
`unsafe_retry` regardless of the cause. Retrying a payment after a timeout can
charge twice. AgentLoop flags these but doesn't change them; they need a person
to look.

## Loops

`find_loops(trace)` reports:
- calls repeated at least three times with the same `tool_reuse_key` or `input_hash`;
- steps that alternate in a fixed pattern, such as open, close, open, close,
  for at least three cycles.

Calls without an input key are never treated as identical.

## Proposing limits

`propose_limits(runs)` takes several recorded runs, each with your own verdict
on whether it completed. It proposes a `LoopLimits` with a limit only where
there's evidence of waste:

| Evidence | Limit |
| --- | --- |
| Wasteful retries | `max_retries_per_step`: the most any completed run needed, at least 1 |
| Identical repeated calls | `max_identical_calls`: the most any completed run made, at least 2 |
| Oscillation | `oscillation_period` and `oscillation_repeats`, one more cycle than any completed run had |

The proposal is replayed against every completed run, and the pass abstains if
the limits would stop any of them. It also abstains when no run completed,
since there's then no safe lower bound, and when every retry was legitimate.
`would_stop` lists the incomplete runs the guard would have cut short.

`replay_limits(limits, trace)` shows where any `LoopLimits` would stop a
recorded run.

## Turning a proposal on

`shadow_policy(candidate)` returns a `HarnessConfig` in `shadow` mode, built with
the existing `loop_guard_policy`. In shadow mode the guard records what it would
have done without stopping anything. Moving to `enforce` is your decision. This
module never does it.

## Checking a change

Record runs with and without the change, and decide for each whether it
completed. Then:

```python
from agentloop.retry_passes import compare_runs

compare_runs(baseline_runs, candidate_runs)  # each run: {"runtime_ms": ..., "completed": ...}
```

A candidate that completes less often than the baseline is
`rejected_incomplete`, however much faster it is. An agent that stops early is
faster and wrong, and this check exists to catch exactly that. Runtimes are
compared only across completed runs.
