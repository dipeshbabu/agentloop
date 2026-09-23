# Offline semantic judgments

AgentLoop's judgment contract **1.0** provides optional, provider-independent
answers to explicitly posed questions about selected execution spans. Ordinary
`trace.report()`, diagnosis, HTML, replay and store operations never execute a
judge. They can display already attached receipts. No provider SDK, credentials
or remote service is required.

Judgments are separate from observed execution facts, deterministic finding
rules, and [caller-defined task quality](STRUCTURED_QUALITY.md). A judgment is
neither universal correctness evidence nor permission to change production
behavior. This contract does not introduce semantic waste finding rules or
automatic model substitution.

## Python interface

```python
from agentloop import (
    JudgeIdentity, JudgeUsage, JudgmentAnswer, JudgmentSession, JudgmentSpec,
    LocalCallbackJudge, attach_judgment, judgment_request,
)

def local_rule(request, *, timeout_s):
    # Only explicit references, numeric/status observations and caller-supplied
    # summaries are available here. This illustrative rule is not task quality.
    summaries = [item.summary for item in request.evidence]
    if any(item is None for item in summaries):
        return JudgmentAnswer(status="unknown", reason="insufficient_evidence")
    return JudgmentAnswer(
        summaries[0] == summaries[1],
        usage=JudgeUsage(0, 0, 0.0, "reported", "reported"),
    )

identity = JudgeIdentity.configured(
    "example.summary-equality", "1", {"comparison": "exact"},
    provider="local", model_or_rule="summary-equality", revision="1",
)
session = JudgmentSession(LocalCallbackJudge(identity, local_rule), cache_size=128)
request = judgment_request(
    trace, JudgmentSpec("Do these approved summaries match?", "boolean"),
    ["first-span", "second-span"],
    summaries={"first-span": "Approved summary", "second-span": "Approved summary"},
)
receipt = session.evaluate(request, enabled=True, timeout_s=1.0)
attach_judgment(trace, receipt)
trace.export_json("judged-trace.json")
```

`JudgmentBackend` is a Python protocol with a `JudgeIdentity` property and
`judge(request: JudgmentRequest, *, timeout_s: float | None) -> JudgmentAnswer`.
Adapters implement this protocol; analysis consumes the AgentLoop contract.
`LocalCallbackJudge` accepts a trusted synchronous callable directly, without
dynamic imports or executable paths from trace metadata. It forwards `timeout_s`
as a keyword argument. Asynchronous callbacks/results are not supported.

The immutable request contains a run ID, a `JudgmentSpec`, and a tuple of
`JudgmentEvidence`. The four answer types are:

| Kind | Required declaration | Valid known answer |
| --- | --- | --- |
| `boolean` | Question | Exactly `True` or `False`; integers are rejected |
| `probability` | Question | Finite numeric value in `[0, 1]`, excluding booleans |
| `choice` | Question and unique nonempty string `choices` | Exactly one declared string |
| `score` | Question and finite `minimum < maximum` | Finite numeric value within inclusive bounds |

Probabilities and confidence values are uncalibrated unless the backend supplies
`JudgmentUncertainty(calibration_status="measured", calibration_ref=...)`.
The reference must point to caller-owned calibration evidence; AgentLoop records
this assertion, without verifying that the measurement is valid for a new task.
Confidence, when supplied, must be in `[0, 1]` and is distinct from the answer.

## Failure, latency and usage semantics

Execution defaults to `enabled=False`, producing a `disabled` receipt without
dispatch, even if a cached answer exists. Explicit execution returns `known`,
`unknown`, `timeout`, `error`, or `invalid_result`. Unknown answers require
`reason="abstained"` or `"insufficient_evidence"`; failure results carry no answer.
Out-of-domain values, wrong types and changing backend identity during a call
cannot become a known result. Exceptions expose only bounded public categories,
never exception messages or arbitrary class names. Fatal cancellation and other
`BaseException` subclasses propagate.

Timeouts are **cooperative**. The backend owns cancellation and resource limits.
AgentLoop passes a time budget, recognizes `TimeoutError`, and discards an answer
that arrives after that budget, retaining reported usage. A synchronous callback
that never returns is not forcibly interrupted. Run untrusted or unbounded work
in a host-owned isolated process. No background worker or hidden retry is started.

`JudgeUsage` preserves input/output counts independently, with `token_basis`
`reported`, `estimated`, or `unknown`. Cost in USD has a separate `cost_basis`
`reported`, `calculated`, or `unknown`. Missing usage is `None`, including failures
where a provider may already have charged. The backend must explicitly report
zero for a free local rule. Declared usage and its basis must agree.

