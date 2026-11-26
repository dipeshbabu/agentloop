# Finding trust benchmark

The finding benchmark evaluates canonical detector output against a
frozen, versioned corpus. It measures finding correctness separately from
[savings calibration](CALIBRATION.md) and [judgment backend quality](JUDGMENT_BENCHMARKS.md).
It does not execute judges, tune prompts or change detector thresholds.

From a source checkout:

```bash
uv run python scripts/benchmark_findings.py examples/finding_benchmark_corpus.json --split evaluation --source-revision YOUR_COMMIT --baseline examples/finding_benchmark_baseline.json --out runs/finding-benchmark
```

Use a fresh/empty output directory. The command writes `results.json`, `report.md`
and, when a baseline is supplied, `gate.json`. A failed gate exits with status 1
after retaining its evidence. Run development cases separately with
`--split development`, without a release baseline. Larger permission-cleared
corpora use the same command and format outside CI.

## Frozen fixtures and labels

`FindingBenchmark` in `agentloop.finding_benchmark_types` owns a JSON snapshot.
Its format records a corpus version, provenance reference, synthetic flag, frozen
policy identity/version/reference, and cases with:

- A unique case ID, workload reference, group, development/evaluation split and scenario.
- A native trace and the one rule whose findings are labeled in that case.
- `opportunity`, `no_opportunity`, `unknown` or `ambiguous` label status, with a
  label reference, version and rationale, including why a label is unavailable.
- For positive labels, the complete list of justified findings as exact family
  and affected-span sets. Extra findings, wrong spans and duplicates are errors.
- Required evidence dimensions: consistent timing, exact model-token provenance,
  recorded provider billing, and/or validated saved semantic investigations.

Labels are separate from the trace passed to detectors. The protocol rejects
duplicate case IDs, ambiguous span IDs, invalid target spans, groups crossing
splits and identical traces crossing splits. Group and provenance declarations
still require review: hashes do not establish independence, permission or truth.

Freeze labels, splits and policy before evaluation. Develop thresholds or prompts
only on development groups. Evaluate the held-out split once the policy is fixed;
if results inform tuning, retire that split as held-out and obtain a new one.
The checked-in corpus changes no detector policy and is a public regression
suite, not a permanently secret test set or a claim of generalization.

## Interpret the denominators

| Metric | Unit and denominator |
| --- | --- |
| Precision | Correct exact findings / all findings on known-label cases; unavailable when none are emitted |
| False-positive rate | Known-negative cases with any finding / all known-negative cases |
| Coverage | Cases emitting a finding / all planned cases |
| Abstention rate | Successfully analyzed cases emitting nothing / all planned cases |
| Evidence completeness | Cases meeting every declared evidence requirement / all planned cases |
| Missed opportunities | Independently labeled family/span sets without a matching finding |

Unknown and ambiguous cases stay in planned denominators and have separate
unscored-emission counts. They never become negative labels. An abstention is
absence of a recommendation, not proof that the workload has no opportunity.
Rule/analysis failures and missing result rows are separate from abstentions;
both prevent a release gate from passing. Unrelated rules' outputs are outside
the case's declared label scope.

Results report rule ID/version, trace hash, source revision, per-case evidence
and backend/config identities from saved semantic judgments. Rule confidence
is ordinal. Counts are stratified by rule and evidence basis; confidence words
are not pooled into a probability or calibration curve. A numeric calibration
diagnostic would require a defined probabilistic prediction and independent
labels, which these rules do not supply.

Timing measures native report construction, including all canonical rules and
saved-evidence validation, once per selected trace. It is reported overall and
for the trace groups labeled for each rule, not as isolated per-rule latency.
It excludes model inference: semantic receipts are frozen fixtures. Timing is
descriptive and is not a noisy microbenchmark release threshold.

## Small CI corpus

The [corpus](../examples/finding_benchmark_corpus.json) has 30 synthetic cases:
2 development cases and 28 evaluation cases across three deterministic rules
and all five semantic families. It includes valid opportunities, explicit
dependencies, shared-state traps, incomplete timing, a workload shift, different
retry causes, required verification, unavailable evidence and judge disagreement.
Of the 28 evaluation cases, 8 are labeled opportunities, 9 are known negatives,
6 are unknown and 5 are ambiguous.

