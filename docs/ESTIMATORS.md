# Optimization estimates

Use [historical calibration artifacts](CALIBRATION.md) to compare saved predictions
with saved intervention outcomes. The offline tool preserves the original models
and excludes synthetic fixtures from empirical fitting; real calibration still
requires permission-cleared workload evidence.

Every built-in optimization card and diagnosis finding includes an `estimate`
record. These predictions are hypotheses until you implement a change and measure
a candidate run with [replay and quality checks](RESEARCH.md). All current estimators
use `method: "heuristic"` and `calibrated: false`; confidence describes the
recommendation, not a statistical confidence interval.

## Version 1.0 models

Inputs refer to the affected spans. Duration sums can include nested or overlapping
work and should not be interpreted as measured elapsed-time improvements.

| Estimator ID | Formula | Parameters |
| --- | --- | --- |
| `parallelize_tools` | `sum_duration_ms - max_duration_ms` | none |
| `cache_context` | `current_cost_usd * min(max_cost_fraction, repeated_context_ratio)` | `max_cost_fraction = 0.5` |
| `add_schema_validation` | `sum_duration_ms * latency_fraction` | `latency_fraction = 0.8` |
| `batch_model_calls` | `sum_duration_ms * latency_fraction` | `latency_fraction = 0.35` |
| `route_to_smaller_model` | `sum_duration_ms * latency_fraction` | `latency_fraction = 0.25` |
| `split_large_step` | `sum_duration_ms * latency_fraction` | `latency_fraction = 0.20` |
| `runaway_loop` | `sum_duration_ms * latency_fraction` | `latency_fraction = 0.30` |
| `tool_oscillation` | `sum_duration_ms * latency_fraction` | `latency_fraction = 0.40` |

The constants in this table drive the calculation. They are defaults without
empirical calibration, not performance guarantees. Parallelization also retains
the observed timing and declared or inferred safety evidence described in
[Parallelization evidence](PARALLELIZATION.md).

## Read and preserve the evidence

The JSON record contains `estimator_id`, `estimator_version`, `method`,
`confidence`, `calibrated`, `formula`, `parameters`, `inputs`, `assumptions`, and
`unmodeled_metrics`. Inputs snapshot affected duration sum/maximum, span count,
current priced cost, repeated-context ratio, cost status, and token status.
The card's `affected_nodes` and finding's `affected_spans` identify the input spans.
Latency outputs are rounded to three decimal places; cost outputs to six.

`current_cost_usd` and cost savings are `null` when pricing is incomplete.
When tokens are estimated, the input cost retains that estimate and its explicit
`token_status`; it is not a measured cost, and replay cost gates remain
indeterminate without an evaluable token basis. `unmodeled_metrics`
identifies a metric whose legacy zero estimate means no savings were modeled;
it is not evidence that the intervention cannot affect that metric.

Markdown exports and the dashboard show the estimator, assumptions, coefficients,
and uncalibrated label. JSON keeps the full snapshot for reproduction. SQLite and
Postgres persist it with the finding and return that saved snapshot without
rerunning the current estimator. Explicitly re-diagnosing a trace replaces its
current finding payload; archive an export if you need every historical revision.
Older saved findings without an `estimate` remain readable and are not silently
assigned the current model's provenance.

Estimator versions describe savings-model semantics separately from finding-rule
versions. Change the estimator version when changing its formula, parameters, or
assumptions. Future versions may add calibration statistics without changing the
meaning of existing snapshots.

Measured trace values remain under the report/current run fields, and replay
contains the observed baseline/candidate comparison. Predictions stay in
`estimated_after`, card savings, and finding savings. The existing
[`savings_aggregation` selection metadata](SAVINGS_SELECTION.md) still identifies
exact versus approximate compatible-card selection; exact selection does not
calibrate the selected predictions.
