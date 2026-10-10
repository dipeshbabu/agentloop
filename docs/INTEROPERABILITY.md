# External execution evidence

AgentLoop owns trace analysis and intervention evidence. Harbor owns benchmark
execution and verifiers; Omnigent owns harness orchestration and sessions. Their
artifacts can supply evidence without making either runtime a core dependency.

This document freezes the **internal receipt 1.0 boundary and synthetic fixture
contracts** for [workstream #271](https://github.com/dipeshbabu/agentloop/issues/271)
of [roadmap #270](https://github.com/dipeshbabu/agentloop/issues/270). The
[Harbor ATIF adapter](HARBOR_ATIF.md) adds qualified native projections.
[Job/trial import](HARBOR_TRIALS.md) retains independent verifier outcomes.
[Bounded JSONL import](OTLP_JSONL.md) extends the existing OTel parser.
[Omnigent adaptation](OMNIGENT_OTEL.md) adds session/policy observations and gaps.
[Capability declarations](HARNESS_CAPABILITIES.md), the
[executable offline bench](HARNESS_BENCH.md),
[full-population paired cohorts](CROSS_HARNESS_STUDIES.md) and
[coordination reports](MULTIAGENT_EFFICIENCY.md) extend those import paths.
These Unreleased capabilities are not in the published
`agentloop-profiler==0.7.0` wheel. Optional Harbor plugins, active approval resolution
and research trajectory export remain deferred.

## Existing contracts reused

The inspected AgentLoop source is `69cfcb1cf82061fb70229465e296d821f650283d`.
Native trace schema 1.1 and study manifest 1.0 remain unchanged:

- [AgentTrace](../agentloop/tracer.py) and [AgentEvent](../agentloop/events.py)
  use the existing [native validator](../agentloop/schema.py). Event IDs are
  unique within a trace; event run IDs match their trace. Duration is a required
  finite nonnegative number; status is `ok` or `error`. Timeout/cancellation
  details need metadata or a receipt. Unknown structural fields are dropped on
  native read; metadata survives.
- [Operation kinds](OPERATIONS.md) live in metadata, distinct from legacy event
  types. Agent turns and system observations do not automatically imply model
  calls. Aggregated ATIF calls do not establish independent per-call intervals;
  copied context does not establish new execution.
- [Token provenance](TRACE_SCHEMA.md) distinguishes provider/tokenizer/caller
  counts from unavailable/estimated/unspecified grades. External reports are a
  separate receipt provenance, not permission to claim provider measurement.
  Missing usage stays unknown; cached prompt tokens are a subset.
- [OTel parsing](../agentloop/otel.py) preserves resource/scope/span attributes,
  links, evaluation results and native identities. `trace_from_otel` rejects
  multi-trace batches; `traces_from_otel` keeps boundaries. Missing trace IDs
  currently group together: external adapters must qualify ambiguous identities.
  Span links are relationships, not causal parent edges.
- [Studies](STUDIES.md) accept only their existing fields and native trace paths.
  Pair keys use typed scalar metadata. Missing, ambiguous and unmatched keys
  remain visible. Without task-quality metadata, success means execution only.
  Receipts are not study manifests; cohort adapters must retain missing-trace
  trials in complete inventories and supply independent quality evidence.
- [Interventions](INTERVENTIONS.md) retain original predictions/outcomes and
  trace fingerprints. Receipt snapshots reuse the existing canonical JSON helper
  without changing replay, finding, storage or calibration semantics.

Native timing fallbacks can report empty/untimed traces as zero. The ATIF adapter
uses qualified incomplete native events and sidecars; the current reader displays
missing latency/status/usage as unavailable and refuses incomplete comparisons.
Receipt 1.0 also supports `traces: []` with a reason when no projection is available.
No empty artifact establishes measured zero-cost, zero-latency success.
Older readers must not analyze these projections without qualification support;
see [ATIF compatibility](HARBOR_ATIF.md).

## Receipt 1.0

[ImportReceipt](../agentloop/interoperability/contracts.py) validates a strict
envelope and owns an immutable canonical JSON snapshot. `to_dict()` returns an
independent copy. Unknown structural fields and unsupported receipt versions
fail; source extensions belong in bounded `source_metadata`. Every documented
field is required, with explicit `null` where allowed.

| Field | Contract |
| --- | --- |
| `schema_version` | Exactly `1.0` |
| `receipt_id` | `receipt_` plus SHA-256 of typed identity, source format/version, relative artifact reference and supplied-byte hash |
| `source` | Exactly `system`, `producer_version`, `producer_revision`, `format`, `format_version`, `artifact_reference`, `artifact_sha256`, `trust` |
| `source.system` | `harbor`, `omnigent`, `otel` |
| `source.producer_version`, `source.format_version` | Nonempty string or `null`; receipt validation is not an upstream format validator |
| `source.producer_revision` | Full lowercase 40-character Git SHA or `null` |
| `source.format` | Harbor: `atif`, `harbor_trial`, `otlp_json`, `otlp_jsonl`; Omnigent/general OTel: `otlp_json`, `otlp_jsonl` |
| `source.artifact_reference` | Normalized portable relative reference under the caller's selected source root |
| `source.artifact_sha256` | Lowercase SHA-256 of supplied file bytes, including whitespace |
| `source.trust` | `agent_writable`, `external_reported`, `unknown`; none proves authenticity |
| `external_identity` | Exactly `job_id`, `trial_id`, `task_id`, `task_digest`, `step_id`, `session_id`, `trajectory_id`, `trace_id`; each string or `null`; step ordinals retain their string representation |
| `external_identity.task_digest` | `sha256:<64 lowercase hex>` or `null` |
| `identity_provenance` | The same eight keys; absent IDs require `unknown`; other values use the vocabulary below |
| `traces` | Bounded unique `{run_id, trace_file, trace_sha256}` references; relative paths use the output root, hashes identify exported native file bytes |
| `missing_trace_reason` | With no traces: `missing_artifact`, `invalid_artifact`, `untimed_operations`, `no_execution_spans`, `unsupported_format`; otherwise `null` |
| `outcome` | Exactly the seven fields below; task quality and execution status are independent |
| `completeness` | Exactly `trajectory`, `usage`, `quality`, `parentage`, `timing`, `cost`; each `complete`, `partial`, `missing`, `unknown`, `not_applicable` |
| `relationships` | Bounded `{kind, target_id, basis, resolved}` list; kind `delegation`, `continuation`, `session`, `span_link`; targets are original external identities; no implicit native causal edges |
| `notices` | Bounded `{code, severity, artifact, field, record}` list; bounded lowercase diagnostic code; severity `info`/`warning`/`error`; relative artifact or `null`; field string or `null`; positive record/line number or `null` |
| `source_metadata` | Bounded caller-sanitized JSON object for source attributes, unsupported operations and source aggregates; cannot override native measurements |

Provenance values are `native_observed`, `provider_reported`, `external_reported`,
`calculated`, `inferred`, `synthetic`, `unknown`. Conversion must not promote
them. A hash establishes reproducibility against supplied bytes, not authenticity
of an agent-writable trajectory. Receipt string fields allow 512 UTF-8 bytes;
artifact references allow 4,096 bytes subject to portable-path rules. Metadata
and receipt size are measured on canonical UTF-8 JSON.

### External quality

| Outcome field | Contract |
| --- | --- |
| `execution_status` | `completed`, `failed`, `timed_out`, `cancelled`, `partial`, `missing`, `unknown` |
| `verifier_status` | `available`, `failed`, `missing`, `invalid`, `unknown` |
| `verifier_dimensions` | Source reward names to finite numbers or `null`; negative/fractional dimensions are retained |
| `quality_pass` | Boolean or `null`; without a scoring contract it must be `null` |
| `quality_basis` | `external_uninterpreted_reward`, `configured_thresholds`, `unavailable`, `unknown` |
| `scoring_contract` | `null` or the exact versioned definition below |
| `verifier_isolation` | `shared`, `separate`, `mixed`, `unknown`; recorded isolation is not an honesty guarantee |

A scoring contract contains exactly `schema_version: "1.0"`, a nonempty
`scorer_id`, `rule: "all_gte"`, nonempty `thresholds` of required dimensions to
finite thresholds, and `config_sha256`. The hash is SHA-256 of canonical JSON of
the other four fields. An available verifier with every required dimension
present requires `quality_pass` to equal all exact threshold comparisons;
otherwise `quality_pass` is `null`. Changed hashes, boolean/non-finite thresholds
and undocumented positive-reward shortcuts fail closed. The quality basis with a
scoring contract is always `configured_thresholds`.

For example, `{correctness: 1.0, coverage: 0.5}` fails thresholds requiring both
dimensions to reach `1.0`. A failed verifier stays indeterminate even with stale
positive rewards. Execution failure is separately retained: an optimization gate
must require acceptable execution and verified quality. Completeness describes
coverage of supplied signals, not their honesty or whether rewards pass.

### Identity and diagnostics

`external_id(kind, system, identity)` hashes a sorted tuple of field, exact scalar
type and value. Namespaces remain distinct; string concatenation and integer
`1` versus string `"1"` cannot alias. Importers must include known job/trial/step/
document identities. Session IDs alone do not identify ATIF documents. When IDs
are absent, an importer must explicitly choose stable artifact hashes/references
and retain calculated provenance. No external ID, seed or attempt is invented.

`summarize_receipts` counts receipts, native trace references, missing native
artifacts, available verifiers, quality pass/fail/indeterminate, uninterpreted
rewards, source versions and notice codes. Duplicate receipts fail instead of
inflating denominators. Adapters must separately reconcile planned/discovered
trials, documents, records and invalid parses; receipt counts are not trial counts.

`ImportValidationError` provides the common `code`, `field`, `reason` diagnostic.
Fixed descriptions omit untrusted values. Boundaries can attach normalized source
references and record numbers in the notice shape. Raw decoder messages, exception
stacks, reasoning, payloads and credentials must not be copied into notices.
Bounded metadata still needs minimization by the adapter before receipt creation.

## Safe parsing defaults

[ImportLimits](../agentloop/interoperability/validation.py) and the local JSON
loader use the standard library without either upstream runtime:

| Limit | Default |
| --- | --- |
| JSON document / JSONL line | 8 MiB / 8 MiB |
| Total bytes | 512 MiB |
| Records / trials | 10,000 / 10,000 |
| JSON container depth | 64; configurable up to 128 for decoder safety |
| JSON value nodes per document | 200,000 |
| Trajectories / spans / events per trace | 256 / 100,000 / 10,000 |
| References / path components | 1,024 / 32 |
| Receipt source metadata | 64 KiB |
| Numeric literal length | 128 characters |

Settings must be positive integers; booleans fail. Size and nesting are checked
before recursive decode. Duplicate keys, invalid UTF-8/surrogates, non-finite or
oversized numbers and unsupported Python values are errors. Shared `ImportBudget`
counters support total bytes, records, trials, trajectories, spans and references.
The primitive loader applies document bounds and record/byte budgets; semantic
counts and a JSONL loader remain dependent adapter work.

References cannot be absolute/remote, drive-relative, backslash paths, parent
traversals, non-normalized names, Windows devices or alternate data streams.
Symlink/junction references are unsupported even within the root. Inputs must be
regular files under an explicit root. The loader hashes exact bytes; malformed
records consume read bytes and record count. Total-budget overflow is terminal.
It never reads referenced media or executes scripts, hooks, archives or modules.

Path checks assume a stable local artifact tree. They are not an OS sandbox or
an atomic defense against concurrent filesystem mutation. Snapshot concurrently
written jobs before importing. Payload minimization, reference cycles, format
validation, JSONL conflict merging and trial inventories remain adapter duties.

## Compatibility and known losses

| Signal | Native AgentLoop | Existing OTel JSON | Harbor ATIF boundary | Omnigent OTel boundary |
| --- | --- | --- | --- | --- |
| Timing | Required event duration, optional elapsed | Source bounds and existing fallbacks | Timestamp-only latency unknown; no invented intervals | Turn/tool/guardrail bounds do not establish missing model timing |
| Usage | Per-event counts/provenance | GenAI/OpenInference aliases | Cache subsets, aggregates, multiplicity; root/children not added | Missing executor spans mean unknown coverage |
| Quality | Caller fixtures/versioned evidence | External evaluation metadata | Rewards uninterpreted without hashed scoring | Policy decision does not establish correctness |
| Identity | Unique run/event IDs | One trace per trace ID | Document differs from session; retain job/trial/step scope | Sidecar sessions across distinct traces |
| Lineage | Explicit parent IDs | Links retained separately | Reference relationships; unresolved stays unresolved | Missing cross-process parentage stays unknown |
| Content | Caller capture settings | Supplied attributes preserved without redaction | Omit reasoning/token IDs and avoid media reads by default | Minimize content; DENY/ASK alone proves no enforcement |
| Missing runs | Errors/workflow/evidence metadata | Source error statuses | Complete inventory including no-trace receipts | Coverage gaps remain explicit |

Receipts freeze preservation decisions; dependent adapters must validate the
actual source formats before claiming support. No capability is behavior-verified
here. Harbor's converter documents v1.7 while its trajectory model admits v1.8
audio; converter handling needs separate testing. Synthetic JSONL fixtures are
not converter-produced artifacts.

## Frozen examples and verification

[source_matrix.json](../tests/fixtures/external/source_matrix.json) pins 15 owned
synthetic fixture families, supplied-byte hashes and expected interpretations:

- Harbor `07ad34000e4c481451b1a0ea30a4a09548d6de8b`, release `v0.24.0`:
  [ATIF models](https://github.com/harbor-framework/harbor/tree/07ad34000e4c481451b1a0ea30a4a09548d6de8b/src/harbor/models/trajectories),
  [results](https://github.com/harbor-framework/harbor/blob/07ad34000e4c481451b1a0ea30a4a09548d6de8b/src/harbor/models/trial/result.py),
  [locks](https://github.com/harbor-framework/harbor/blob/07ad34000e4c481451b1a0ea30a4a09548d6de8b/src/harbor/models/job/lock.py).
- Omnigent `713673e9d48cf09cb71fd3a90ae45856ada288d1`, release `v0.17.0`:
  [tracing](https://github.com/omnigent-ai/omnigent/blob/713673e9d48cf09cb71fd3a90ae45856ada288d1/omnigent/inner/tracing.py),
  [declarations](https://github.com/omnigent-ai/omnigent/blob/713673e9d48cf09cb71fd3a90ae45856ada288d1/omnigent/harness_capabilities.py).

Upstream projects are Apache-2.0; fixtures contain owned synthetic data and no
copied implementation code. They cover v1.7/v1.8, media, shared-session embedded
children, continuation/copied context, deterministic/aggregated calls, an
eight-trial mixed job, four Omnigent cases, three JSONL cases and hostile inputs.
Trial config/lock files are field projections, not runnable Harbor configurations.
No live job/provider/benchmark result is claimed. Semantic hostile adapter cases
are explicitly pending; parser/receipt negative cases run now.

[Example receipts](../tests/fixtures/external/expected/imported_receipts.json)
represent a failed trial without a trajectory and a fractional uninterpreted
reward with untimed operations. Neither invents a trace, pass, count, cost or
measured duration.

```bash
uv run --frozen python -m pytest tests/test_interoperability_contracts.py -q -ra
uv run --frozen python examples/external_evidence_contract.py --out runs/external-contract
uv run --frozen python tests/fixtures/external/build_fixtures.py
```

The last command deliberately regenerates fixtures; tests never rewrite frozen
expectations. Source inspection/fakes do not replace final-head CI, installed-wheel
smoke, live integration probes or dependent offline import/report workflows.