Labels in [the fixture builder](../scripts/build_finding_corpus.py) were specified
without reading detector outputs. It uses local fake judges only to construct
saved evidence. The builder never overwrites an existing artifact; regeneration
changes receipt timing and corpus hashes and requires a new reviewed baseline.
The fixtures contain no private data or third-party dataset content.

## Results: archived baseline and current rules

Both rows use the same evaluation split of the same corpus snapshot
(corpus hash `0dd18272b0c34a794f6e1c149f097ca313c5e0b53bb7ca9f6d13e416c0da79da`).
Every rate is shown with its count and denominator.

- **Archived baseline.** This is [`finding_benchmark_baseline.json`](../examples/finding_benchmark_baseline.json),
  recorded at source revision `41e0cc58274b9a257892fae29b697110cc3283f9` under
  frozen policy "canonical finding rules" 1.0. It is historical evidence and is
  kept unchanged as the release gate's reference.
- **Current rules.** These are today's rules on the same cases, recomputed by CI
  on every run and by the command above. They are not stored as a separate artifact.
  Run the command with `--source-revision` set to the commit you are checking
  to get a dated copy.

<!-- finding-benchmark-overall:start -->
| Snapshot | Rule versions | Planned cases | Precision (correct / scored findings) | Known negatives with any finding | Cases with a finding | Unscored findings | Missed opportunities |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Archived baseline (`41e0cc5`) | 1.0, 1.1 | 28 | 8 / 10 (0.80) | 2 / 9 (0.22) | 11 / 28 (0.39) | 1 | 0 |
| Current rules | 1.0, 1.2 | 28 | 8 / 8 (1.00) | 0 / 9 (0.00) | 9 / 28 (0.32) | 1 | 0 |
<!-- finding-benchmark-overall:end -->

<!-- finding-benchmark-per-rule:start -->
| Rule | Cases | Archived version: correct / false | Current version: correct / false |
| --- | --- | --- | --- |
| `add_schema_validation` | 2 | 1.1: 1 / 1 | 1.2: 1 / 0 |
| `batch_model_calls` | 2 | 1.1: 1 / 1 | 1.2: 1 / 0 |
| `context_relevance` | 4 | 1.0: 1 / 0 | 1.0: 1 / 0 |
| `low_contribution` | 4 | 1.0: 1 / 0 | 1.0: 1 / 0 |
| `parallelize_tools` | 4 | 1.1: 1 / 0 | 1.2: 1 / 0 |
| `retry_usefulness` | 4 | 1.0: 1 / 0 | 1.0: 1 / 0 |
| `semantic_no_progress` | 4 | 1.0: 1 / 0 | 1.0: 1 / 0 |
| `semantic_redundancy` | 4 | 1.0: 1 / 0 | 1.0: 1 / 0 |
<!-- finding-benchmark-per-rule:end -->

`tests/test_finding_benchmark_docs.py` regenerates both tables from the baseline
file and a fresh evaluation, and fails when this page disagrees with them.

The archived baseline deliberately retains two known false positives:

- `add_schema_validation` 1.1 recommended schema repair for a transport timeout.
- `batch_model_calls` 1.1 recommended batching a same-role sequence with dependencies.

In that baseline, each of those two rules had one correct and one false finding
in its two-case evaluation subset, a precision of 1 / 2. That was a diagnostic of
these fixtures at that revision, not a population estimate, and it describes
no other rule.

The current rules exclude both cases. Batching 1.2 respects causal dependencies,
and schema repair 1.2 requires a declared structured-output failure. These
corrections were developed with these public labels in view, so the current row
measures regression against known cases, not held-out accuracy.

Limits that apply to both rows:

- The unknown and ambiguous cases are unscored and stay in the planned denominators.
- The missing-timing parallelization case emits one unscored finding in both snapshots.
- Supported semantic fixtures are artificial evidence-contract cases, not evidence
  that real judges are accurate.
