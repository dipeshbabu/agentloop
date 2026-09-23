# Harness decision evidence

An enabled [harness](HARNESS.md) records control decisions independently of
model/tool spans. When a trace is active at admission, its `metadata["agentloop.harness"]`
contains the decisions and historical policy declarations. A stream's completion
stays with that captured trace even if another trace is active when it closes.
Disabled wrappers add no evidence and remain the original callable.

Run the offline example:

```bash
uv run python examples/harness_evidence.py --out runs/harness-evidence
agentloop analyze runs/harness-evidence/shadow.json --html runs/harness-evidence/shadow-cli.html
```

The example writes traces, detached evidence JSON, and HTML for all three modes.
Disabled and shadow execute the synthetic tool once; enforce denies it before
dispatch. This demonstrates capture and control flow, not an optimization benefit.

## Envelope 1.0

The namespace has four fields:

| Field | Contract |
| --- | --- |
| `schema_version` | Independent harness-evidence version, `1.0` |
| `policies` | Immutable-at-capture declarations indexed by policy configuration hash |
| `decisions` | Records indexed by their `hdec_` identity |
| `capture_errors` | Safe diagnostics when delivery to a trace failed |

`run.export_evidence()` returns an independent copy, including decisions captured
without an active trace. `validate_evidence(artifact)` validates a detached
artifact; `read_evidence(trace)` additionally checks ownership of its trace
references. JSON object order is not execution order: use `hook_sequence` within
a harness run and `policy_order` within a hook. HTML uses those fields explicitly.

Normal trace-context propagation rules still apply. Bind context explicitly with
`bind_trace_context` when dispatching in a new thread. Calls made without an active
trace remain in the detached run artifact with unavailable trace/span references;
they are not silently attributed to a different trace.

Policy declarations retain ID, version, configuration hash, priority, hooks, and
actions. They are captured when the harness is created. Later configurations do
not overwrite earlier snapshots. A shared policy snapshot is stored once per
hash, rather than once per decision. Identical duplicate writes are idempotent;
different content at an existing decision identity or policy hash raises an error.

## Decision fields

| Fields | Meaning |
| --- | --- |
| `decision_id`, `hook_id` | Stable decision identity and shared hook/effect identity |
| `run_id`, `call_id`, `branch_id` | Explicit harness run, invocation, and branch |
| `trace_id`, `span_id` | Captured trace and active **parent** span at admission; null when unavailable |
| `policy_id`, `policy_version`, `policy_config_hash`, `harness_config_hash` | Historical policy/configuration provenance |
| `boundary`, `phase`, `mode` | Model/tool/iteration/completion, before/after, and shadow/enforce |
| `requested_action`, `resolved_action` | This proposal and the resolution across matching policies |
| `outcome`, `reason_code`, `origin` | Proposal disposition, bounded reason, and policy/intrinsic-harness origin |
| `execution_status`, `dispatched` | Observed execution status and whether invocation was attempted |
| `evidence_refs` | Caller-declared opaque identifiers; no automatic prompt or argument forwarding |
| `conflicting_decision_ids`, `retry_of` | Conflicting proposals in the same hook and an explicitly declared prior retry decision |
| `hook_sequence`, `policy_order` | Recorded ordering, independent of JSON dictionary order |
| `timing` | UTC start, policy evaluation duration, and shared hook evaluation duration |
| `budget_snapshot` | Null while budget evidence is unavailable; never an invented zero budget |
| `evaluation_status` | `unverified`; a decision alone is not an outcome comparison |

Decision identity is SHA-256 of canonical JSON containing evidence version, run
ID, invocation ID, boundary, phase, and policy ID. Identity excludes outcome and
timing so an attempted overwrite with changed evidence is a conflict. Invocation
IDs distinguish actual repeated calls; retry links do not reuse identities.
The shared hook identity omits policy ID so multiple proposals are not counted as
multiple control effects.

The span reference is the existing parent context, not an invented model/tool
span for denied work. Use ordinary tracing alongside the harness to capture
work spans. Wrap the traced callable **inside** the harness wrapper to keep
policy evaluation outside that callable's model/tool span. Broader host spans
and overall elapsed runtime can still include control time.

Run, branch, and evidence references preserve nonempty opaque IDs, including
Unicode and paths used by existing applications. Policy IDs, versions, and reason
codes remain bounded labels. Identifiers themselves can reveal private information;
the harness does not treat them as anonymized data.

