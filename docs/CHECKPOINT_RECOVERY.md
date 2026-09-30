# Host checkpoint recovery

Available from an **Unreleased source checkout**, not PyPI 0.7.0. The narrow
`LangGraphRecovery` adapter resumes a synchronous root checkpoint on LangGraph
1.2.11. The framework retains graph state; a trusted host recovery journal retains
AgentLoop's operational policy state and atomic resume claims. Trace replay cannot
restore either source of state.

## Supported contract

Use an enforced `LangGraphHarness` with a built-in budget policy and optional
built-in loop guards. Other policies are rejected because their private state has
no supported recovery codec. Declare the iteration boundary and all boundaries
required by those policies. Both the original graph and resumed graph use the same
configured wrappers. Async entrypoints, streams, nested checkpoints, dynamic
`Command(resume=...)` interrupts and managed-server recovery are outside this path.

After the original synchronous invocation, using `durability="sync"`, has unwound
at a pause or exception:

```python
from agentloop.checkpoint_store import MemoryRecoveryStore
from agentloop.integrations.langgraph_recovery import LangGraphRecovery

# controlled is adapter.runnable(builder.compile(checkpointer=host_checkpointer)).
# The original invoke supplied config={"configurable": {"thread_id": "job-123"}}.
recovery = LangGraphRecovery(
    controlled, MemoryRecoveryStore(),  # Example only: not durable across restarts.
    owner="application-scope", thread_id="job-123", graph_version="workflow.v1",
    clock_domain="host-boot-identity", retry_safe_nodes={"read_only_remaining_node"},
)
reference = recovery.capture()
# Open a distinct trace for the next attempt, if tracing is enabled.
outcome = recovery.resume(reference, request_id="unique-resume-request")
assert outcome.status in {"completed", "incomplete"}
```

Capture validates the original run's thread/namespace and the latest framework
checkpoint. It atomically checks that wrapped calls have finished, serializes only
supported policy state, and retires the old run. The bound runnable then refuses
ordinary invoke/ainvoke/stream/astream entrypoints: use `resume` for subsequent
attempts. The host must serialize initial work and capture on that thread, route
all future resumptions through the recovery controller and journal, and avoid raw
app calls or newly constructed ordinary wrappers that bypass that ownership.

Resume validates owner/thread, record and codec versions, graph version, declared
retry safety, adapter capabilities, exact policy/config hashes, latest checkpoint
identity and outstanding task metadata before claiming the record. Bump
`graph_version` when graph code or state schema changes. Unsupported/changed
configuration requires host reconciliation; it never silently starts with fresh
quotas. Checkpoint/reference identifiers and safety declarations are host-supplied
contracts, not access credentials or inferred proofs of idempotence.

## Quotas, outstanding work and ambiguous outcomes

Every resumed attempt has a new run ID linked to its parent. Model/tool/iteration
counts, global and per-step retries, exact decimal money accounting, token usage,
unknown/held usage, usage-ID deduplication and bounded loop history carry forward.
The resumed root consumes one `harness` retry and one iteration; child dispatches
retain their own accounting and any framework retries. No additional model/tool
usage is invented for the root. Configure those budgets accordingly.

Absolute monotonic deadlines retain their original value. Recovery with a deadline
requires the same host-declared clock epoch (`clock_domain`), and a backwards clock
still fails. Downtime never grants a new deadline. A process may recover on the
same continuous host clock; a reboot or another clock domain requires explicit
host reconciliation. Existing cooperative spending/deadline limitations remain;
there is no hard billing cap or process termination guarantee.

Outstanding or unclosed wrapped work prevents capture. A stopped/escalated run
cannot be resumed to bypass its controls. Every pending root node must be explicitly
declared retry-safe. Failed, interrupted, or uncertain non-idempotent side effects
require host reconciliation or escalation; the adapter never automatically repeats
them. It delegates completed-node retention to the framework checkpoint rather
than reconstructing external outcomes from spans.

A claim is consumed even if execution, evidence capture, or journal persistence
subsequently fails. It is not automatically unlocked or expired. If the process
dies after the claim, remote work and its spend may be uncertain: the host must
reconcile the framework checkpoint, external outcomes and cumulative usage before
issuing a new record. This deliberately prevents a retry from loading an older,
smaller ledger. There is no exactly-once side-effect guarantee.

## Host journal requirements

`RecoveryStore` is a trusted Python protocol, not a public HTTP executable loader.
It declares schema version 1.0, `atomic_claim=True`, and whether it is durable.
Production implementations must enforce authenticated ownership and keep claims,
records and the stream's latest-reference marker in durable atomic transactions.

| Operation | Required behavior |
| --- | --- |
| `save(owner, stream, record, previous_ref=..., request_id=...)` | Save an owned JSON record only if the previous reference is still latest and its claim belongs to that request; return a new opaque reference. Initial save requires no existing stream. |
| `load(owner, stream, reference)` | Return a detached record for that owner/stream; reject missing or unauthorized records. |
| `claim(owner, stream, reference, request_id, record_hash)` | Atomically compare the record hash and latest reference; reject any used request or already-claimed/stale record. Persist the claim before returning true. |

`MemoryRecoveryStore` is the bounded, thread-safe fake used by tests and the offline
example. It cannot survive a process restart and is not a production durable store.
The graph checkpointer must also be durable for restart recovery. Protect journal
contents as application data: they contain operational IDs and state. The codec
uses bounded JSON and an explicit built-in type allowlist, never pickle or dynamic
imports. Ordinary trace exports contain no checkpoint payloads.

## Evidence and studies

Trace metadata `agentloop.recovery` records payload-free checkpoint hashes, parent
and attempt IDs, admission reasons and attempt status. Its separate native harness
audit records have scope `recovery_admission_only`; they contain no task quota and
must not be counted as a new execution budget. Actual resumed dispatch records
contain the carried cumulative budget snapshots.

Use `agentloop.recovery_evidence.recovery_status(trace)` when assigning a study
observation's status. A paused/checkpointed return is `incomplete`; an exception,
cancellation or budget stop remains unsuccessful. `completed` means the root has
no pending nodes, not that its output is correct. Keep independent quality scoring
separate, retain all attempt traces and include all attempt usage in task totals.
Do not sum cumulative budget snapshots across attempts, which double-counts prior
consumption. The adapter requires distinct parent/resume trace IDs to preserve
those attempt boundaries.

From a checkout, run the synthetic example:

```bash
uv run --isolated --frozen --with langgraph==1.2.11 python -m examples.langgraph_recovery
```

It exports separate paused/resumed traces. A one-model-call budget blocks the
remaining model call after resume; a two-call budget finishes without repeating
the first node. Both stores are in memory and no external model is called.

Framework behavior is tested on the pinned SDK; see LangGraph's primary
[checkpoint documentation](https://docs.langchain.com/oss/python/langgraph/checkpointers)
for state snapshots, pending writes and durability. This adapter uses public
`get_state` and synchronous `invoke(None, ..., durability="sync")` operations.
