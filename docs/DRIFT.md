# Reviewed-window drift reports

These are **Unreleased source-checkout APIs**. Drift compares an explicitly
reviewed evidence window and its thresholds with a later window. It produces
local descriptive evidence; it does not notify anyone, deploy changes or roll a
workflow back. A distribution change is not itself a correctness change.

## Collect and review a window

```python
from agentloop.drift import ReviewedBaseline, compare_drift
from agentloop.drift_types import DriftRule, QualityMetric, WindowSpec
from agentloop.drift_windows import WindowBuilder

builder = WindowBuilder(
    WindowSpec("baseline-september-1", "v1",
               "2026-09-01T00:00:00Z", "2026-09-02T00:00:00Z",
               "workload:v1", "config:v1", "model:v1"),
    quality_metrics=(QualityMetric("accuracy", "quality", "v1", "scorer:exact-match-v1"),),
)
# Supply completed traces from the declared window and independent host observations.
for trace, score in completed_scored_traces:
    builder.add(trace, cohort="standard", timeout=False, abstention=False,
                quality={"accuracy": score})

reviewed = ReviewedBaseline(
    builder.to_dict(),
    rules=(DriftRule("latency_p95_ms", relative_pct=10, noise_margin=5),
           DriftRule("quality:accuracy", direction="decrease", absolute=0.02)),
    review_ref="review-record:approved-baseline-42",
)
pinned_hash = reviewed.sha256
report = compare_drift(reviewed, later_window, expected_baseline_sha256=pinned_hash)
```

Window membership uses completion timestamps in `[started_at, ended_at)`. Native
traces must have timezone-aware completion times inside that range. Each window
declares workload, configuration and model version references. Collection is
serial; do not mutate source traces while adding them. Failed additions leave the
builder unchanged. Use disjoint records and partitions; no unbounded run-ID
deduplication registry is maintained.

`ReviewedBaseline` owns an immutable JSON snapshot of the window **and rules**.
Mutating an exported dictionary cannot modify the baseline. Its hash identifies
the exact reference used in each comparison. The CLI requires that hash when
comparing and writes new files exclusively. Changing thresholds requires a new
explicit baseline review. `review_ref` records a host review assertion; AgentLoop
does not authenticate the reviewer or treat it as deployment approval.

## CLI artifacts

A collection manifest contains a version, window declaration and source records:

```json
{
  "schema_version": "1.0",
  "spec": {
    "window_id": "baseline-september-1", "version": "v1",
    "started_at": "2026-09-01T00:00:00Z", "ended_at": "2026-09-02T00:00:00Z",
    "workload_version": "workload:v1", "config_version": "config:v1",
    "model_version": "model:v1"
  },
  "quality_metrics": [
    {"name": "accuracy", "kind": "quality", "version": "v1", "source_ref": "scorer:exact-match-v1"}
  ],
  "records": [
    {"trace": "traces/task-1.json", "cohort": "standard", "timeout": false,
     "abstention": false, "quality": {"accuracy": 1.0}}
  ]
}
```

Paths resolve against the manifest's directory. Quality scores are normalized to
`[0,1]`; `None`/omission means unknown. `kind="proxy"` records a proxy separately
from a quality label. Scorer/version/source references are mandatory declarations,
not a claim that the profiler independently verified the labels. Timeout and
abstention annotations are optional booleans; missing values never become false.

Create a rules JSON list, for example:

```json
[
  {"metric": "latency_mean_ms", "relative_pct": 10, "minimum_observations": 20},
  {"metric": "quality:accuracy", "direction": "decrease", "absolute": 0.02}
]
```

```bash
agentloop drift collect baseline-manifest.json --out runs/baseline-window.json
agentloop drift baseline runs/baseline-window.json rules.json \
  --review-ref review-record:42 --out runs/reviewed-baseline.json
agentloop drift collect current-manifest.json --out runs/current-window.json
agentloop drift compare runs/reviewed-baseline.json runs/current-window.json \
  --baseline-sha256 HASH_PRINTED_ABOVE --out runs/drift-report.json
```

The comparison writes JSON with baseline/current IDs, versions, bounds and hashes,
metric/cohort denominators, observed intervals, deltas, reviewed thresholds,
completeness and reasons. Console output lists statuses. Successful generation
exits zero even when a report contains alerts; the command is not a pager or a
deployment gate. Existing output files are never overwritten.

Manifests allow up to 100,000 records within the 16 MiB input-file bound. For larger
exports, use the iterator-based Python API or [aggregate partitions](INCREMENTAL_AGGREGATION.md).
A record can instead provide `aggregate`, `cohort` and `time_assignment_ref`.
The reference is an explicit host assertion assigning that partition to the
window; aggregate artifacts do not retain individual completion timestamps.
`WindowBuilder.add_aggregate(..., time_assignment_ref=...)` provides the same API.
Time-membership denominators distinguish verified native completion times from
host-assigned aggregate rows. Per-run quality/timeout/abstention annotations cannot
be broadcast over aggregate partitions and remain missing.

