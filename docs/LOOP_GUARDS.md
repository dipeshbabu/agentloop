# Retry and no-progress guards

`loop_guard_policy` applies explicit operational limits to declared steps. It
does not automatically retry, replan, deduplicate semantic work, or decide whether
a repeated operation is useful. Combine it with [admission budgets](BUDGETS.md)
for total iteration, retry, call, token, cost, and deadline limits.

```python
from agentloop.budget_types import DispatchOptions
from agentloop.budgets import BudgetLimits, budget_policy
from agentloop.harness import Harness, HarnessConfig
from agentloop.loop_guards import LoopLimits, loop_guard_policy
from agentloop.loop_types import StepInfo, fingerprint

run = Harness(HarnessConfig(mode="enforce", policies=(
    budget_policy(BudgetLimits(max_iterations=20, max_retries=3)),
    loop_guard_policy(LoopLimits(max_retries_per_step=2, max_identical_calls=3)),
))).start_run("task-01")

step = StepInfo(
    "lookup", argument_fingerprint=fingerprint({"query_id": 7}),
    progress_fingerprint=fingerprint({"completed_items": 0}), mutating=False,
)
protected = run.wrap(lambda: "synthetic result", boundary="iteration",
                     dispatch=DispatchOptions(step=step))
result = protected()
```

Run `uv run python examples/harness_loop_guards.py` for a complete offline example.
It accepts changed inputs/progress, then stops at the configured repetition bound.
These are synthetic control-flow checks, not proof of recommendation usefulness.

## Step evidence

`StepInfo` requires a nonempty, host-owned `step_id`. State is scoped to the harness
run, branch, and step. A repeated function name alone is never sufficient evidence.
Keep identities stable for the same logical step and separate unrelated work.

`argument_fingerprint` and `progress_fingerprint` are optional SHA-256 hex digests.
The `fingerprint` helper accepts selected finite JSON values with string mapping
keys. Mapping order is ignored; preserve meaningful order as lists. Unsupported
objects, tuples, cycles, and non-finite values are rejected instead of hashing
their repr or losing type distinctions. Select and normalize application data
explicitly before hashing it.

Input/state payloads are not retained. Fingerprints and identifiers can still
reveal private information and are not anonymization. Progress is caller-defined:
include state that actually represents advancement for your task. The harness
cannot infer domain correctness from a state digest.

If input or progress evidence is missing, no-progress detection stays a qualified
hint. Changed input/progress fingerprints do not trip the identical-call guard.
Overlapping attempts invalidate the sequential history; they are not presented
as proof of a serial loop. Count/deadline budgets remain available for these cases.

## Operational limits

| Setting | Behavior |
| --- | --- |
| `max_retries_per_step` | Maximum dispatched retries for a branch/step; default 3, optional None to disable |
| `max_identical_calls` | Optional bound on consecutive unchanged input/progress fingerprints; 1–256 |
| `oscillation_period` | Optional maximum cycle period, 2–32 |
| `oscillation_repeats` | Required repetitions of a nonconstant fingerprint pattern; 2–8, default 3 |

Histories are bounded by these settings. Per-step retry capacity is reserved
before dispatch so concurrent retries cannot all consume the last allowance.
Another guard's admission denial releases that reservation. Completed attempts
remain counted even if the callable failed or was cancelled.

An identical-call limit permits the configured number of calls and rejects the
next one. An oscillation guard checks the proposed next fingerprint against the
bounded observed sequence and stops before completing the configured repeated
cycle. Both describe operational patterns, not proof that work is useless.

`polling=True` declares legitimate polling and bypasses semantic repetition
checks. Declared retries also use retry limits rather than no-progress heuristics.
Actual polling, retry, or incomplete-evidence work breaks the semantic repetition
sequence. Global [budget limits](BUDGETS.md) still apply, so polling is not an
implicit permission for unlimited work.

The policy supports a `boundaries` subset for adapters with narrower capabilities.
Use matching budget boundaries; unsupported hooks are rejected by the harness.
Each host iteration must cross an iteration wrapper for its total iteration
budget to apply. A generator lifetime is one wrapped call, not an inferred
iteration per yield.

## Retry origin and side-effect safety

The host requests every retry and identifies its source with
`DispatchOptions(retry_source="framework" | "provider" | "harness", step=...)`.
Sources remain distinct in budget counters and decision references. Each attempt
must pass both global budgets and the per-step guard. Avoid marking both parent
orchestration and the same child attempt as a single retry. Hidden provider/SDK
attempts without supported dispatch hooks are not claimed as controlled.

Retries require explicit step identity. `mutating=False` declares read-only work;
otherwise safety is unknown unless `retry_safe=True` explicitly declares safe
retry semantics, such as a reviewed idempotency mechanism. These are host
declarations, not properties inferred from names or arguments.

Potentially mutating retries escalate without dispatch unless safe semantics are
declared. The same applies when identical input fingerprints follow an error,
cancellation, or premature stream closure with potentially mutating effects, even
if the next call omitted its retry marker. Different inputs are not silently
treated as the same operation. Missing identity/input evidence cannot establish
equivalence; supply accurate declarations and bounded global budgets.

The guard does not undo remote effects, automatically repeat a timeout, or claim
that an exception proves no mutation occurred. Original exceptions and cancellation
retain their identity. Shadow mode records proposals while preserving execution.

## Decision evidence and feedback

Continue, stop, and escalation decisions retain reason codes, bounded diagnostic
feedback, explicit evidence references, and prior decision links for retries.
Library-generated feedback uses fixed templates and counters, never raw prompts,
arguments, outputs, or exception messages. Explicit custom `Decision.feedback`
is limited to 256 characters and must be reviewed by its author for privacy.

[Decision evidence schema 1.1](HARNESS_EVIDENCE.md) adds optional feedback.
Readers still accept schema 1.0 records unchanged. Appending new records upgrades
an older envelope without rewriting historical records, and the identity algorithm
remains version 1.0 so old references stay stable. Native trace schema remains 1.1.
Feedback is escaped in HTML; unknown evidence versions remain opaque during
native/OTLP transport and are not interpreted as successful controls.

No diagnostic, stop, or gate pass establishes task quality or a beneficial
intervention. Keep failed/early-stopped cases and validate changes with independent
task outcomes and the existing intervention/study workflow.
