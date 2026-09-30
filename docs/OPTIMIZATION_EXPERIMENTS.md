# Finding-linked optimization experiments

Available from an **Unreleased source checkout**. Schema 1.0 links frozen baseline
traces/findings to explicit caller-run candidates, then reuses native quality,
replay, intervention and study artifacts. It does not deploy candidates or execute
code named by a JSON reference.

## Scope and declarations

The first supported path **links existing baseline runs**. Create those with the
normal tracing APIs or load them from the existing store, then freeze an
`ExperimentCase` for each actual baseline. Each case includes:

- Unique case ID, example ID and repetition, plus the original completed baseline
  trace and its implementation/configuration references.
- Explicit finding IDs that belong to that baseline, with owned full diagnosis
  and selected expected-effect snapshots captured before any candidate execution.
- Owned finite JSON inputs, independently configured expected output and observed
  baseline output, each with references and hashes. A recorded workflow output
  cannot disagree with the supplied baseline output.

Baseline run IDs and `(example_id, repetition)` keys must be unique within a plan.
One baseline can be a shared control across separate candidate comparisons; it is
not an independent new execution for each comparison. Historical controls are not
randomized contemporaneous baselines, and this contract makes no causal guarantee.

An `ExperimentRunner` binds a candidate ID, version, implementation/configuration
references, a trusted synchronous callable and optional conservative resource
reservation. References are metadata; no import, shell command or remote code
loader resolves them. The host must change references when its actual code/config
changes. Hashes detect artifact inconsistency, not dishonest declarations.

`ExperimentPlan` schema 1.0 freezes the following fields before dispatch:

| Field | Meaning |
| --- | --- |
| `name`, `frozen_at`, `permission_ref`, `synthetic` | Experiment identity context, permission reference and preserved source markers. |
| `intervention` | Explicit type and version of the proposed change. |
| `cases` | Baseline hashes, finding IDs/snapshots, implementation/config references, input/expected/output hashes, example and repetition identities. |
| `runners` | Candidate implementation/config references, versions and reservations. |
| `scorer`, `scorer_hash` | Versioned deterministic structured quality configuration. Custom executable import strings are rejected. |
| `gate_version`, `gates` | Explicit native `ReplayGates`; independent `min_quality_score` is required. |
| `budget`, `budget_scope` | New candidate invocation, timeout, token and reported-cost admission limits. |
| `pairing_keys`, `artifact_layout` | Stable case pairing and relative locations for native traces, predictions, attempt records and reports. |

The plan owns its JSON. Baselines, full diagnoses and selected prediction snapshots
are saved before `run(enabled=True)` admits any candidate. A frozen timestamp/hash
is not independent proof of preregistration; retain the source artifacts and review
history. Changes to bindings, predictions or source traces require a new plan.

## Running candidates

```python
from agentloop.experiments import ExperimentSession
from agentloop.experiment_reports import export_experiment, persist_experiment

session = ExperimentSession(plan, cases=cases, runners=runners, root="runs/experiment")
session.run()                 # Disabled: no candidate callbacks.
session.run(enabled=True)     # Explicit local execution.
evidence = export_experiment(session.root)
# Optional: use an existing SQLiteTraceStore/PostgresTraceStore instance.
# persist_experiment(session.root, store, project_id="reviewed-project")
```

The callback receives `ExperimentRequest`: owned inputs, identifiers, input
reference and `remaining_s`. Expected labels and baseline outputs are not passed
to it. Return `ExperimentResult(output, usage=ResourceUsage(...))`; normal tracing
inside the callback records children under the experiment's workflow span.
Callbacks cannot append to an unrelated ambient trace through the current trace
binding. Trusted code can still perform external side effects; this is not a
sandbox or permission system.

A runner invocation is metered through the existing harness's tool boundary. Its
reported usage must be exclusive for that callback; hidden SDK calls and provider
retries are not individually intercepted. Invocation limits count admitted
candidate attempts, not model API calls. A deadline guard can deny the callback
after admission; each receipt's `invoked` flag preserves that distinction.
Native replay/study cost remains the **recorded
model profile estimate**; exclusive caller-reported runner usage is a separate
receipt/summary. Unknown runner usage stays unknown, including when model-only
profiles are empty. Do not interpret an empty model profile as zero workflow cost.