- Rules absent from this small corpus have no claimed precision or release coverage.
- Independent application-owned labels are still needed to estimate field accuracy.

## Content fingerprint corpus

The [content fingerprint rules](CONTENT_FINGERPRINTS.md), `unchanged_output` and
`repeated_result`, have a separate corpus,
[`finding_benchmark_content_corpus.json`](../examples/finding_benchmark_content_corpus.json).
It has its own frozen policy, "content fingerprint rules" 1.0, so the main
corpus and its archived baseline above are unchanged. CI gates it the same way,
against [`finding_benchmark_content_baseline.json`](../examples/finding_benchmark_content_baseline.json).

It has 15 synthetic cases: 2 development cases and 13 evaluation cases, built by
`scripts/build_finding_corpus.py --corpus content`. Of the 13 evaluation cases,
4 are labeled opportunities, 5 are known negatives, 2 are unknown and 2 are
ambiguous. They cover:

- A trailing check that returned its draft unchanged, once and across four tasks.
- Checks that changed the draft, that changed most drafts, or that the caller
  marks as required.
- A rephrased retrieval that found nothing new, including one whose rephrased
  query another step also used.
- A second retrieval that found a new passage, and passages that repeat only
  across separate tasks.
- Traces with no fingerprints (unknown), a check that changed only letter case,
  and a retrieval that returned the same passages in a different order (ambiguous).

How it was made, in order:

1. The rule thresholds were fixed while writing the unit tests, before any
   corpus case existed.
2. The labels were written from each fixture's step list, without running the rules.
3. The development split was run once. Both cases matched their labels, and
   nothing was changed.
4. The evaluation split was run once, at source revision
   `3658d5ecd461c0bacad5f7fb5527dc91a5a5b09d`. That result is the baseline.

The same author wrote the rules and the labels, so this is a regression suite for
the rules' contract, not an estimate of their accuracy on real agents. No task
from the [optimization benchmark](OPTIMIZATION_BENCHMARK.md) was used, and its
held-out tasks were neither run nor read. Its saved traces have no fingerprints,
so these rules abstain on them.

<!-- content-finding-benchmark-overall:start -->
| Snapshot | Rule versions | Planned cases | Precision (correct / scored findings) | Known negatives with any finding | Cases with a finding | Unscored findings | Missed opportunities |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Recorded baseline (`3658d5e`) | 1.0 | 13 | 4 / 4 (1.00) | 0 / 5 (0.00) | 5 / 13 (0.38) | 1 | 0 |
| Current rules | 1.0 | 13 | 4 / 4 (1.00) | 0 / 5 (0.00) | 5 / 13 (0.38) | 1 | 0 |
<!-- content-finding-benchmark-overall:end -->

Corpus hash: `ab7f2530fa31085745ffe9aea710d13fde992934bae8e315d2d8227eb3ca050a`.

The unscored finding is the reordered retrieval. The rule treats it as
redundant, and the label leaves open whether the order mattered.

## Release gate and explicit review

CI runs the deterministic suite and compares against the
[versioned baseline](../examples/finding_benchmark_baseline.json). The Python API
`compare_finding_benchmarks(protocol, baseline, candidate)` recomputes counts
from retained rows rather than trusting summary scores. Both results must use
the same corpus hash, frozen policy and evaluation split, with all planned rules.
Rule versions may differ and are shown in regressions.

The strict default allows no increase in false findings, negative cases with
findings, missed labeled opportunities, unscored emissions, or findings whose
required evidence is incomplete. Turning off every detector cannot improve the
gate. Missing or failed analyses always fail, even with a review reference.

For a deliberate regression, a reviewer can run the CLI with
`--review-ref issue-or-review-reference`. That reference and the full regression
remain in `gate.json`; it is an explicit host declaration, not authentication or
a bypass of repository branch protection. Normal CI supplies no override.
Baseline/corpus changes are explicit review artifacts: explain changed labels,
known regressions and split provenance in the PR, retain the old evidence, and
review the baseline diff before accepting it. Do not regenerate a baseline merely
to clear a failing gate. Release preparation must review these results alongside
the ordinary quality and performance checks.
