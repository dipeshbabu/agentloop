# Trace retention

These are **Unreleased source-checkout APIs**. Retention acts on completed native
traces, before configured finalization destinations. It reduces persisted trace
volume; it does not reduce the live tracer's memory use or provide a warehouse.

```python
from agentloop import RetentionContext, RetentionPolicy, RetentionSession

session = RetentionSession(RetentionPolicy(
    policy_id="batch-review",
    version="1",
    mode="compact",
    sample_rate=0.05,
    seed="review-2026-09",
    max_events=20,
    high_latency_ms=5000,
    high_cost_usd=0.10,
    protected_cohorts=("release-canary",),
    representatives_per_bucket=2,
    max_buckets=256,
))

# completed_trace must already be finished. Host outcome is an observation,
# not a quality score; supply it only when the host actually knows the outcome.
retained = session.retain(completed_trace, context=RetentionContext(
    sample_key="opaque-task-attempt-id",
    outcome="success",
    cohorts=("release-canary",),
    disagreement=False,
))
if retained is not None:
    retained.export_json("runs/retained.json")
counts = session.summary()
```

For automatic export/store/upload, pass `agentloop.init(retention=session)`.
All configured destinations receive the same retained artifact, and sampled-out
runs reach none. Retention/redaction errors fail closed before destinations run;
the finalization result records a retention error without copying exception text.
`fail_silently=False` raises `FinalizationError`. The exception chain is for trusted
local debugging and can contain the redactor's error text. `retention=None` keeps
the existing session; `retention=agentloop.CLEAR` disables it. Direct calls to
`trace.export_json`, stores or clients bypass runtime configuration: pass the
retained artifact explicitly when using those APIs. The in-memory source trace
is not modified or erased.

Automatic finalization has no host annotations. Completed workflow status can
establish execution completion, while legacy traces with unknown outcomes remain
protected by default. Use explicit `retain(..., context=...)` for annotated batch
pipelines. A trace cannot be retained twice. Each call must represent one completed
execution; sessions count calls and do not maintain an unbounded run-ID registry.
Keep completed source traces immutable during retention. Redactor-driven source
mutation is detected and rejected before committing a retained result.

## Selection and rates

Deterministic sampling hashes the seed and sample key (run ID by default). It is
stable across ordering and processes for the same key/seed/rate. Probabilistic
sampling uses a seeded Python pseudo-random stream, reproducible for the same call
order and runtime. Concurrent calls are serialized; their arrival order is not
promised. This randomness is not suitable for secrets or adversarial sampling.

Failures/timeouts, unknown outcomes or incomplete usage/cost, disagreements,
anomalies, configured cohorts and threshold outliers can override sampling. Known
cost is a lower bound when pricing is partial: exceeding the threshold is enough
to retain a run, but falling below it does not establish a cheap execution.
Cost measures recorded model calls, not total operating cost. Defaults retain
unknowns. Omitted disagreement/anomaly annotations remain `None`, not `False`;
hosts must signal those domain-specific observations explicitly.

Representative retention keeps the first K retained traces for each hashed
stage/outcome/status bucket. Undeclared stages use event names. The registry has a
hard `max_buckets` limit; additional unseen buckets get ordinary sampling and
protection only. No eviction silently resets quotas. One run can cover several
buckets, and the same stage can occur repeatedly without consuming extra quota
within that run. These are order-dependent examples, not an unbiased sample.

Every artifact includes the versioned policy/hash, source hash, selection reasons,
base sample rate, conditional inclusion rate, sample-key hash and session-prefix
counts/rate. The prefix rate is not the final stream rate. `session.summary()`
reports the latest exact `observed_input_count` and `observed_sample_count`.
`estimated_population_count` is explicitly unavailable: no sampling frame,
independence or uncertainty model is inferred. Reused keys select clusters;
protected/representative cases further change the sampling distribution. Do not
multiply counts by `1/sample_rate` or treat sampled quality as population quality.
Persist the final session summary alongside a batch if final denominators matter.

## Payloads and identity

`payloads="omit"` is the default. It removes input/output/error text and arbitrary
metadata, keeping SHA-256/type/length summaries for retained events. Trace, event
and model names become hash aliases; retained stage/schema references are hashed.
`payloads="redact"` additionally requires a trusted `str -> str` callback and a
versioned `redactor_ref`; it processes only retained text fields and must return
at most 100,000 characters. Metadata remains omitted. AgentLoop does not verify a
custom redactor's privacy guarantees. Raw text/names/metadata require the explicit
`payloads="capture"` setting.

Run IDs, event IDs, parent IDs, timestamps, event types, token provenance and policy
references remain structural data. Supply opaque IDs and permission-cleared labels;
do not put user data in those fields. Hashes support equality checks, not anonymity
or protection against guessing low-entropy values. Policy seed and protected
cohort labels are recorded as configuration. Use opaque labels there too.

The original run identity and retained event order are unchanged. Artifacts record
the original event count/order hash and each retained event's original index.
`compact` keeps a prefix of `max_events`, plus error/unknown-status events when
protected. This is a soft limit: error-heavy traces can exceed it. Parent references
can point to omitted events; no replacement parents or dependencies are invented.
`metrics_only` retains no events, even for protected executions, so failures survive
as retained run summaries/counts rather than full error spans.

## Supported analyses

| Mode | Available evidence | Analysis limits |
| --- | --- | --- |
| `full` + `capture` | All source events and metadata, plus retention record | Existing findings/replay/studies remain available; sampling still limits population claims. |
| `full` + `omit`/`redact` | All event identities/order and safe fields; original aggregate snapshot | Descriptive aggregate metrics only; payload/metadata evidence is incomplete. |
| `compact` | Retained event subset/order map and original aggregate snapshot | No complete graph, finding, replay or study inference. |
| `metrics_only` | Original aggregate snapshot and retention record | No event-level analysis or replay/study inference. |

Snapshots preserve original event/operation/model/tool/retry counts, wall runtime,
cumulative recorded span time, token totals and provenance, known model cost and
its completeness/pricing basis, error-span count and the original repeated-context
heuristic. That heuristic remains an estimate, not measured saved tokens. Model
cost details are aggregate-only; aliases are never repriced. `event_count` describes
the original trace and `retained_event_count` describes stored events.

Lossy reports set `analysis_complete=False`, contain no inferred findings, and
explain missing evidence. Existing diagnosis numeric zero-savings placeholders
must not be read as evidence that no optimization exists: finding analysis is
unavailable. Replay and study entry points reject lossy artifacts explicitly,
including when an external quality report is supplied. Retained error counts or
host success labels do not substitute for task-quality evidence.

Content hashes bind metrics and retention metadata to the derived native artifact;
modifying either invalidates analysis. They detect accidental mutation, not forged
data from an untrusted producer. Native JSON and local storage retain the metadata;
exports through other telemetry formats are not a retention round-trip contract.
Session state is bounded by bucket limits, but processing is proportional to the
source trace/payload size. Metric collection skips graph/finding analysis. No
provider calls, external scorers, production rollout or paid spend are involved.
