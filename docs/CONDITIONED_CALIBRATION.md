# Workload-conditioned calibration

Every optimization pass predicts how much it will save. Once interventions have
been measured, you can see how far those predictions tend to be off, and
whether the error depends on the kind of task. `agentloop.conditioned_calibration`
learns a simple correction per pass and task cluster. It reports how well that
correction does on task clusters it didn't learn from, and stays inactive until
someone reviews it.

It complements the [estimator calibration](CALIBRATION.md), which validates
preserved prediction and outcome artifacts from a manifest. This module works
on a plain list of measured interventions instead.

```python
from agentloop.conditioned_calibration import approve, evaluate, fit, predict

report = evaluate(observations)              # held-out comparison with the raw predictions
model = fit(observations, as_of="2026-11-25T00:00:00Z", max_age_days=180)
model = approve(model, "review: issue 123")  # inactive until reviewed
predict(model, {"pass": "parallelize_tools", "task_cluster": "retrieval"}, 1200.0)
```

## Observations

Each observation is one intervention:

| Field | Meaning |
| --- | --- |
| `pass`, `task_cluster` | Required. The cluster is the unit used for held-out splits. |
| `model`, `framework`, `environment` | Optional strata |
| `predicted` | The pass's predicted gain. Must be positive. |
| `measured` | The measured gain. A negative value is a real result and is kept. |
| `measured_basis` | `observed`, `estimated` or `missing`. Only `observed` rows are fitted. The others are counted. |
| `success` | Whether the intervention kept quality, from your own checks |
| `confounders` | Anything else that could explain the result. These rows are kept and counted. |
| `observed_at` | When it was measured. With `as_of` and `max_age_days`, older rows are stale. |

## The model

For each stratum, the model is the median of measured ÷ predicted, with a seeded
bootstrap interval. By default the strata are pass and task cluster, falling
back to pass alone. A stratum needs `min_rows` observed rows (default 5) to be
fitted. Smaller ones are reported as `insufficient_rows`, rather than fitted to
a handful of points.

`predict` uses the most specific fitted stratum that matches the context. If
none matches, it abstains instead of extrapolating. Each stratum also reports
its row count, negative results, rows with confounders and success rate.

## Review before use

`fit` returns a model with `active: false`. `predict` refuses an inactive model
unless you pass `allow_unreviewed=True`, which `evaluate` does internally for
its held-out scoring. `approve(model, review_ref)` returns an active copy that
records the review. Nothing activates a fitted correction automatically.

## Checking it helps

`evaluate(observations)` holds out each task cluster in turn, fits on the
others, and predicts the held-out cluster. It reports:
- **Calibrated predictions:** mean absolute error, median relative error,
  interval coverage, and how many rows it scored or abstained on.
- **Raw predictions:** the current heuristic's mean absolute error and median
  relative error on the same rows.
- **Overall:** the success rate, and abstentions per cluster.

If the calibrated errors aren't clearly lower than the raw ones, don't approve
the model. Interval coverage well below 95% means the intervals are too narrow
to trust.

## Limits

- A ratio corrects proportional bias, not every kind of error.
- Task clusters are your own labels. Leakage between clusters, such as near-
  identical tasks under two labels, makes held-out results look better than
  they are.
- Few rows give wide intervals. Fit nothing from one or two interventions.
