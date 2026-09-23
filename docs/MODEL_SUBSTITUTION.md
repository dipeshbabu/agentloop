# Offline decision implementation experiments

AgentLoop can compare explicit replacements for one traced decision step on
frozen, permission-cleared or synthetic examples. Baseline agreement is separate
from independent labels or trusted quality checks. The workflow records outcomes,
latency, declared token usage and reported/calculated cost, then exports native
paired [study artifacts](STUDIES.md). It never activates a candidate, selects or
downloads a model, or changes production routing.

```console
python examples/model_substitution.py --out runs/model-substitution
```

This deterministic local example compares a baseline model **fixture**, a keyword
rule, a baseline-copy implementation, and a failing rule. The copy agrees with
the baseline while reproducing its mistakes; the independently checked rule does
better on the fixture labels. A timed-out output remains unavailable. Cost/token
values are illustrative fixtures and timings measure local Python callbacks,
not remote model inference. Outputs/grades are reproducible; measured timings,
timestamps and generated run IDs vary.

## Declare the experiment

The API is in `agentloop.substitution_types`, `agentloop.substitutions`,
`agentloop.substitution_reports` and `agentloop.substitution_export`:

```python
from agentloop import JudgeIdentity, JudgeUsage
from agentloop.substitution_types import (
    DecisionImplementation, DecisionResult, DecisionStep,
    SubstitutionExample, SubstitutionProtocol,
)
from agentloop.substitutions import run_substitution_experiment
from agentloop.substitution_reports import summarize_substitution_experiment
from agentloop.substitution_export import export_substitution_studies

step = DecisionStep.from_trace(
    source_trace, "decision-span", step_id="routing", version="1",
)
example = SubstitutionExample(
    "example-1", {"message": "approved evaluation input"},
    input_ref="dataset:v1:example-1", quality_ref="labels:v1:example-1",
    expected="support",
)
protocol = SubstitutionProtocol(
    "routing comparison", "1", step, [example],
    {"type": "decision", "version": "1.0", "labels": ["support", "other"]},
    repetitions=1, seed=0, timeout_s=2.0,
    min_quality=1.0, max_quality_drop=0.0,
)
```

`DecisionStep.from_trace` validates that the selected source span exists and
records its run/span identity and source trace fingerprint. That trace anchors
the step being investigated; its historical timing/output is not silently used
as a fresh baseline measurement. The supplied baseline callback executes again
on each frozen example alongside the candidates when execution is enabled.

Each implementation is a trusted synchronous callback:

```python
def decide(request, *, timeout_s):
    inputs = request.inputs  # Owned input, isolated from every other invocation.
    output = my_application_decision(inputs, timeout_s=timeout_s)
    return DecisionResult(
        output,
        usage=JudgeUsage(cost_usd=0.001, cost_basis="reported"),
        cost_ref="billing:my-record",
    )

candidate = DecisionImplementation(
    "candidate",
    JudgeIdentity.configured(
        "my-application.decision", "2", {"configuration_revision": "3"},
        provider="my-provider", model_or_rule="my-model-or-rule", revision="pinned-revision",
    ),
    decide,
    kind="model",  # Also classifier, rule, tool or external_service.
)
bundle = run_substitution_experiment(
    protocol, baseline_implementation, [candidate], enabled=True,
)
report = summarize_substitution_experiment(bundle)
artifacts = export_substitution_studies(bundle, "runs/my-experiment")
```

`DecisionIdentity` is an alias of the shared versioned `JudgeIdentity` record;
using it does not turn the implementation into a semantic quality grader.
Version, provider/model or rule identity, revision and configuration hash are
explicit declarations. The host must update them when behavior changes; they do
not detect mutable closures or freeze an upstream unpinned model alias.

Inputs, expected values and scorer configuration are copied into immutable JSON
snapshots. Each invocation receives its own decoded input. Reference answers,
quality references and scorer configuration are not passed to the implementation.
All outputs are collected before scoring, so grading does not provide feedback
between candidate invocations. Callbacks and scorers are trusted host code, not
a security sandbox; keep evaluation labels independent of implementation logic.

The scorer uses [structured quality contract 2.0](STRUCTURED_QUALITY.md) with an
explicit version. Built-in scorers use version `1.0`; local custom scorers require
their own version. An expected value may be omitted for a trusted check that does
not require a reference label. Missing reference data or failed scoring remains
unavailable according to that contract, not a positive quality result.

