# Harbor job and trial evidence

This unreleased source checkout imports completed Harbor jobs or individual
trial directories without Harbor, Docker, credentials or a network connection.
Import reads bounded JSON; it never runs a task, verifier, hook or model.

```bash
agentloop harbor inspect jobs/completed --json-out runs/harbor-inventory.json
agentloop harbor import jobs/completed --out runs/harbor-import
uv run --frozen python examples/harbor_trials.py --out runs/harbor-trials
```

The example uses owned synthetic fixtures. It exports eight trial receipts,
five ATIF trajectory projections and four trial measurement projections, plus
a native study and its complete source population inventory. Its quality failures
are deliberate; it establishes no empirical optimization result.

## Source files and inventory

Exports also include `harbor-trials.jsonl`: one complete summary record followed
by one record per discovered trial, including failures, missing trajectories and
invalid rows. Records are written and compared incrementally, with conflicting
bytes rejected. The existing `harbor-inventory.json` remains unchanged. Planned
unidentified attempts stay in summary counts. See the
[offline adoption guide](INTEROPERABILITY_READINESS.md) for framing and limits;
correlated import results retain bounded native objects, rather than claiming
constant-memory whole-job import.

The adapter reads `result.json`, `config.json`, `lock.json` and
`agent/trajectory.json` at a trial root, plus `verifier/reward.json` when supplied.
Only immediate directories with recognized trial metadata are discovered. A
validated `step_results` list selects `steps/<step_name>/` paths; directory names
alone do not establish a multi-step trial. Referenced task paths are metadata,
never executed or opened. Archives are unsupported.

The [ATIF adapter](HARBOR_ATIF.md) retains trajectory semantics. Job/trial identity
qualifies native IDs, so repeated trial IDs in different identified jobs do not
collide. Duplicate trial IDs within one job fail. Missing job/trial identities
use explicitly calculated identities; copying identical unidentified artifacts
does not establish a distinct experiment. Supply `HarborOptions(job_id=...)`
when separate source jobs have no recorded job UUID. Seed/attempt/repetition
identity is never manufactured.

Job `n_total_trials` is retained separately from observed trials. Embedded job
results can retain trials without local files; unidentified planned attempts
remain a count with no invented identities. Inventory reports imported trials,
absent trajectories, invalid records, quality outcomes, source versions, source
file hashes, ignored directories and planned gaps. Invalid independent records
are retained by default. `continue_on_error=False` raises on invalid data;
overall limits and impossible duplicate identities always fail.

The canonical filename is `result.json`. The Python-only option
`allow_legacy_results_name=True` enables `results.json` only when `result.json`
is absent. Contradictory embedded/local results or reward sources invalidate
the trial's comparison evidence. A malformed canonical result never falls back
to an alternate file.

## Independent external quality

Rewards remain source dimensions, including fractional, negative and null values.
Without explicit acceptance criteria every `quality_pass` is null, including a
positive reward. Create a versioned scoring file to interpret exact required
dimensions:

```json
{
  "schema_version": "1.0",
  "scorer_id": "my-task-verifier-v1",
  "rule": "all_gte",
  "thresholds": {"correctness": 1.0, "coverage": 1.0}
}
```

```bash
agentloop harbor import jobs/completed --out runs/scored --scoring scoring.json
```

The importer calculates and retains `config_sha256` using the
[receipt contract](INTEROPERABILITY.md). If supplied, the hash must match.
Boolean/nonfinite thresholds, unknown rules, missing dimensions, corrupt rewards,
failed verifiers or conflicting source hashes cannot establish a pass. Rewards
`{correctness: 0.5, coverage: 1.0}` fail the example criteria.

Execution status and quality are independent. A stale positive reward does not
make a timed-out, failed or cancelled execution successful. Step rewards and
exceptions remain separate; the importer does not average step rewards or infer
a trial acceptance rule. A contradictory passing trial reward and failed step
criterion invalidates comparison evidence. Regrade source-trial identity/action
is retained without inventing a fresh execution.

Single-step isolation uses recorded `verifier_environment_mode` or the resolved
trial lock. Current Harbor step results lack resolved isolation, so a requested
step config remains `configured_verifier_mode` and observed isolation remains
unknown unless explicitly recorded. Shared/separate settings do not prove
authenticity or freedom from agent influence.

## Measurement and report qualification

