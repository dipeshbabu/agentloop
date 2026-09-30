# Cross-workload usefulness benchmark

This benchmark evaluates a fixed AgentLoop analyzer on five maintained reference
workflows and three archived real-agent workloads. Reference executions are new;
real-agent analysis is explicitly retrospective and makes no new model calls.
Results remain separate by workload, candidate, phase and finding family.

The implementation requires an **Unreleased source checkout**. The protocol pins
the analyzer's package version, source revision and normalized hashes of its
Python sources, plus the benchmark runner, fixture code, tasks, scorers, pairing
keys, candidate variants and split policy. A package version alone is insufficient
to identify this source-checkout build.

## Frozen inputs and sequence

Reference workloads cover email classification/routing, marketplace processing,
fictional payment review, incident triage and batch extraction/classification/
matching. The complete fixture sets and every declared candidate variant are
included, including deliberately weak and failing reference controls. Their
backend token units, reported prices and semantic judges are synthetic and do not
measure an actual model provider.

Email and payment are development groups; marketplace, incident and batch are
evaluation groups. Whole workloads stay in one split. These maintained public
fixtures are regression evidence, not blind unseen data. No detector, coefficient,
candidate or scorer is tuned against the evaluation outcomes during this run.

For each reference case/repetition the runner:

1. Executes matched recorded and recording-disabled baseline controls in alternating order.
2. Runs any declared fixture semantic inspection, then records analysis time.
3. Writes the baseline, original diagnosis, selected/rejected findings and content-bound timestamp receipt.
4. Executes all frozen candidate variants only after that receipt exists.
5. Scores independent expected outputs and writes native quality, replay, study-compatible scored traces and finding-linked intervention records where matching findings exist.

Candidates with no matching finding are retained as unattributed experiments;
no finding ID is invented. Combined configuration effects are not allocated to
individual findings. The first repetition supplies independent label cases for
the [finding-quality methodology](FINDING_BENCHMARKS.md); all repetitions remain in
resource, quality and emission inventories.

## Real evidence and original predictions

The [real-agent study](REAL_AGENT_STUDY.md) provides repository-question, UCI wine
SQL-analysis and GSM8K calculator-agent traces. Both the revised pilot and held-out
evaluation remain present, along with the retired response-contract pilot's six
recorded baselines and six unexecuted candidate slots. Original task/model/scorer/
implementation versions and original selected/rejected findings are retained.

Current diagnoses are saved beside the originals and marked retrospective. They
are not assigned effects from already-completed historical interventions. The
original native ledger, original quality scores and original measured outcomes
remain authoritative for that study. Current labels mark serial agent-decision
batching as invalid, based on the archived workflow's dependency on preceding tool
outputs; routing scope remains ambiguous without a validated quality-preserving
replacement. Unknown and ambiguous labels are not correctness trials.

The [empirical calibration](EMPIRICAL_CALIBRATION.md) supplies original estimator
diagnostics where attribution exists. Its combined interventions stay
unattributed, quality-rejected observations stay rejected, and no fitted factor is
installed or promoted. Original UTF-8 application-receipt hashes and native
ASCII-escaped trace/ledger hashes are validated using their original conventions.
No archived hash or observation is rewritten to make validation pass.

All historical operating costs remain unknown. Zero paid-provider spend is a
separate fact, not a hardware/energy cost measurement. Model weights and runtime
binaries are not redistributed; their pinned public revisions/hashes remain in
the original protocols. Public source licenses and original reproduction notes
are included in the evidence bundles.

## Denominators, uncertainty and effort

Every named case/variant/repetition remains in the planned denominator. Missing
or incomplete reference receipts become missing observations; existing receipts
must match their trace, prediction, selection, quality and native intervention
artifacts. Reference quality is recomputed from the frozen scorer and outputs
during offline reconstruction. Historical outcomes must match their original
native ledgers.

Reports distinguish candidate quality-gate passes, numeric quality regressions,
missing quality, execution failures, and paired non-regression. Two wrong answers
can be non-regressing without meeting quality criteria. Unknown costs and absent
measurements remain unavailable instead of zero.

Paired resource changes are averaged within each independent task before the
existing task-cluster percentile bootstrap (1,000 samples, fixed seed, 95% interval).
Intervals describe variation across this small selected task set; fixed execution
order, correlated fixtures, shared hardware and public-task selection limit causal
or population claims. Finding labels describe the finite first-repetition case
inventory; their denominators are shown rather than treating repeated executions
as independent correctness labels.

Analysis time measures native diagnosis including saved-evidence validation.
Reference semantic inspection is timed separately in the raw results. Recording
controls disable span recording while retaining root lifecycle, input hashing and
fixture tokenization; batch controls also omit synthetic prompt capture. Their
delta is a partial instrumentation comparison, not total SDK overhead. Negative
overhead deltas remain visible. Historical paired wall differences include model
and warm-state variation; direct recording times are preserved separately.

The protocol records three orchestration steps: freeze, execute/import, and
summarize/inspect. Operator minutes and subjective review effort were not timed
and are explicitly unavailable. No global score combines these different evidence
kinds or hides workload/family failure modes.

## Reproduction

Use fresh destinations; the runner refuses to overwrite observations:

```bash
uv run --frozen python -m examples.usefulness_benchmark.run freeze \
  --source-revision 8af65da781029a581c6cf01cf8700cdea99da12c --out runs/usefulness-protocol.json
uv run --frozen python -m examples.usefulness_benchmark.run execute \
  runs/usefulness-protocol.json --out-dir runs/usefulness-evidence
uv run --frozen python -m examples.usefulness_benchmark.run report \
  runs/usefulness-evidence --json-out runs/recomputed.json --markdown-out runs/recomputed.md
```

The fixed source revision above identifies the analyzed core. A new source build
must use its own revision and protocol; frozen source hashes are checked before
execution. Execution is local/offline and resets runtime auto-export settings.
It verifies and unpacks the already-published source archives without executing
archived application code. Reporting consumes retained observations without
executing workloads, judges or model calls.

For a published run, unpack its checksummed multipart evidence archive and use the
retained implementation snapshot to reconstruct the JSON/Markdown outputs. Source
files are normalized to UTF-8/LF for portable hash checks. The archive includes
protocols, source snapshots/licenses, raw measurements, selected/rejected findings,
native quality/replay/intervention evidence, corpora and reports. Publication
verifies offline reconstruction before packaging.

The [published report](../research/cross-workload-2026-09/REPORT.md),
[machine-readable results](../research/cross-workload-2026-09/results.json),
[frozen protocol](../research/cross-workload-2026-09/protocol.json) and
[evidence index](../research/cross-workload-2026-09/index.json) retain the full run.
The isolated snapshot includes archive-loader support sources from the same
pinned Git revision, with separate hashes in `reproduction-support.json`. Adding
those packaging dependencies did not change a measured source file or observation.
