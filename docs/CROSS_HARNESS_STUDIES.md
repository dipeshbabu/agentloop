# Finished-trial cross-harness cohorts

This unreleased checkout compares completed Harbor source cohorts using exact
task/digest/scorer/verifier/environment/protocol/repetition identity. It reuses
the existing native study manifest/statistics and task-weighted uncertainty
helper. AgentLoop does not launch Harbor, schedule trials or run source verifiers.

```bash
agentloop harbor study sources.json --out runs/cohort
agentloop study summarize runs/cohort/study.json --json-out runs/native-study.json
uv run --frozen python examples/harbor_cross_harness_study.py --out runs/harbor-cross-harness
```

The example includes a correct and an incorrect candidate, both 50% faster in
owned synthetic source data, plus planned missing attempts. Correctness remains
an independent gate: the incorrect candidate cannot count as an improvement.
JSON/HTML clearly identifies synthetic data and unestablished causal attribution.

## Complete populations and native observation records

Every discovered attempt receives a native **receipt-only observation record**,
including failed, timed-out, cancelled, invalid or missing-trajectory cases.
These have no execution spans and `execution_data_present=false`. They do not
manufacture successful execution traces. Original import receipts remain
unchanged, including their empty trace lists and raw verifier outcomes.

The reserved `agentloop.external_cohort` version 1.0 records source receipt/
canonical snapshot reference, import/trajectory/usage coverage, explicit condition,
task/verifier identity and missingness policy. Existing captured trial evidence
binds it to the native observation. Native studies apply separate correctness,
execution and source-coverage acceptance; mutation and weak pairing fail.

All identified observed attempts enter the ordinary native manifest. Planned
but unidentified attempts remain count-only gaps; no seed, task, repetition or
fake execution ID is invented for them. The companion report includes these
gaps in population/metric denominators. A condition with no identified observed
attempts has no native manifest and a clear reason, while retaining its planned
population. Native statistics must not be generalized to unknown planned tasks.

Acceptance is false for recorded execution failure or configured quality failure.
With default `missingness_policy: indeterminate`, missing/corrupt trajectory,
verifier or required usage stays indeterminate. The explicit `reject` policy
counts those cases as rejected. Raw external quality remains separate: a passing
verifier with no trajectory is preserved as a recorded pass, while its run
acceptance remains indeterminate/rejected and cannot win.

## Exact pairing and independent quality

Pair keys contain task ID, content digest, exact hashed scorer and verifier
configuration/isolation, environment config hash, protocol ID and typed repetition.
Different digests or task IDs never pair by display name. Missing keys, ambiguous
repetitions, incompatible configs and absent counterparts remain explicit exclusions.
Trial IDs identify attempts; backend UUIDs do not become seeded replications.

Use [explicit scoring contracts](HARBOR_TRIALS.md) for required reward dimensions.
Per-task digest rules are supported by `HarborOptions.scoring_by_task_digest`.
Fractional/negative/null dimensions remain source data; positive reward alone
never establishes correctness. Failed/missing/invalid verifiers do not win.

A paired runtime improvement requires both acceptable completed observations,
independent configured correctness passes, available comparable agent runtime,
and strict candidate improvement meeting the configured minimum percentage.
Failed, unpaired, unknown or merely equal-speed candidates cannot win.

`quality_preserving_intervention_rate` records exact known-win numerator,
candidate population denominator and selection policy. It includes observed
candidate attempts plus planned unidentified candidate attempts; unpaired or
unknown evidence cannot inflate wins. This conservative observed fraction is
not an exact causal rate, and unknown baseline allocation remains a limitation.

## Scope, phases and provenance

Reports separate job orchestration, environment setup, agent setup, agent runtime,
trial wall time, verifier runtime and source model/tool observation counts.
Agent-only runtime is never substituted for total trial wall time. A selected
single-trial root is not labeled measured job orchestration. p50/p90/p95 use the
native summary helper. Missing counts/retries/timing remain unavailable.