ATIF projections describe source operations. A separate trial measurement trace
describes only the externally recorded `agent_execution` phase and agent-context
usage. Setup and verifier phase timing stay in the receipt. Job wall time is never
substituted for agent runtime. Missing or timezone-free intervals remain unknown.
Missing usage/cost stays null; root and step usage are never added together, and
cache counts remain input subsets. Incomplete step accounting stays partial.

The reserved native metadata namespace `agentloop.harbor_trial` version `1.0`
binds the measurement, outcome and pairing fields to a captured trace snapshot.
Reports/studies read this qualification instead of treating native compatibility
placeholders as measured zeros. Model/tool/retry counts remain unknown in a phase
projection. Source-reported cost is distinguished from provider measurement;
no price table is applied. Value estimates require operation-level evidence.

A trial without a valid trajectory retains a receipt with `traces: []` and no
manufactured successful trace. Invalid trials remain in the inventory. Receipt
and trace hashes establish supplied-byte reproducibility, not authenticity.
Output is stable on repeated imports; conflicting output bytes are rejected.
Exception messages/tracebacks are omitted with bounded size markers; original
safe exception class names are retained. Paths outside the selected source are
hashed/omitted. Structural identifiers can still be private.

## Native study export and pairing

`write_harbor_study` requires at least two explicit conditions and a named
baseline. It writes native study manifest 1.0 without adding manifest fields:

```python
from agentloop.integrations.harbor.manifest import write_harbor_study
from agentloop.integrations.harbor.trials import HarborOptions, import_harbor
from agentloop.integrations.harbor.verifier import ScoringContract

scoring = ScoringContract.all_gte("task-v1", {"correctness": 1.0})
baseline = import_harbor("jobs/base", options=HarborOptions(
    scoring=scoring, condition="baseline", protocol_id="protocol-v1",
    trial_context={"trial-a": {"repetition": 1}},
))
candidate = import_harbor("jobs/candidate", options=HarborOptions(
    scoring=scoring, condition="candidate", protocol_id="protocol-v1",
    trial_context={"trial-b": {"repetition": 1}},
))
path = write_harbor_study({"baseline": baseline, "candidate": candidate},
                        "runs/study", baseline="baseline")
```

```bash
agentloop study summarize runs/study/study.json --json-out runs/study/report.json
```

Pair keys include exact task digest, scorer config hash, environment config hash,
protocol ID and typed repetition. Missing/incompatible/ambiguous keys remain
unpaired. Direct replay refuses incompatible pairs; its external correctness gate
requires both completed executions and passing external criteria even when the
candidate is faster. Default retry gates remain indeterminate when source retries
are absent.

For CLI imports, `--condition`, `--protocol-id` and `--context` accept explicit
condition/protocol/repetition declarations. Context keys are relative immediate
trial directory names; the empty key addresses an individually selected trial.
These declarations do not establish that a controlled experiment was executed.

Native study statistics describe available trial measurement projections.
`cohort-inventory.json` retains every excluded source row and each complete job
inventory. Missing trajectories are excluded from native measurements and cannot
be called successful native runs. Do not generalize these statistics to the full
planned population or claim a causal optimization from them. Complete cohort
correctness/confounder gates are a dependent workstream.

## Compatibility and validation

Trial/job field projections follow Harbor
[`d5ac1be17f575852eaf4fffc4072fd18481c209b`](https://github.com/harbor-framework/harbor/tree/d5ac1be17f575852eaf4fffc4072fd18481c209b/src/harbor/models).
Trial locks support schemas 2/3; unsupported versions fail. This inspected
revision is separate from the producer revision recorded in job locks. The
[fixture matrix](../tests/fixtures/external/source_matrix.json) pins synthetic
source bytes and their original model reference. No upstream code is copied.
AgentLoop remains Python 3.10+, native schema 1.1 and package version 0.7.0.
Use this checkout's qualification-aware reader; the published wheel lacks these
commands and older readers can misinterpret native placeholders.

```bash
uv run --frozen python -m pytest tests/test_harbor_trials.py tests/test_harbor_atif.py tests/test_interoperability_contracts.py tests/test_studies.py tests/test_replay.py -q -ra
```

Offline tests exercise mixed outcomes, multi-step missingness, quality failures,
conflicts, hashes, namespace stability, parser budgets, partial imports, CLI/HTML,
native studies and output mutation. No live provider/Harbor job is claimed.
