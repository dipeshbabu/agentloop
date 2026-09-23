# Judgment adapters and frozen benchmarks

AgentLoop owns the [typed judgment contract](SEMANTIC_JUDGMENTS.md), frozen
examples, quality accounting and comparison reports. A rule, classifier, local
model, LLM judge or external typed-decision service supplies a replaceable
implementation. No provider SDK is required by core or the benchmark.

Run the complete offline example:

```console
python examples/judgment_benchmark.py --out runs/judgment-benchmark
```

It compares a deterministic rule, a **fake learned predictor**, a **fake service
transport**, and an unconfigured optional service. All data and inference
implementations are synthetic. Timeout and unlabelled cases remain visible;
the example does not establish real-model quality, remote latency or savings.

## Adapter interface

Every backend implements `JudgmentBackend` from contract 1.0: a `JudgeIdentity`
and `judge(request, *, timeout_s) -> JudgmentAnswer`.

| Adapter | Host supplies | Result |
| --- | --- | --- |
| `LocalCallbackJudge` | Trusted synchronous rule/callback | Native `JudgmentAnswer` |
| `LocalPredictorJudge` | Trusted synchronous predictor and optional known `JudgeUsage` | Scalar output mapped into a native answer, then checked against the request domain |
| `TypedServiceJudge` | Optional synchronous transport and credential-availability declarations | Strict decoded answer object mapped into native answer/usage/uncertainty |

The predictor may wrap any local classifier or model framework. It receives the
native request and keyword `timeout_s`; AgentLoop does not infer token counts,
cost, confidence or calibrated probabilities from its output. A local backend
with no model billing must explicitly report zero if that is known.
The predictor may instead return a native `JudgmentAnswer` to preserve per-call
usage and uncertainty; that answer's metadata takes precedence over the adapter's
static usage declaration.

The typed-service transport receives an owned JSON request containing `run_id`,
`spec` and `evidence`, plus keyword `timeout_s`. It returns a decoded JSON object
with only `value`, `status`, `reason`, `usage` and `uncertainty`. The latter two
use the native dataclass fields. Unknown fields, free-form prose, nonfinite
values and answer objects over 64 KiB are rejected. The transport must bound
network response bytes **before decoding**; this adapter's check is not a network
memory limit. It may wrap an LLM client or an external typed-decision system.

```python
from agentloop import JudgeIdentity
from agentloop.judgment_adapters import TypedServiceJudge

backend = TypedServiceJudge(
    JudgeIdentity.configured(
        "my-adapter", "1", {"prompt_version": "2", "temperature": 0},
        provider="my-provider", model_or_rule="model-name", revision="pinned-revision",
    ),
    transport=my_host_owned_transport,
    credentials_required=True,
    credentials_available=my_host_credential_check,
)
```

The host owns SDK imports, credentials, network destinations, connection limits,
provider budgets and cancellation. Credentials are never stored in these
declarations or automatically loaded from environment variables. An unconfigured
service reports `availability="not_configured"`; a configured service without
required credentials reports `missing_credentials`. Both return a native unknown
abstention without calling the transport, with known zero provider usage. The
benchmark records this availability separately from model abstention/failure
status. The session's dispatch flag records calling the adapter; its usage may
still report `not_dispatched` when the adapter did not call a model/service.

Provider failures, invalid responses and timeouts are explicit unknown/failure
results. Exception messages and arbitrary provider output are not copied into
receipts. Timeouts remain cooperative as defined by contract 1.0; there is no
forced worker termination or hidden retry. Ordinary analysis never executes an
adapter or resolves credentials from saved artifacts.

## Frozen examples and execution

Use `JudgmentExample` and `JudgmentBenchmark` from
`agentloop.judgment_benchmark_types`, and `BenchmarkBackend`,
`run_judgment_benchmark`, `summarize_judgment_benchmark`, and
`judgment_benchmark_markdown` from `agentloop.judgment_benchmarks`.

```python
example = JudgmentExample(
    "case-1", request, expected=True, label_status="independent",
    label_ref="reviewed-labels:case-1", label_version="1",
)
protocol = JudgmentBenchmark(
    "redundancy study", "1", (example,), repetitions=2, seed=7,
    timeout_s=2.0, synthetic=False,
)
bundle = run_judgment_benchmark(
    protocol,
    [BenchmarkBackend("my-backend", backend, category="typed_service")],
    enabled=True,
)
report = summarize_judgment_benchmark(bundle)
```

Supported categories are `deterministic`, `local_predictor`, `llm` and
`typed_service`; these describe comparison targets, not different core contracts.
Set `simulated=True` for fake/model-surrogate implementations and
`synthetic=True` for invented examples. These are caller declarations because
requests contain references and derived summaries, not a dataset provenance
oracle. Use permission-cleared summaries and preserve their source references.

