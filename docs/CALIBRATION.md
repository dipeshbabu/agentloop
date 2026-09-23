# Historical intervention calibration

`agentloop study calibrate` compares **saved predictions** with **saved outcomes**.
It reads the existing intervention ledger and source traces; it never reruns
today's estimator, reprices past calls, executes an agent, or changes a runtime
policy. Ordinary interventions can be calibrated without harness controls.

This supplies the offline portion of
[#189](https://github.com/dipeshbabu/agentloop/issues/189). Empirical calibration
still requires the permission-cleared workloads, scorers and outcomes in
[#181](https://github.com/dipeshbabu/agentloop/issues/181); harness cohorts also need
the real [ablation study](HARNESS_ABLATIONS.md). No real-workload calibration
coefficients or benefit claims are shipped with this change.

## Run the fixture example

```bash
uv run python examples/intervention_calibration.py --out runs/intervention-calibration
uv run agentloop study calibrate runs/intervention-calibration/manifest.json --out runs/intervention-calibration/report.md --json-out runs/intervention-calibration/report.json
```

The [example](../examples/intervention_calibration.py) creates original finding
snapshots, native traces and existing intervention records for successes,
failures, missing quality, an unselected change and a missing outcome. All six
registrations are explicitly synthetic and therefore excluded from empirical
summaries and fitting. Their raw illustrative errors remain visible in JSON.
An empty empirical result is expected: fixtures verify the contract and do not
calibrate an estimator for real agents.

The Python API is `summarize_calibration(manifest_path)` and
`calibration_to_markdown(report)` in `agentloop.calibration`.

## Register predictions before candidates

Archive each original diagnosis finding before running its candidate. Retain
rejected and unselected findings as well as selected ones, with decision reasons.
Later, attach the ledger record and exact source traces for any executed outcome.
The manifest is an offline inventory, not an experiment runner or dataset host.

Top-level manifest fields are:

| Field | Meaning |
| --- | --- |
| `schema_version`, `name` | Version `1.0` and an experiment name |
| `as_of`, `valid_until` | Explicit ISO-8601 times with timezones; freshness is assessed at the declared `as_of`, not an implicit live clock |
| `fit_method` | `none` for historical error summaries, or explicit diagnostic `scale` fitting |
| `min_fit_tasks` | Integer of at least two; a floor for identifiability, not evidence of adequate study power |
| `selection_inventory_complete` | Host declaration that rejected/unselected cases are included; it does not prove unbiased sampling |
| `bootstrap` | Positive `samples` up to 10,000, integer `seed`, and `confidence` strictly between zero and one |
| `registrations` | Complete case inventory, including missing outcomes; an empty list is valid |

Each registration has these fields:

| Field | Meaning |
| --- | --- |
| `case_id` | Unique record identity |
| `task_id`, `task_sha256`, `repetition` | Typed task/repetition identifiers (nonempty string or integer) and SHA-256 of the complete frozen task |
| `split` | `fit` or `held_out`; repetitions of a task cannot cross splits |
| `selection`, `selection_reason` | `selected`, `rejected` or `unselected`, and a bounded reason label |
| `synthetic` | Boolean fixture marker; markers on source traces or ledger records also apply |
| `context` | The versioned cohort declaration below |
| `prediction` | The entire original finding snapshot, including its `finding_id`, `estimate` and `savings`; older incomplete snapshots remain visible but cannot gain fabricated provenance |
| `prediction_recorded_at` | Archive time, or null when unknown; eligible predictions must precede the candidate trace's start and recorded outcome |
| `outcome` | Null for no recorded attempt, otherwise `record`, `baseline_trace`, `candidate_trace`, `recorded_at` and overall `status` |

Outcome paths resolve relative to the manifest and may be null when unavailable.
Statuses are `completed`, `failed`, `cancelled`, `timed_out`, `stopped` and
`incomplete`. Missing or invalid referenced artifacts remain as categorical
unknowns; malformed manifest declarations fail explicitly. A rejected/unselected
registration cannot simultaneously claim an executed outcome.

Task IDs retain their types: integer `1` does not alias string `"1"`. Source trace
metadata must match `task_id` and `repetition` (or the existing `seed` field when
`repetition` is absent). Hash the full task, including constraints; predictable
hashes are not anonymization. The same task cannot change its digest or cross
splits, and identical task inputs cannot inflate the task count under new IDs.
For workload-level holdout, assign all tasks of each held-out workload to that
split and keep the workload contexts separate.

Archive timestamps and inventory declarations need independent runner/version
control evidence. Their presence is not proof of preregistration, permissions,
experimental control, or an unbiased task sample. Do not retrospectively replace
an original snapshot with an estimate generated after seeing the candidate.

## Keep cohorts distinct

`context` contains `workload`, `model`, `provider`, `environment`, `scorer`,
`policy`, `cost_basis`, `pricing`, `quality_gate` and `intervention`.

- Use immutable version/configuration references for workload, model/provider,
  environment and scorer. The scorer reference must cover its configuration too.
- `policy` is null for ordinary interventions, or contains `id`, `version` and
  the policy's `config_hash`. A harness cohort requires matching native enforced
  decision evidence with no reported capture gaps. Observing that policy does
  not establish complete coverage or an individual causal policy effect.
- `cost_basis` is `provider_reported`, `calculated`, `mixed` or `unknown`.
  `pricing` is an immutable pricing-snapshot reference or null; calculated-cost
  calibration requires that reference.
- `quality_gate` contains `min_score` and `max_regression`, both in `[0,1]`.
- `intervention` freezes the planned `type` and JSON-object `configuration` before
  selection/outcome collection. They must match any attached ledger record. This
  keeps missing outcomes in the intended cohort and separates different applied
  settings even when their estimator and workload are the same.

Cohort identity also includes the original estimator ID/version, method, formula
and parameter snapshot. Different estimator parameters, model/provider versions,
policy settings, environments, pricing bases or quality rules are never silently
pooled. The original finding and ledger remain unchanged in the artifact.

The original prediction must exactly match the ledger's saved finding. Source
IDs, typed pairing metadata and trace fingerprints must match. Both canonical
exported JSON and its compatible normalized trace form are recognized, so an
integer duration becoming a float on import does not invalidate an unchanged
historical export. Stored ledger identities are never rewritten. Combined
multi-finding interventions remain unattributed rather than assigning their
whole effect to each finding. A pairing key includes cohort, task, repetition and
original finding identity. Duplicate outcomes or pairing keys are ambiguous;
all copies are retained and excluded from empirical calculations. Different
findings on one task remain distinct observations, while the task still receives
one statistical weight.

## Errors, quality, and denominators

For each modeled metric:

```text
realized savings = saved baseline value - saved candidate value
signed error     = original predicted savings - realized savings
```

Values retain the stored measurement precision. Positive error means
overprediction; negative realized savings mean a regression.
The realization ratio is undefined when the original prediction is zero. A
legacy zero marked in `unmodeled_metrics` becomes an unavailable prediction,
not a claim that the intervention had no effect. Unknown inputs and incomplete
measurements never become zero. Raw values remain visible even when a comparison
is ineligible; aggregate calibration metrics require both prediction and outcome.

Latency uses saved elapsed-time values and completed source timing. Cost requires
complete saved measurement provenance. Provider-billed and calculated cohorts
remain distinct. Billed charges must have numeric per-model source metadata
(`provider_reported_cost_usd` or `cost_usd`) consistent with the saved totals;
they do not require exact token counts. Calculated costs require exact/empty
token provenance, no conflicting billed amounts, and the declared pricing
snapshot. Mixed, partial and unknown costs abstain. No current pricing lookup
is used to reconstruct a past amount.

The independent quality gate requires a completed overall outcome, explicit
task success on both source traces, and saved scores meeting the configured
minimum/regression bound. Scores come from the saved quality report when present,
otherwise the saved replay's explicit quality-score fields. A passing performance
gate does not imply task quality. Missing scores/success are indeterminate.
Recovered event errors are retained separately; the host supplies overall
execution status and task outcome. Decimal score thresholds use their declared
values without binary-subtraction boundary errors.

Every cohort/split discloses registered, selected, rejected/unselected, synthetic,
missing, ambiguous and quality-gate counts. Quality acceptance is among **all
selected non-synthetic registrations**, including failures and unknown outcomes;
zero selected cases produces an undefined rate. That conditional acceptance rate
is not general finding precision. Metric summaries show known/missing observation
and task counts against the full non-synthetic registration denominator.

Raw error/savings summaries retain measurable failures and unknown quality.
Separate quality-preserving summaries use only independently gate-accepted
outcomes and retain their full denominator. A run that ends earlier by failing
cannot become a quality-preserving win. The raw resource model may nevertheless
describe that shorter failed attempt; its predictive error is a different
question from whether the change was useful.

## Optional diagnostic fitting and uncertainty

`fit_method: "none"` leaves the original predictions as the only predictor.
Explicit `scale` fitting averages paired prediction and realization within each
eligible fitting task, then fits one multiplicative factor through the origin:

```text
factor = sum(task_mean_prediction * task_mean_realization)
         / sum(task_mean_prediction ** 2)
```

Tasks receive equal weight; adding seeds to one task does not give it extra fit
weight. All measurable fitting outcomes, including failures, enter this **raw
resource-savings** fit. It is not fitted on successes alone and is not a utility
or quality model. Zero denominators, insufficient task counts and unrepresentable
factors produce explicit unavailable states. Negative factors are retained.
The original coefficient snapshots are unchanged, and no runtime consumes this
factor automatically.

Held-out tasks never enter fitting. Reports keep in-sample and held-out original
and scaled errors separate, and mark cohorts without evaluable held-out outcomes.
Intervals resample equally weighted task means within each cohort/split rather
than treating seeds as independent tasks. Fewer than two evaluable tasks produces
`insufficient_tasks`. Scaled-error intervals condition on the fitted factor;
they do not quantify fitting uncertainty or refit inside each bootstrap sample.
Small samples, selection, missingness, multiple comparisons, scorer errors and
nonrepresentative workloads still limit conclusions. These are descriptive
intervals, not a universal confidence guarantee or automatic significance gate.

## Freeze and review the artifact

The JSON preserves the manifest, original snapshots/ledgers, every registration,
exclusion reason, cohort identity, denominators, source paths and report hashes.
Rerunning unchanged artifacts at the same paths is deterministic. Resolved source
paths are part of the exported artifact, so relocation changes that artifact's
hash even when numerical summaries are unchanged. The CLI rejects output paths
that resolve to consumed source files or to each other.

Review the explicit `as_of` and `valid_until` when using a saved report later.
Expiry is assessed at the declared evaluation time; a historical artifact does
not refresh itself. A new model, provider configuration, workload, policy,
environment or quality gate requires renewed evidence. Preserve task/scorer
definitions and permissions alongside the bundle. Original ledger metadata can
contain caller-provided content, so review it before publishing a real study.
