# Recorded multi-agent coordination

Read-only Harbor and Omnigent imports now export `coordination.json` and
`coordination.html` alongside their existing traces, receipts and inventories.
These reports join supplied evidence without running an agent or merging
separate documents into a session trace. They are Unreleased checkout features;
the published `agentloop-profiler==0.7.0` wheel does not contain them.

## Offline workflow

Use Python 3.10+ and the installed checkout. Harbor, Omnigent, vendor clients,
credentials, a database and a dashboard are unnecessary:

```bash
uv sync --locked --dev
uv run --frozen agentloop harbor import-atif tests/fixtures/external/harbor/atif_embedded_subagents.json --synthetic --out runs/coordination-harbor
uv run --frozen agentloop omnigent import-otel tests/fixtures/external/omnigent/parent_child_multi_trace.otlp.json --synthetic --out runs/coordination-omni
uv run --frozen python examples/multiagent_coordination.py --out runs/coordination-example
```

The Harbor fixture retains three distinct trajectory documents in one shared
session, two referenced children and three reported model calls. Recorded model
blocks report 36 input tokens; the parent/child inclusive final aggregates total
60 if blindly added. The report preserves those aggregates separately and uses
36 for attribution to the recorded model leaves. Cached tokens remain a subset.
The fixture has no complete root timing: wait, overlap and critical path are
unavailable.

The Omnigent fixture retains two native trace IDs. Its span link correlates the
records but establishes no causal parent, handoff or cross-trace critical path.
The example adds owned implementer/reviewer/responder spans with actual synthetic
intervals. It reports the observed overlap and child failures without claiming a
review quality effect or provider benefit.

For completed Harbor jobs, `agentloop harbor import JOB --out OUT` also writes
coordination reports. Trial agent-context usage remains separate from trajectory
usage. The reported parent task/verifier outcome can be attached to a child
observation, explicitly scoped to the parent trial; it does not establish that
child's independent contribution or correctness.

## Evidence and metric boundaries

| Field | Evidence and meaning |
| --- | --- |
| `actors` | Each ATIF trajectory document or recorded Omnigent AGENT span; native run/event and receipt identities remain distinct |
| `handoffs` / `handoff_count` | Recorded ATIF delegation references or explicit source span parents for AGENT operations; no network dispatch count inferred from names |
| `observed_child_count` | Distinct supplied children resolved by scoped document/span identity |
| `unknown_child_count` | Referenced children lacking a supplied projection; no invented duration/status |
| `child_failure_count` | Explicit recorded failed, timed-out or cancelled child status; unknown child statuses counted separately |
| `handoff_wait_ms` | Unavailable without a dispatch/blocking-join interval; child runtime is reported separately and never substituted |
| `parallel_overlap_ms` | Union of time with at least two recorded leaf actors; enclosing parents are excluded; incomplete timing or declared missing children prevents a complete total |
| `known_parallel_overlap_ms` | Overlap among supplied timed leaves, with its denominator; not independence, a synchronization guarantee, or a missing-child estimate |
| `critical_path_by_trace` | Existing execution graph calculation only for a connected, acyclic, fully timed explicit source span tree; no order-based edges |
| `critical_path_ms` | Available only for one such trace without unresolved/correlation relationships or missing children; never a guessed cross-process task path |
| `model_call_count` | Source-reported multiplicity for recorded blocks; complete executor coverage is not established |
| `usage` | Known and complete-for-recorded-leaves input/output/cache counts; nested model parents and source inclusive aggregates excluded from leaf sums |
| `retry_count` | Unavailable because these source contracts do not establish all retry attempts |
| `possible_duplicate_work` | Same named tool with identical explicitly captured arguments in the same source scope; investigation evidence, not safe reuse or saved spend |
| `review_overhead_ms` / `review_quality_delta` | Unavailable without controlled reviewed-vs-unreviewed task pairs |

`agent.role` and `omnigent.agent.role` are bounded observational aliases; ATIF
`agent.extra.role` preserves the explicit `reviewer`, `implementer` and `responder`
labels. A role label never changes a verifier outcome. Parent agent/session aliases
may resolve to one actor, multiple actors or no actor. Those references remain
noncausal and create no dispatch or dependency edge. Native OTel parents remain
scoped to their original source trace.

Source clocks are not independently synchronized or authenticated. Converter-
inferred timing and conflicting source segments cannot establish measured actor
latency or a critical path. Actual source intervals may describe overlap even
when full execution coverage is unknown; the report states that scope.

## Python API and duplicate-work investigation

```python
from agentloop.integrations.harbor.atif import import_atif
from agentloop.interoperability.coordination import summarize_coordination

result = import_atif("trajectory.json")
report = summarize_coordination(result.traces, result.source_receipts)
result.write("runs/imported")  # Includes the JSON and HTML coordination reports.
```

The API checks each projection against the byte hash in its immutable receipt.
It rejects mutated traces, multiple receipts claiming one run and ambiguous
trajectory identities. Job/trial namespaces keep repeated trajectory IDs distinct.
Original traces, receipts and outcomes remain unchanged.

Default input capture omits tool arguments, so equal tool names or argument sizes
cannot support duplication. `AtifOptions(capture_content=True)` is an existing
explicit privacy opt-in. With complete captured argument dictionaries, the report
compares canonical hashes and exports only hashes/span references, never argument
values. Omitted/redacted arguments remain unsupported. Changing tool state,
side effects and required independent checks still prevent a reuse conclusion.
Use the existing [semantic investigations](SEMANTIC_WASTE.md) and
[paired interventions](INTERVENTIONS.md) to validate a proposed removal; no new
finding estimator, model judge or savings claim is added by coordination import.

## Privacy, failures and compatibility

Default imports omit raw prompts, outputs, reasoning, tool arguments and arbitrary
exception text. Report text is escaped and HTML uses the existing offline exporter
styles and restrictive content policy. Explicit content capture affects the
underlying imported artifacts; treat them according to the existing import guide.

Unresolved children, missing intervals, invalid/cyclic parent graphs, unrecorded
roles, unknown usage and noncausal links stay visible. Export is idempotent for
identical supplied bytes and refuses conflicting files or indirect output paths.
No source scripts, verifiers, providers or external URLs execute. Native trace,
study, storage and database schemas and core dependencies remain unchanged.

The frozen Harbor ATIF contracts are described in [HARBOR_ATIF.md](HARBOR_ATIF.md),
and Omnigent aliases in [OMNIGENT_OTEL.md](OMNIGENT_OTEL.md). Their pinned source
revisions are `d5ac1be17f575852eaf4fffc4072fd18481c209b` and
`a2956be0e97bb175a60b274053d836f07d494c6c`, respectively. These owned fixtures
exercise format behavior, not live vendor compatibility. Artifact hashes establish
consistency with supplied bytes, not source authenticity, verified provider
billing, or a causal harness effect.