Token/cost caps require declared upper-bound reservations. Reservations accumulate
across resumed invocations and are **not refunded**, even when actual usage is
smaller. Actual known/unknown usage remains separately visible. Native budget
stops/overshoots halt further dispatch. Historical baseline work lies outside the
new-candidate admission budget and remains in paired evidence. These controls are
cooperative admission limits, not a provider billing cap.

Pass the request's remaining time to callback I/O/subprocess timeouts. A late
return is retained as `timed_out`; Python code is not forcibly killed. Failures,
invalid returns and cancellations retain their attempt records. Cancellation is
re-raised after capture, and a later capture error does not replace the original
interruption. There are no automatic retries.

## Identity, resume and uncertainty

`experiment_id` hashes the complete canonical specification. Each slot hashes
`(experiment_id, case_id, candidate_id)` and has a deterministic candidate run ID.
One owned journal root is authoritative for that identity. Reusing the same plan
in a different empty root is a separate host action, not a supported resume or an
exactly-once guarantee; native persistence rejects conflicting existing run data.

An exclusive `journal.lock` serializes writers. Admission writes `started.json`
before callback execution, then publishes native artifacts and a final receipt
with atomic local-file replacement. Filenames use hashes, including case IDs
with filesystem-special characters. Paths must remain inside the chosen root.
The host must provide a filesystem with suitable exclusive-create/rename semantics.

```python
resumed = ExperimentSession.resume("runs/experiment", cases=cases, runners=runners)
resumed.run(enabled=True)  # Existing final receipts are reused, never re-executed.
```

Completed, failed, timed-out, stopped and cancelled slots are terminal. A start
record without a final receipt is `unresolved` and blocks further execution.
After a hard process failure, the host must confirm no worker/remote action is
still active and reconcile the stale lock, external outcomes and spend. Never
remove a lock merely because it is old or blindly repeat an uncertain side effect.
The journal is not a durable workflow engine and cannot recover external state.

Budget exhaustion leaves remaining slots `not_started`; those slots remain in
the planned denominator. Unknown/partial writes or changed artifacts are reported
as unresolved/corrupt evidence, rather than silently omitted or reconstructed.

## Reporting and existing stores

`summarize_experiment(root)` reads saved outcomes without a runner or scorer.
`export_experiment(root)` writes JSON, readable HTML and Markdown under a
content-addressed report directory. Re-exporting the same snapshot is idempotent.
Every planned slot appears, including missing candidates and interrupted attempts.

Each observed pair has a native `InterventionRecord` bound to the actual original
baseline, original finding snapshots, candidate trace, scorer and gate versions.
Available candidates produce native study manifests and summaries. A condition
with no candidate traces has an explicit unavailable-study reason; the exporter
does not fabricate executions to satisfy the study format.

Study views add pairing/quality annotations to copies and record their source
trace hashes. Original traces and predictions remain unchanged. Shared baselines,
repeated examples, historical controls, missingness and measurement coverage must
be considered before drawing conclusions. Passing the configured gates is not
automatic deployment approval or proof of transfer to another workload.

`persist_experiment` uses the existing trace/finding/intervention store APIs, with
no new database tables. It checks existing run identities before writing and
preserves existing findings/lifecycle decisions when linking an already-stored
baseline. The host must serialize conflicting store writers and retain referenced
historical findings. Intervention conflict checks remain native.

Artifacts can contain supplied baseline trace content and caller-recorded spans.
The coordinator saves callback output hashes and quality assessments; it does not
save an additional raw-output body. Keep any required output source in a
permission-controlled host store for independent reproduction. Review artifact
contents before sharing; a permission reference or checksum is not access control.

## Offline example

```bash
uv run python -m examples.optimization_experiment --out runs/optimization-experiment
uv run python -m examples.optimization_experiment --out runs/optimization-experiment
```

The first run evaluates a synthetic correct candidate and a quality-regressing
candidate against a linked synthetic baseline. The second executes zero callbacks
and reuses the same report snapshot. No provider request or production change is
performed. See [finding ranking](FINDING_RANKING.md),
[model substitution](MODEL_SUBSTITUTION.md) and [reviewed promotion](POLICY_PROMOTION.md)
for the separate investigation, decision-step evaluation and rollout contracts.