## Metrics and evidence

| Rule metric | Evidence and scope |
| --- | --- |
| `latency_mean_ms`, `latency_p95_ms` | Reported run time; p95 is a fixed-bin interval, not an invented point estimate. Timing basis is retained. |
| `input_tokens_mean`, `output_tokens_mean` | Recorded model usage; incomplete/estimated token provenance prevents a complete mean. |
| `known_cost_mean_usd` | Recorded model cost, with provider/calculated source totals and token completeness shown separately. Unpriced calls make the complete mean unavailable. |
| `failure_rate` | Declared execution/task failure among known success/failure outcomes; unknown outcomes remain outside its observed denominator. |
| `timeout_rate`, `abstention_rate` | Explicit per-run host flags, with missing flag counts visible through coverage. |
| `quality:NAME` | Independent caller-supplied normalized score or proxy and its version/source declaration. |
| `model_mix`, `stage_mix` | Distributions of recorded model identities and declared stage ID/version pairs. |
| `outcome_mix`, `decision_outcome_mix` | Declared workflow outcomes and classifier/rule stage outcomes respectively; separate observation denominators. |
| `finding_incidence` | Fraction of runs with any native finding, only when collection explicitly enables `include_findings`. |
| `instrumentation_complete_rate` | Fraction with supported individual evidence and no capture-schema, boundary, parentage, operation or usage gaps detected by first-use validation. |

Capture completeness describes the supplied evidence; it cannot detect every
uninstrumented operation. Privacy warnings are separate. Finding incidence is
optional because rule analysis needs original spans and extra computation; it is
unavailable on lossy retained traces, aggregate-only inputs or traces above 2,000
events. Collector rule IDs/versions are recorded and must match. Quality presence
and coverage are reported even when no quality rule was requested. Performance
or outcome changes never fill in absent quality labels.

Window state is bounded to 32 explicit cohorts, 16 quality metrics and 128 distinct
keys per distribution. Keys are hashes of identifiers/outcomes, not payloads.
Overflow and missing identities are counted; distribution comparisons with
overflow or missing individual evidence are indeterminate. Native synthetic/real
producer markers survive separately, while absent markers and aggregate-only
sources remain unspecified. Markers are producer assertions, not authentication.
Source hashes and a rolling source chain preserve identity without retaining
individual payloads. Keep the source artifacts for audit/reproduction.

## Threshold semantics and limitations

Rules select an increase or decrease direction. Absolute thresholds use the metric
unit (rates/scores use fractions, so `0.02` is two percentage points). Relative
thresholds use percent of the baseline. If both are supplied, a change must exceed
both: the effective allowance is their maximum. A zero baseline makes a purely
relative threshold indeterminate. Distribution drift uses total variation distance
on the observed categories and requires an absolute increase threshold.

The default requires at least 20 runs and 20 metric observations on each side,
with 100% evidence coverage. Many spans from one run do not satisfy the run floor.
`minimum_coverage` can explicitly relax label/flag coverage, but comparisons then
describe the observed subset and can be biased. Incomplete token/cost totals still
remain unavailable. Matching retention policies do not prove equal populations;
the report concerns supplied records, never an estimated traffic population.

`noise_margin` is a reviewed deadband in metric units, not an estimated confidence
interval. An alert requires the entire worsening interval to exceed allowance plus
margin. A within-bounds result requires the entire interval to fit below allowance
minus margin (floored at zero). Intermediate changes are indeterminate. Wide
histogram bins can therefore prevent a p95 alert even when its true value changed.
No significance test, universal anomaly model or causal conclusion is implied.

Overlapping/nonlater windows, unmatched cohorts, changed sampling/retention/pricing
or aggregation configuration, changed runtime basis, and changed quality/rule
definitions are incomparable where relevant. Workload/config/model version
changes are surfaced as potential confounders. Cohorts are evaluated separately;
there is no pooled headline that hides a cohort regression. Overall status reports
the reviewed rules only: an alert takes priority, otherwise incomplete comparisons
produce indeterminate. Within bounds does not certify quality or rollout safety.

Run the maintained offline synthetic regression/recovery example:

```bash
uv run --frozen python examples/drift_demo.py --out-dir runs/drift-demo
```

It retains the reviewed baseline, later windows and both reports, producing an
alert and then recovery to within bounds. Its fixed synthetic timings and labels
test report behavior; they are not empirical deployment results. No provider,
notification service, scorer callback, rollback or paid work is invoked.