The original evaluation's ID, timestamp, latency, usage and uncertainty remain
intact on cache hits. Each invocation receives its own ID, dispatch/cache flags,
latency and incremental usage. Cache hits and disabled execution carry zero
incremental model usage with basis `not_dispatched`. Read summaries sum invocation
costs, not repeated copies of cached evaluation costs. These are offline analysis
costs and timings; workflow cost, runtime, token totals and quality stay separate.

## Evidence and privacy

`judgment_request` selects unique existing span IDs. Its automatic projection
contains only ID, source hash, normalized operation kind, normalized status and
duration. It does not forward trace/event names, prompts, outputs, tool payloads,
errors, arbitrary metadata, provider configuration or model names. Unknown
operation/status labels normalize to `unknown`. Caller-authored questions,
identifiers, judge identities and explicit derived summaries are visible to the
backend and exported receipts; use permission-cleared values for them. References
are opaque strings, never automatically fetched URLs or file paths.

The source hash binds the normalized selected event, including its locally held
content and metadata. Hashing does not redact the original trace and is not a
privacy guarantee for low-entropy data. Transport-only `otel_span_id` and
`otel_trace_id` are excluded. Changes to selected source content invalidate
evidence even when the automatic projection is unchanged. Context outside the
selected spans is not silently added; include needed context in an approved
summary and update it when that context changes.

`attach_judgment` copies and validates a receipt and checks its source binding.
It preserves other trace metadata and earlier receipts. Reattaching the same
invocation is idempotent; conflicting or duplicated invocation records are
invalid. `read_judgments` retains original receipts but reports `effective_status`
`stale` and `effective_value=None` if selected source evidence no longer matches.
Consumers must use these effective fields, not an archived answer in isolation.
Malformed or unsupported evidence is explicit and cannot trigger execution.
The `semantic_judgments.status` field describes these optional receipts;
the existing `analysis_complete` field continues to describe deterministic
finding-rule execution.

## Cache identity and storage contract

Each session has a bounded LRU of successful results only, default capacity 128.
Use `cache_size=0` to disable it and `clear_cache()` to invalidate it. A session
belongs to one backend and one analysis worker; it is not thread-safe. Caches are
not shared across sessions/backend instances and are never reconstructed from
trace metadata. Repeated source validation hashes each selected event once per
read, even when many receipts refer to it.

The cache key is SHA-256 over canonical finite JSON containing the contract
version, full typed request/evidence, and full judge identity. Identity includes
implementation, version, provider, model/rule, revision and configuration hash.
Questions, choices, bounds, summary text, source content, configuration, revision
and implementation changes therefore change the key. Identity/version/configuration
are adapter declarations: adapters must bump them when behavior changes. A mutable
callback closure or external model alias cannot be automatically detected; use a
new session/revision, or disable caching for such a backend. The configuration
helper hashes configuration without retaining it or configuring the callback.

Native trace schema 1.1 remains unchanged. Metadata key `agentloop.judgments` is:

```json
{"schema_version": "1.0", "records": []}
```

Each record contains these required contract fields:

| Field | Meaning |
| --- | --- |
| `schema_version`, `evidence_kind` | `1.0`, `semantic_judgment` |
| `request` | `run_id`, typed `spec`, ordered `evidence` objects |
| `judge` | The six `JudgeIdentity` fields |
| `cache_key` | Hash of version, request and judge |
| `evaluation` | `id`, `evaluated_at`, `status`, `value`, `reason`, `usage`, `uncertainty`, `latency_ms` |
| `invocation` | `id`, `cache_hit`, `dispatched`, `latency_ms`, incremental `usage` |
| `record_hash` | SHA-256 of the entire record excluding this field |

Canonical JSON uses sorted object keys, compact separators and finite JSON
values. The Python dataclasses document field names and validate primitive
constraints; `validate_judgment_record` validates the receipt relationships and
hashes. Hashes detect alteration and binding errors, not author authenticity or
truthfulness. Unsupported contract versions remain stored but are not interpreted.

Native JSON, SQLite and OTLP preserve the metadata and provenance. If a transport
removes original source content, the archived receipt remains exportable but its
effective answer becomes stale on the redacted trace. This deliberately avoids
accepting a judgment against evidence that is no longer present.

Run the local synthetic example with
`python examples/semantic_judgments.py --out runs/semantic-judgments`.
It requires no provider credentials and is not empirical optimization evidence.