Harbor agent-context input/output/cache usage remains externally reported;
cache is a prompt subset. Parent/root aggregate usage is not added to child
counts. Model multiplicity and tool counts derive from supported distinct
trajectory projections; failed/missing coverage remains unknown. Reported USD,
local estimated USD and unknown self-hosted operating cost stay distinct.
Provider billing and AgentLoop instrumentation overhead are not fabricated.

The report preserves source dataset/version, agent/model, environment backend,
config hashes and source versions where recorded. Controlled-ablation vs
system-level descriptive design is explicit. Model/harness/config differences,
unrecorded labels, caches, concurrency, provider queueing and held-out allocation
remain confounders. Equal labels do not prove causal control. A false declaration
of equal model control cannot establish a harness effect.

Task uncertainty averages repeated observations within each independent task,
then bootstraps task means. Repeated seeds or events on one actual task do not
claim multiple independent tasks. Planned gaps do not acquire fabricated task
clusters. The native manifest omits repetition-level bootstrap to prevent that
result from being presented as task-level uncertainty.

If original `InterventionRecord` snapshots are supplied to the Python API,
predicted findings and measured outcomes are retained byte-for-byte semantically.
Arbitrary harness differences do not acquire invented estimator attribution.

## Strict source manifest 1.0

Source manifests are data-only local references to completed Harbor jobs. The
first supported cohort route is Harbor-derived cross-harness evidence; Omnigent
session files are separately imported with [their qualified adapter](OMNIGENT_OTEL.md).
No genuine live Omnigent/Harbor run is claimed by the synthetic example.

```json
{
  "schema_version": "1.0",
  "name": "Completed source comparison",
  "baseline": "baseline",
  "protocol": {"protocol_id": "my-study-v1", "comparison_kind": "system_level_confounded"},
  "sources": {
    "baseline": {
      "system": "harbor", "job_path": "inputs/baseline",
      "scoring": {"schema_version": "1.0", "scorer_id": "task-v1", "rule": "all_gte", "thresholds": {"correctness": 1}},
      "task_scoring": {}, "trial_context": {"trial-a": {"repetition": 1}},
      "expected_hashes": {}, "expected_agent": "recorded-agent", "expected_model": "recorded-model"
    },
    "candidate": {
      "system": "harbor", "job_path": "inputs/candidate",
      "scoring": {"schema_version": "1.0", "scorer_id": "task-v1", "rule": "all_gte", "thresholds": {"correctness": 1}},
      "task_scoring": {}, "trial_context": {"trial-b": {"repetition": 1}},
      "expected_hashes": {}, "expected_agent": "recorded-agent", "expected_model": "recorded-model"
    }
  }
}
```

Unknown top-level/protocol/source fields fail. Remote/absolute/traversal/symlink
job paths, executable commands/prompts/callbacks and false model/agent claims
are rejected. Expected source hashes use the existing import boundary.
`expected_agent`/`expected_model` can be null when unrecorded; that does not prove
control. Source condition names and protocol IDs are explicit. Optional
`synthetic: true` identifies owned fixture sources.

## Python and verification

```python
from agentloop.interoperability.cohorts import CohortProtocol, write_cohort_study

# baseline/candidate are already captured HarborImportResult objects.
result = write_cohort_study({"baseline": baseline, "candidate": candidate},
    "runs/cohort", baseline="baseline", protocol=CohortProtocol("my-study-v1"))
print(result.report()["comparisons"]["candidate"])
```

```bash
uv run --frozen python -m pytest tests/test_external_cohorts.py tests/test_harbor_trials.py tests/test_studies.py tests/test_replay.py -q -ra
```

The mapping was refreshed against Harbor
`d5ac1be17f575852eaf4fffc4072fd18481c209b`. Core Python 3.10, native trace 1.1,
study manifest 1.0, dependencies and package version remain unchanged. No upstream
implementation code copied, paid provider invoked, sandbox cloned or release made.
Use a source checkout; published 0.7.0 does not contain these unreleased commands.
