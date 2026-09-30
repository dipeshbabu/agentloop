# Real non-agent CPU workload study

Three application-owned Python/NumPy research pipelines completed actual learned-model
inference: image-feature classification, batch record matching and a multistage
classification workflow with conditional fallback. They are not autonomous agents,
mocked model responses, customer traffic or deployed business decisions.

The repository owner delegated workload/scorer selection in this session. Quality
criteria were frozen from published dataset labels before held-out execution.
Dataset authors supplied ground truth and reuse licenses; they did not approve a
downstream deployment or this optimization protocol.

## Applications and independent criteria

| Application | Actual learned models | Frozen quality criterion |
| --- | --- | --- |
| Banknote features | Five-neighbor classifier; compare batching and a centroid model | Source-class accuracy at least 0.90, with no paired regression |
| Batch record matching | Logistic matcher on agreement features; compare batching and reduced features | Native edge-match F1 at least 0.95, with no paired regression |
| Bean decision pipeline | Centroid model, confidence-margin rule and conditional five-neighbor fallback; compare batching and primary-only routing | Source-class accuracy at least 0.85, with no paired regression |

Sources are [UCI Banknote Authentication](https://archive.ics.uci.edu/dataset/267/banknote%2Bauthentication),
[UCI Record Linkage Comparison Patterns](https://archive.ics.uci.edu/dataset/210/record%2Blinkage%2Bcomparison%2Bpatterns)
and [UCI Dry Bean](https://archive.ics.uci.edu/dataset/602/dry%2Bbean%2Bdataset), each
listed under CC BY 4.0. The banknote integer labels are used literally without an
unsupported semantic mapping of class 0/1. Model confidence values are heuristic
scores, not calibrated probabilities. Matching uses agreement features, not raw
names or dates; identifiers never enter a model.

## Data and execution protocol

Banknote/bean feature-vector groups use fixed 60/20/20 hash partitions, preventing
identical vectors from crossing train/calibration/evaluation folds. Task groups
also exclude repeated feature groups. Matching partitions positive-match record
components, retains pairs only when both endpoints share a fold, and selects
component-disjoint calibration/evaluation rows. Its sampled class balance does
not represent production prevalence. Raw identifiers are omitted from published
model inputs; hash references are not an anonymity guarantee.

Training uses its own fold only, at most 4,096 rows per model. Transforms, learned
parameters, source/model hashes, tasks, scorers and thresholds are frozen before
inference. Gold labels and split/group fields are stripped from inference inputs.
Each workload has eight fitting and 32 held-out tasks, repeated twice under both
candidates: 96 fitting and 384 held-out pairs. Banknote/bean tasks contain eight
rows and matching tasks 16 pairs. Two unscored training-input warm-ups per model
and model loading occur outside task timing.

Original baseline findings, per-candidate selected/rejected inventories and a
content-bound timestamp journal are written before candidates. All candidates
run. Numeric models have no language-token usage: native provenance remains
unavailable/not applicable, operating cost is unknown, and paid-provider spend
is separately zero. There are no provider API calls or invented dollar savings.

## Held-out observations

| Application / candidate | Quality accepted / 64 | Paired quality regressions | Mean instrumented time change | Mean recording-disabled time change |
| --- | ---: | ---: | ---: | ---: |
| Banknote / batched | 64 | 0 | -1.1250 ms | -0.5706 ms |
| Banknote / centroid | 12 | 52 | -0.6460 ms | -0.7058 ms |
| Matching / batched | 64 | 0 | -2.5553 ms | -0.9626 ms |
| Matching / reduced features | 58 | 6 | +0.1464 ms | +0.0106 ms |
| Bean workflow / batched | 48 | 0 | -1.4230 ms | -0.7250 ms |
| Bean workflow / primary only | 46 | 6 | -1.9280 ms | -1.8681 ms |

Negative time changes mean faster candidates. Batching preserved predictions,
but 16 bean pairs still failed the absolute quality floor because their baseline
already fell below it. Primary-only bean routing slightly improved mean accuracy
while regressing on six pairs; those failures remain visible. Faster low-quality
results are not accepted optimizations, and these observations authorize no rollout.

Recorded and recording-disabled outputs must agree exactly. Both timing series
and recorder overhead are retained; instrumented savings are not presented as pure
application gains. Task-cluster intervals average repetitions within tasks. Fixed
order, shared hardware, small public-data groups and numeric precision limit
general/causal claims. Setup steps are recorded; operator minutes were not timed.

## Finding and calibration feedback

The native finding benchmark supplies 192 held-out first-repetition label cases:
102 justified batching findings across 96 task cases, plus 288 unscored routing
findings whose required token evidence is unavailable. The latter are not counted
as correct predictions.

All 1,944 selected/rejected calibration registrations remain present. Only isolated
banknote/matching batching outcomes are attached for per-finding latency diagnostics.
Multistage and cheaper-model changes remain outside attribution scope even when
only one finding happens to be emitted. Their native ledgers and quality outcomes
remain available, with explicit exclusion reasons. No coefficient or runtime policy
changes. The existing #209/#189 contracts are reused rather than replacing them.

## Reproduction

This is **Unreleased source-checkout** research tooling. Actual inference used
Python 3.13.12 and NumPy 2.5.1, requesting one OpenBLAS/OMP thread. Models are stored
as content-bound JSON parameters, not pickle. Selected training/task data and
attributions are retained; full source archives remain at pinned public URLs.

```bash
uv run --frozen --all-extras python -m examples.non_agent_study.data --sources SOURCE_ZIPS --out-dir PREPARED
uv run --frozen --all-extras python -m examples.non_agent_study.protocol --prepared PREPARED --out-dir PLAN --source-revision SOURCE_COMMIT
uv run --frozen --all-extras python -m examples.non_agent_study.run PLAN/protocol.json --phase fit --out-dir FIT
uv run --frozen --all-extras python -m examples.non_agent_study.run PLAN/protocol.json --phase held_out --out-dir HELD_OUT
uv run --frozen --all-extras python -m examples.non_agent_study.export --plan PLAN --fit FIT --held-out HELD_OUT --out-dir EVIDENCE
```

Use fresh destinations. Changed code/models/criteria require new evidence. Exact
timings are not guaranteed across machines. Offline reconstruction rechecks frozen
labels, trace/prediction/ledger bindings, saved finding labels and native calibration.
Calibration portability excludes only absolute source paths and their outer hash;
original artifacts remain unchanged. Execution and reproduction source snapshots
are separate when export/packaging is corrected after measurements.

Large native artifacts use bounded gzip chunks inside checksummed multipart
archives. Restoration rejects unsafe/duplicate paths, verifies every restored
file and caps declared logical bytes at 512 MiB. It never overwrites a destination.
The [published report and evidence](../research/non-agent-2026-09/README.md) include
the frozen protocol, machine-readable results, dataset attributions and exact
restoration/reconstruction commands.