`JudgmentExample` owns an immutable native request. An independently labelled
example requires `expected`, `label_ref` and `label_version`. Boolean/choice/score
labels must satisfy the request's type/domain; probability labels must be actual
booleans representing the observed event. An unlabelled example has no expected
answer or reference. Agreement with another evaluated backend is not an
independent label. Label independence and reference quality remain caller-owned
claims; references are never automatically fetched.

The protocol freezes name/version, examples, labels/references, repetition
count, seed, timeout and synthetic marker. The plan hash additionally binds every
backend's name, category, simulation marker and full judge identity/config hash.
Canonical JSON and SHA-256 detect changed artifacts, not author authenticity.
The execution schedule is shuffled with the fixed seed. All backends receive
the same requests and planned repetitions. Expected answers and label references
are not supplied to a backend. Reordering backend declarations does not change
the schedule. Results retain the actual execution ordinal.

Execution defaults to disabled and requires `enabled=True`. A benchmark session
always disables caching and starts no hidden warmup or retry. The default
`max_invocations=10000` rejects excessive plans before execution; the summary
reader applies the same limit before allocating planned slots. Its additional
`max_pairwise_comparisons=100000` bounds pairwise output allocation. Both limits
can be set explicitly for an appropriately provisioned host. These are call and
allocation limits, not financial spending guarantees. Obtain provider budgets
and enforce them in the host before enabling any real remote transport.

## Availability, quality and comparison

The portable bundle schema is `1.0` with `protocol`, `backends`, `plan_hash`,
`enabled`, `environment`, and `records`. The environment records Python version,
implementation, operating system and architecture without hostnames or paths.
Each record has backend/example/repetition identity,
ordinal, availability and either a native receipt or an explicit execution
failure. Save the bundle as well as the report. The report retains its source
bundle hash and source indices for unmatched records.

Summarization is read-only. It validates native receipt integrity and bindings
to the frozen request and judge declaration. Missing, duplicate, mismatched,
reused-invocation and invalid records cannot produce a known answer. Cached
receipts are rejected because their latency/cost does not measure the scheduled
backend invocation. Extra unmatched records remain in an explicit inventory.
All planned slots remain in the failure/unknown and quality denominators.

Quality is grouped by the exact question, kind, choices and score bounds:

| Kind | Metric | Better direction |
| --- | --- | --- |
| Boolean or closed choice | Accuracy against independent reference labels | Higher |
| Probability | Brier loss against binary event labels | Lower |
| Bounded score | Absolute error divided by the declared score range | Lower |

Each example receives equal weight. A labelled example contributes only after
**all planned repetitions** have known answers; its metric averages those
repetitions. Full `value` is unavailable unless the whole cohort is labelled and
complete. `observed_value` describes only the complete labelled subset, with
coverage, incomplete and unlabelled counts beside it. Missing outputs never
improve the full metric by disappearing from its denominator. No universal
aggregate quality score, automatic winner or production routing decision is
generated.

Probability calibration uses one mean forecast per complete labelled example,
not repetitions treated as independent examples. Ten equal-width bins show
counts, mean forecast and positive frequency. Expected calibration error is the
count-weighted absolute bin difference. At least 20 distinct complete examples
and both binary classes are required by the reporting policy. This minimum is
not a statistical guarantee: metrics remain descriptive, expose full-coverage
status, and never set `calibrated=True`. Small or single-class samples report
insufficient support. Brier loss remains a predictive-quality metric; it does
not itself establish calibration. There is no claim that provider confidence
values are calibrated, and no runtime confidence/policy coefficients are fitted.

Availability and performance are separate from quality. Reports expose planned
and observed invocation counts, failure/timeout/abstention rates, missing
credential/configuration counts, latency mean/median, partial token totals with
basis counts, and known cost with completeness/basis. Timings measure recorded
offline adapter invocations, including failures; unknown or missing measurements
are not zero. Totals that exceed finite JSON numeric range are unavailable.
Host conditions, order, cold starts and network variability still affect timing;
the report makes no causal or deployment performance claim.

Backend disagreement is also separate. Boolean/choice comparisons report the
observed fraction of differing answers; numeric comparisons use mean absolute
difference normalized to the declared range. Per-example/repetition pairs,
coverage and unavailable pairs remain visible. Agreement does not certify
correctness, label independence or safe model substitution.

## Optional external conformance test

Required CI uses only AgentLoop-owned deterministic/fake backends. To exercise a
real integration deliberately, provide a trusted Python `module:factory` that
returns a `JudgmentBackend`, including its own SDK setup and approved budget:

```console
AGENTLOOP_RUN_JUDGMENT_PROVIDER_TESTS=1
AGENTLOOP_JUDGMENT_TEST_FACTORY=my_adapter:create_backend
python -m pytest tests/test_judgment_provider_optional.py -q
```

Set these environment variables using your shell's syntax. The optional test
makes at most one synthetic judgment call with a cooperative 15-second timeout.
It checks contract compatibility, not model quality. Without explicit opt-in it
is skipped. No vendor is privileged by the fixture or required dependencies.
