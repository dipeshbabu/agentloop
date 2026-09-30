# Incremental trace aggregation

These are **Unreleased source-checkout APIs**. Aggregate native traces one at a
time to summarize a batch without storing all input traces in memory. Aggregates
contain counts, bounded distributions and candidate identifiers, not individual
evidence spans or payloads. They do not generate optimization findings.

```python
from agentloop.aggregate_stats import AggregateConfig
from agentloop.aggregates import TraceAggregate, aggregate_files

config = AggregateConfig(
    latency_bounds_ms=(1, 10, 100, 1000, 10000),
    heavy_hitter_capacity=32,
)
left = aggregate_files(iter(["runs/a.json", "runs/b.json"]), config=config)
right = aggregate_files(iter(["runs/c.json"]), config=config)
left.merge(right)
artifact = left.to_dict()
restored = TraceAggregate.from_dict(artifact)
```

For a generator of native `AgentTrace` objects, use
`TraceAggregate(config).consume(traces)`. A failed `add` or `merge` leaves the
existing aggregate unchanged. Live traces still recording timing are rejected.
Legacy completed traces can use the existing timing fallback; its contribution
is counted separately from explicitly recorded elapsed time.
Latency histograms cover reported runtimes, including those legacy estimates;
check `runtime_basis` before treating a distribution as measured wall time.

## CLI and bounded input reads

```bash
agentloop aggregate traces runs/a.json runs/b.json --out runs/part-1.json
agentloop aggregate traces runs/c.json --out runs/part-2.json
agentloop aggregate merge runs/part-1.json runs/part-2.json --out runs/all.json
```

For a large file list, `--manifest inputs.jsonl` streams one JSON path string per
line. Relative paths resolve against the manifest's directory. A manifest can
also list aggregate partitions for `aggregate merge`. Examples:

```json
"traces/task-001.json"
"traces/task-002.json"
```

Each trace/partition file is read and released individually. Native JSON contains
one trace per file, not an array of traces. Input reads default to a 16 MiB bound;
`aggregate traces --max-bytes N` can change it up to 1 GiB. Manifest lines have a
16 KiB bound. Peak memory includes one input file/trace, the pricing table, and
bounded summary state. This is not a streaming parser for the events inside one
enormous trace. Use retention upstream to reduce such traces.

The default aborts on an invalid or unreadable input. `--skip-invalid` (Python
`on_error="skip"`) counts omitted inputs under `invalid_inputs`, keeps prior
valid contributions and includes no raw error messages in the artifact. An
incompatible trace is also an invalid input under this explicit mode. Iterator
failures and invalid manifests abort; they are not interpreted as successful
end-of-input. Outputs must differ from all inputs and the manifest. No output is
written until processing succeeds.

## Counts and completeness

- `counts` records observed runs, original event/model/tool/retry counts, reported
  input/output tokens, error spans, declared stage/decision spans and missing
  evidence counts. Operation and token-provenance categories have fixed keys;
  unknown future values contribute to `unknown`/`unspecified`.
- Outcomes separate execution success, failure and unknown. Errors or declared
  workflow failure override success. A trace with neither declared completion nor
  host success remains unknown. Execution success does not establish task quality.
- Token totals retain recorded counts even when provenance is partial or estimated.
  The completeness fields are `None` if any contributing run lacks exact token
  evidence. No missing measurement becomes a fabricated zero.
- Cost separates provider-reported dollars, calculated dollars and unknown calls.
  `known_model_cost_usd` is a lower bound when calls are unpriced;
  `pricing_complete_model_cost_usd` is `None` then. Pricing completeness does not
  make an estimate a provider bill: consult calculated dollars and token
  provenance. These totals exclude infrastructure and unrecorded provider work.
- Stage metrics require supported declared stage IDs. Undeclared events are
  counted separately. Classifier/rule stage outcome buckets are hashed and bounded.
  Stage time is cumulative recorded span time, including nesting/overlap, not
  elapsed wall time. Stage cost covers model calls directly declaring that stage;
  it does not allocate child costs to parents.

Every artifact contains schema `1.0`, configuration, metric semantics, native
retention policy when present, completeness, algorithms and a content checksum.
Raw-trace cost calculations record the frozen pricing-table hash. Retained
snapshots preserve their recorded totals, including historical pricing sources,
without repricing aliases; source/date hashes are themselves bounded candidates.
Snapshots can reflect different historical rates, so their sums are descriptive,
not a common-rate counterfactual.

## Sampling and compacted evidence

The [retention policy](TRACE_RETENTION.md) must match across merged traces and
partitions, including seed, rate, mode, protection and representative settings.
Raw and retained traces cannot be merged implicitly. Different histogram bounds,
candidate capacity, schema or raw pricing-table hashes also reject a merge.
An empty partition adopts the first populated partition's semantics; its invalid
input count still survives.

Counts describe **supplied records**, not unique executions. Input files and
partitions must be disjoint: replaying a file or merging a partition twice counts
it twice. An unbounded deduplication registry would defeat the memory contract.

`observed_record_count` is exact for accepted input records. Population size,
estimated population size and effective population sampling rate remain `None`.
Retention session-prefix counts overlap and are not additive, so the aggregator
does not sum them or inflate totals by `1/sample_rate`. Protected or first-K
representative retention is not an unbiased sample. Keep final collection-frame
metadata separately when a population analysis is required.

For compacted/metric-only/redacted traces, run-level measurements come from the
bound original snapshot. Missing individual stage evidence is counted under
`stage_evidence_missing_runs`; it does not appear as measured zero stage work.
Even full-capture retained snapshots currently lack per-call price-source splits,
so `priced_unsplit` and cost-detail/stage-cost missing counts identify that limit.
Aggregate-only findings, replay and quality analysis remain unavailable; use
complete individual traces for those operations.

## Approximation algorithms

Latency uses fixed upper-inclusive bins: `[0, b0]`, `(b0, b1]`, and so on, with a
final overflow bin. Counts, observed min/max and cumulative sum are retained.
For each requested quantile the nearest-rank observation is located in a bin;
the report gives its conservative lower/upper interval, clipped to observed
min/max. It does not invent a point estimate within a bin. Precision depends on
chosen boundaries and can be poor in wide/overflow bins. Bounds are configurable
and limited to 256; merges require identical boundaries.

Heavy hitters use at most K weighted counters per metric. On a new key when full,
subtract the smaller of the incoming weight and minimum counter from every
counter, dropping zero entries; apply any remaining incoming weight. Accumulated
subtraction bounds the weight lost by any one key. Each returned candidate has a
lower bound and an upper bound capped by total weight. An absent key's weight is
at most `unlisted_key_upper_bound`. Merging adds both error amounts, then feeds
the surviving counters through the same reduction. Candidate membership can vary
with ordering/partition boundaries; error bounds, not identical top-K membership,
are the contract. Overlapping intervals cannot establish a strict ranking.

These are deterministic descriptive approximations, not confidence intervals or
population estimates. Floating sums use ordinary Python arithmetic; last-bit
differences between merge orders are possible. Numeric overflow rejects the
update atomically. Run/count integers grow with their numeric magnitude, while
the number of counters/bins stays bounded. Per-event candidate updates are O(K)
in the full-counter case; K is configurable from 1 to 1024.

Candidate keys hash stage ID/version/kind or decision outcome identity, omitting
payloads and names. Hashes support equality checks, not anonymity against guessing.
Artifact checksums detect mutation, not maliciously forged input. This feature
adds no hosted service, database migration, model call or automatic deployment.