## Execution, outcomes and measurements

Execution defaults to disabled. `enabled=True` calls the explicit baseline and
candidate implementations in a seeded shuffled order. The default limit of
10,000 planned invocations applies before any callback and on artifact read;
`max_invocations` can be set explicitly. There is no framework result cache,
hidden warmup, retry or model download. Call count is not a financial spending
cap. The host owns side effects, approved API spend and cooperative cancellation.

`DecisionResult(output)` represents an observed finite JSON value, including an
actual JSON null. `DecisionResult(status="unknown")` represents no assessed
output. Returning an untyped object, raising an exception, or returning after the
cooperative time budget cannot become a successful output. Exceptions expose
public failure categories, not their messages. A late result keeps any known
usage but discards the answer. Fatal cancellation propagates. Nonreturning
synchronous callbacks are not forcibly killed; asynchronous callbacks are not
supported by this interface.

One generic classifier span records the measured decision invocation. Its logical
stage identifies the declared implementation kind/version. The runner clears and
restores the ambient trace context, so it does not append experimental work to a
live run or pretend to instrument provider internals. The callback supplies usage
through `JudgeUsage`: input/output counts can be independently unavailable, and
cost can be reported, calculated, or unknown. A free operation must explicitly
report zero when that is known. The cost reference can identify billing or the
cost model. Scoring and artifact generation are outside the callback timing and
decision-usage scope; their separate costs are not silently included.

Native profile totals remain scoped to recorded model spans. The experimental
`agentloop.substitution_trial` receipt records **declared decision-step** cost and
tokens separately, with identity, source/input/output hashes, timing and usage
basis. Native studies recognize that scope and use its frozen measurements,
without repricing them or replacing unknown usage with profile zeroes. The receipt
is source-bound and checked against workflow/stage state. Missing, altered or
unsupported receipts remain unavailable. A completed callback without saved
versioned quality does not count as task success.

## Quality and comparison

Saved quality assessments are bound to the exact plan, example/reference,
scorer configuration/version, output hashes and baseline/candidate run IDs.
Read/export operations never rerun implementations or scorers. Missing, duplicate,
reused, mismatched or corrupted trial records remain visible in planned slots;
unmatched records are listed separately.

Quality means give each example equal weight and include an example only when
all planned repetitions have known baseline and candidate quality. Full
`quality_preserved_on_examples` is unavailable if any planned example is
incomplete. Otherwise it requires the candidate's saved quality criteria to pass,
the minimum score, and the allowed mean quality drop. Acceptance uses exact
arithmetic on declared scores before display conversion. Baseline agreement uses
typed canonical JSON equality and is never treated as independent correctness.

Reports show observed measurement means with known/missing counts and usage bases.
Failures and unknown outputs are not removed from denominators. Results are
explicitly exploratory, including small samples and repeated/shared controls.
There is no automatic winner, leaderboard or claim that model size predicts
capability. Independent labels/checks and a passing fixture are not universal
correctness or deployment-safety evidence.

## Portable artifacts

Bundle schema `1.0` contains the frozen plan declarations, implementation
identities, trial traces/receipts, recorded order, quality assessments and a
limited Python/OS/architecture profile. It omits raw inputs, expected bodies and
candidate output bodies by default; their hashes and caller-supplied references
remain. References and declared identities are visible and never automatically
fetched. Keep the permission-cleared source dataset and implementation/scorer
versions available to reproduce an experiment. Hashes validate integrity and
binding, not authenticity or the truth of reported usage/labels.

Export writes `bundle.json`, `comparison.json`, `comparison.md`, and one native
study directory per candidate. Each directory contains baseline/candidate traces,
a `study.json` manifest and JSON/Markdown study reports. Conditions pair on plan,
example and repetition. Baseline observations are shared controls across these
separate studies, not additional independent executions. Quality evidence is
attached to copies appropriate to each pair. No bootstrap or promotion decision
is automatically enabled.

Study readers reject mixed ordinary model-only and declared decision-step cost
scopes, or different frozen experiment plans. Failed/unknown outputs with valid
receipts remain exportable. Ambiguous/missing receipts or absent saved quality
assessments are retained by comparison reports but cannot be fabricated into
study artifacts. Export uses generated filenames, checks output-directory bounds,
and refuses to replace different existing artifacts or reviewed study reports.
Exporting the same bundle again is idempotent.
