# Finding trust benchmark

The unreleased finding benchmark evaluates canonical detector output against a
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

## Small CI corpus and honest baseline

The [corpus](../examples/finding_benchmark_corpus.json) has 30 synthetic cases:
two development cases and 28 evaluation cases across three deterministic rules
and all five semantic families. It includes valid opportunities, explicit
dependencies, shared-state traps, incomplete timing, a workload shift, different
retry causes, required verification, unavailable evidence and judge disagreement.

Labels in [the fixture builder](../scripts/build_finding_corpus.py) were specified
without reading detector outputs. It uses local fake judges only to construct
saved evidence. The builder never overwrites an existing artifact; regeneration
changes receipt timing and corpus hashes and requires a new reviewed baseline.
The fixtures contain no private data or third-party dataset content.

The initial baseline deliberately retains two known false positives:

- `add_schema_validation` recommends schema repair for a transport timeout.
- `batch_model_calls` recommends batching a same-role sequence with dependencies.

Each rule has one correct and one false finding in its two-case evaluation
subset. That 0.5 precision is a diagnostic of these fixtures, not a population
estimate. The missing-timing parallelization case is unscored and remains
visible. Supported semantic fixtures are artificial evidence-contract cases,
not evidence that real judges are accurate. Rules absent from this small corpus
have no claimed precision or release coverage.

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
