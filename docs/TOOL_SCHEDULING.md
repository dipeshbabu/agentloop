# Declared tool scheduling

This **Unreleased** adapter runs tool callbacks concurrently only when the host
declares dependencies, resource reads/writes, side effects and worker-thread
support. It does not infer safety from names, past successful runs or trace timing.
Ordinary tracing remains unchanged. The [read-only study](SCHEDULING_STUDY.md)
checks actual query outputs and records scheduling overhead.

```python
from agentloop.budgets import BudgetLimits, budget_policy
from agentloop.harness import Harness, HarnessConfig
from agentloop.scheduling_types import ScheduleConfig, ToolCall
from agentloop.tool_scheduling import ToolScheduler, scheduling_policy

settings = ScheduleConfig("lookup-batch", "1", "lookup", max_concurrency=4)
run = Harness(HarnessConfig(
    mode="shadow",
    policies=(scheduling_policy(settings), budget_policy(BudgetLimits(max_tool_calls=4))),
)).start_run("task-01")
scheduler = ToolScheduler(run, config=settings)
result = scheduler.execute([
    ToolCall("first", lambda context: read_first(),
             depends_on=(), reads=("source-first",), writes=(),
             concurrent=True, effect="read_only"),
    ToolCall("second", lambda context: read_second(),
             depends_on=(), reads=("source-second",), writes=(),
             concurrent=True, effect="read_only"),
], timeout_s=2)
```

The host supplies synchronous `read_first` and `read_second` callbacks. Each
callback receives `ToolContext`: its call ID, a read-only mapping of prerequisite
results, a cooperative cancellation flag and remaining deadline time. With known
dependencies, only those declared prerequisite values are supplied. Unknown
dependencies use the completed preceding values under original-order execution.
Values themselves remain host-owned objects; callbacks must honor their declared
resource and mutation contracts.

## Planning and execution

`ToolCall.depends_on=()` declares independence; `None` means unknown. Reads and
writes similarly require explicit tuples, including empty tuples. Resource IDs
are opaque bounded identifiers compared by exact equality. The host must give
the same physical shared resource the same identifier; aliases and external
work are not discovered automatically. `effect` is `read_only`, `mutating` or
`unknown`; read-only calls cannot declare writes.

Plans are bounded to 256 calls and 4,096 explicit edges. Duplicate call IDs,
duplicate dependencies, missing prerequisites and cycles fail before dispatch.
Known dependencies use a stable topological order. Read/write or write/write
conflicts add serialization edges in that order, preserving causal constraints.
Results are returned in the caller's submission order even when execution differs.

Unknown safety or unsupported concurrency serializes the whole batch on the
calling thread. `on_unknown="error"` rejects unknown declarations under enforcement.
When dependencies are unknown, known edges must be compatible with the caller's
original order; an ambiguous forward dependency is rejected. No concurrent work
is authorized solely because two callbacks have matching names.

Disabled and shadow modes execute sequentially on the calling thread. Shadow
retains the proposed plan and policy decisions. Enforce mode permits up to the
configured concurrency, bounded between 1 and 32. Worker threads receive separate
copies of the active tracing context, and every tool is admitted through the same
`HarnessRun` before its callback can start. `concurrent=True` declares that the
callback can safely run on a worker thread, including any SDK or connection use.

Each call can supply `DispatchOptions`, a usage reader and an error-usage reader.
Use [budget reservations](BUDGETS.md) for metered resources; token/cost metering
defaults to model boundaries, so explicitly include `metered_boundaries=("tool",)`
for tool billing. Call-count budgets apply to tool admission independently.
Provider/SDK internal retries remain the host adapter's responsibility.

## Errors, cancellation and completed work

`ScheduledResult.outcomes` associates every call ID with a status, original return
value and, when present, the original exception. Values/errors are omitted from
their default representation and from schedule receipts. `completed` also requires
that no batch stop reason occurred.

`on_error="stop"` stops new admission after a failed call. The optional
`continue_independent` mode continues only work whose prerequisites completed;
dependents and conflicting-resource successors remain blocked after failure.
No callback is automatically retried, including partially completed mutations.

Deadlines and an optional `threading.Event` stop future admission cooperatively.
They do not kill running Python threads. The scheduler drains already-started
callbacks before returning or re-raising an interruption; elapsed time can exceed
the requested timeout. Callbacks must use bounded I/O or cooperate through
`ToolContext.cancellation_requested` and `remaining_s`. Completed outputs remain
available even if a later budget hook stops the batch.

An interrupted caller can inspect `scheduler.last_result` for retained partial
outputs. A scheduler rejects overlapping batches and retains claimed call IDs
within its bounded lifetime, preventing accidental resubmission of started work.
Explicit retries require new attempt IDs and host review of prior effects.
Recovery across process restarts remains host-owned; no distributed lock or
checkpoint guarantee is implied.

## Evidence and quality

`scheduler.export_evidence()` and native trace metadata at `agentloop.tool_schedule`
retain policy/configuration identity, the declared plan, conflict edges, actual
dispatch/completion order, peak running callbacks, admission IDs, timestamps,
outcomes and stop reasons. Exports are copies. Evidence and claimed-call limits
stop new work rather than silently evicting its history. Opaque step/reservation
references are hashed; raw arguments, results and exception messages are not
logged by the scheduler.

The callback-entry marker describes host invocation, not proof of a remote side
effect. Partial failures may have changed external state. Declared safety is not
a sandbox, and separate schedulers/external workers require host coordination.

Task-specific output checks remain independent of scheduling decisions. Replay
compares retained observations; it never re-executes a recorded tool effect. The
study demonstrates preserved ordered outputs and actual concurrency, while also
retaining overhead that exceeds the benefit for these small operations.