## Outcomes and timing

| Outcome | Meaning |
| --- | --- |
| `proposed` | A shadow-mode control proposal; execution is unchanged |
| `applied` | An enforced control action that matches the hook resolution |
| `rejected` | A control proposal superseded by another policy's stronger action |
| `failed` | Policy evaluation failed; enforcement can escalate/stop admission |
| `no_op` | Continue/no policy change, including intrinsic pass-through hooks |

An error-triggered escalation remains a **failed policy evaluation**, not a
successful optimization. Multiple policies supporting one resolution share a
hook ID. Intrinsic admission decisions use origin `harness` and do not count as
applied user-policy interventions.

Policy evaluation timing includes callback validation/error handling and is
separate from shared hook evaluation timing. The latter
includes lock wait and evaluation bookkeeping, but excludes evidence
serialization. Do not sum the repeated `hook_duration_ms` across proposals;
deduplicate by hook ID as HTML does. Cumulative timings across concurrent hooks
can exceed wall-clock duration. They neither create model/tool events nor
fabricate usage. Protected calls from inside policies are rejected, avoiding
recursive dispatch and duplicate usage; arbitrary unwrapped policy code remains
the trusted host's responsibility.

`dispatched=false` on failed admission distinguishes a pre-dispatch cancellation
from cancellation of an attempted call. `dispatched=true` does not prove that a
provider completed work, billed it, or produced side effects. A generator that is
never resumed creates no decisions.

## Privacy and capture failures

Raw prompts, arguments, results, policy state, and exception messages are not
included in decision records. Policy configuration values are also omitted by
default. Snapshots retain their hash and declarations, with
`configuration_capture="redacted"` and `configuration=null`. Hashes identify
settings; they do **not** anonymize secrets or recover omitted settings.

Only set `HarnessConfig(capture_policy_configuration=True, ...)` for configuration
you have reviewed for sharing. This explicitly retains its immutable JSON values.
HTML still omits those values unless `include_content=True` (CLI
`--include-content`) is also selected. These rules do not remove private data
that the host independently records elsewhere in the trace.

The `agentloop.harness` namespace is reserved when capture is enabled. If delivery collides
with invalid or conflicting trace metadata, the detached run artifact records a
safe `trace_evidence_error`. Shadow execution continues unchanged; enforce mode
stops further admission and fails closed. Original callable failures and
cancellation retain precedence over after-hook capture failures. Inspect capture
diagnostics rather than assuming trace delivery always succeeded.

## Comparisons and persistence

The existing [intervention builder and ledger](INTERVENTIONS.md) snapshot harness
evidence from the actual baseline and candidate traces under
`metadata["agentloop.harness_evidence"]`. This creation-time key is reserved for derived
evidence; callers cannot supply a replacement. Old intervention artifacts without
it remain readable. No new ledger or database migration is introduced.

The comparison keeps all decision outcomes, but only enforced, applied **policy**
decisions appear in `applied_candidate_decision_ids`. Shadow proposals, failed
policies, and intrinsic admission records are not promoted to applied
interventions. Missing retry/conflict targets appear in
`unresolved_decision_ids`; capture errors or unresolved references produce
`capture_status="known_incomplete"`. Otherwise it is `no_reported_gaps`, which
does not prove that uninstrumented or unbound work was captured.

`comparison_status="observed_pair"` means the existing replay compared supplied
traces. It does not establish a causal effect for an individual policy;
`individual_policy_effect` remains `unverified`. Failed quality gates and unknown
costs remain in the existing measured report. Original decision and estimator
snapshots are retained even if current policy settings or findings later change.
Project isolation and idempotent/conflicting writes use the existing SQLite and
Postgres store contracts.

## Compatibility and export

Native trace schema **1.1** is unchanged: the additive namespace is metadata.
Existing traces and ordinary reports remain valid. AgentLoop OTLP exports carry
it in the existing `agentloop.trace.metadata` resource attribute, including empty
traces containing only decision evidence. Generic third-party exporters are not
assumed to preserve AgentLoop-specific metadata.

Unknown harness-evidence versions remain transportable as opaque trace metadata.
The decision reader rejects unsupported/malformed evidence; HTML reports that
limitation and omits its raw contents. Interventions refuse invalid source
evidence rather than manufacture comparison links. Native metadata is not rewritten
or dropped during transport. These artifacts are not a tamper-proof security audit.
