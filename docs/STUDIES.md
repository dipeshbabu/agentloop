# Paired study summaries

[Structured quality](STRUCTURED_QUALITY.md) adds versioned decision, extraction
and matching evidence to these same paired summaries, retaining unscored cases.

[Offline model substitution](MODEL_SUBSTITUTION.md) exports explicit candidate
trials into this format with frozen decision-step usage, independent quality and
retained failures. Its measurement scope is kept separate from model-only traces.

[Historical calibration](CALIBRATION.md) compares frozen intervention predictions
with outcomes, using separate fitting/held-out tasks and unchanged source ledgers.

For frozen four-condition runtime-control comparisons with task-level uncertainty
and explicit planned denominators, see [harness ablations](HARNESS_ABLATIONS.md).

`agentloop study summarize` compares recorded conditions across tasks and
repetitions. It runs offline with the Python standard library. It does not run
agents or assign a universal significance decision to an experiment.

## Manifest 1.0

Save this JSON manifest beside your trace directories:

```json
{
  "schema_version": "1.0",
  "name": "Context compression study",
  "baseline": "baseline",
  "conditions": {
    "baseline": ["baseline/*.json"],
    "compressed": ["compressed/*.json"]
  },
  "pairing_keys": ["task_id", "seed"],
  "bootstrap": {"samples": 1000, "seed": 7, "confidence": 0.95}
}
```

Each condition contains one or more native trace paths/globs, resolved relative
to the manifest. At least two conditions are required; each is compared with
the named baseline. A pattern matching no files, malformed trace, unknown
manifest field/version, or duplicate run ID is an explicit input error.
Overlapping globs within a condition include each resolved path once. The same
run ID cannot appear twice, including across conditions.

Each trace should record pairing values in its top-level `metadata`:

```json
{"task_id": "task-01", "seed": 7, "success": true, "quality_score": 0.9}
```

Pairing values are strings, finite numbers, or booleans; their types are part of
the key. Missing, null, or compound values are reported as invalid pairing
metadata. Include a repetition key when task and seed alone are not unique.
If a pairing key has multiple runs on either side, all runs at that key are
reported as ambiguous and excluded from that paired comparison. Unmatched and
ambiguous runs still contribute to their condition's descriptive summary.

## Generate reports

```bash
agentloop study summarize study.json --out study.md --json-out study-results.json
```

JSON includes the manifest, ordered run identities and source paths, condition
summaries, matched pairs, per-pair deltas, and unmatched reasons. Markdown shows
the condition/paired tables and unmatched run IDs. Output order follows condition
names, typed pairing keys, and run IDs, independently of file creation order.
Missing pairs are reported without making the command fail; invalid inputs exit
nonzero. This supports studies where incomplete observations need inspection.

Metrics include success, quality score, elapsed latency, input/output tokens,
model/tool/retry counts, and evaluable cost. Each summary contains available
`count`, `missing_count`, mean, median, minimum, maximum, and P05/P25/P75/P95
quantiles. Quantiles interpolate at `(n - 1) * p`. No observations produce
`null` statistics; a single observation has identical quantiles.

All paired deltas are **candidate minus baseline**. Negative latency/cost deltas
mean reductions. Success and quality deltas use fractions, so `0.1` means ten
percentage points. The mean of paired deltas uses only matched observations; it
can differ from the difference between condition means when runs are unmatched.

## Interpret evidence carefully

- `metadata.success` is optional task evidence. Any error span makes execution
  unsuccessful even if metadata says success. Without task metadata, success
  means only that no error span was recorded. `success_basis_counts` exposes that
  distinction; an empty trace does not prove successful task completion.
- Optional `metadata.quality_score` must be a number in `[0, 1]`. Missing scores
  remain missing. Supply scores from a documented evaluation; study aggregation
  does not validate the evaluator's coverage.
- Cost statistics include only runs with complete/empty pricing and an evaluable
  token basis, following replay's legacy-provenance compatibility policy. Partial
  or unknown costs never become zero. Per-pair cost requires both sides to be
  evaluable; counts disclose excluded observations. Token totals retain their
  reported values, with `token_status_counts` identifying approximations.
- `failure_categories` counts error spans by `event.metadata.error_type`, falling
  back to `event_error`. An explicit failed task with no error span contributes
  `task_failure`. Multiple failed spans may belong to one failed run.
- Normalized operation counts are preserved alongside legacy call counts.
  Synthetic trace metadata produces an explicit fixture-data notice.

Omit `bootstrap` to disable intervals. When requested, each metric resamples its
available **paired deltas** with replacement and reports a percentile interval
for the mean. The default is 1,000 resamples, seed 0, and 95% confidence; accepted
resample counts are 1–10,000. Fewer than two evaluable pairs yield an
`insufficient_pairs` result, not an invented interval.

The bootstrap assumes independent, exchangeable pairs. Multiple seeds or
repetitions of the same task may require a cluster-aware analysis, and many
condition/metric comparisons may require a multiple-comparison plan. Intervals
do not resolve selection bias, missing-data bias, evaluator errors, or dataset
representativeness. Report the pairing rule, unmatched cases, denominators,
seed/resample count, and evaluation method with the results.

## Offline research example

```bash
uv run python examples/paired_study.py --out runs/study-demo
agentloop study summarize runs/study-demo/study.json --out runs/study-demo/study.md --json-out runs/study-demo/results.json
```

[The example](../examples/paired_study.py) writes deterministic synthetic traces
for three tasks, two repetitions, and two conditions, including a failed candidate
and an unpriced model. The resulting report demonstrates the analysis format;
its timings and quality scores are fixtures, not benchmark claims.
