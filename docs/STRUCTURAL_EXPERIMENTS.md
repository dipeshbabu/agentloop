# Structural optimization experiments

These **Unreleased source-checkout templates** create candidates on the
[generic experiment contract](OPTIMIZATION_EXPERIMENTS.md). They evaluate explicit
stage removal, conditional execution, batching and parallelization. Every
candidate declares changed stage IDs/versions, source references, implementation
references and why the change is proposed. Findings supply hypotheses; trace shape
or repeated operation names never authorize an optimization.

All results use the same frozen cases, independent quality gates, native traces,
intervention records, paired studies and JSON/HTML/Markdown exports. Measurements
cover the candidate's complete protected invocation, including orchestration and
assembly overhead. Summed span durations are not substituted for end-to-end gains.
Passing a fixture does not enable production changes.

## Stage removal

`stage_removal_runner` takes a trusted stage factory, assembler, `StageReference`
declarations and a mapping of removed stage IDs to explicit replacement values.
The factory returns existing `ToolCall` objects. Each actual callback returns
`ExperimentResult`; downstream `ToolContext.prerequisites` contain owned outputs.
The assembler receives the ordered stage-output mapping and returns the final
`ExperimentResult` with any exclusive assembly usage.

The original dependency plan is validated first. The template replaces selected
operations with constant-result shims, so the original functions never run and no
actual-stage span is invented for the omitted work. The logical dependency node
remains to deliver the declared replacement to downstream code. Prerequisites
are not silently removed. Replacement values are owned; receipts store hashes.

This deliberately allows harmful candidates: a missing required computation or
side effect can change downstream output or cause failure. Both remain in the
experiment evidence and fail the independent task gate. The host must isolate
experiments with mutating callbacks and declare suitable permission/reset rules.

## Conditional execution

`conditional_experiment_runner` selects between explicit full/expensive and
fallback implementations returning the same task-level output contract. These
implementations remain host-owned; the declared changed stages describe the
branch difference. A versioned predicate returns `True`, `False`, or `None`.
Model-assisted predicates return an `ExperimentResult` containing that value and
exclusive usage so predicate overhead is included in the candidate total.

Unknown/error/invalid predicate results use the explicit policy `expensive`
(default), `fallback`, or `error`. They never become an implicit false decision.
Cancellation propagates. Plain boolean predicates are local host computations;
hidden provider work needs explicit usage reporting.

Reports retain predicate-known coverage, predicate values, selected routes and
unknown reasons. Route observations include quality pass counts and fractions
for each cohort. A routing miss can therefore show a fast fallback with failed
task quality, independently of successful full-route cases. Routing metadata
describes selection; overall attempt state records later timeout/failure.

## Batching

`batching_experiment_runner` selects a list at an explicit input path and invokes
a trusted `BatchRequest -> BatchResult` callback. Item IDs are stable original
positions. Results must cover every requested item exactly once; returned order
may differ and is restored before final output. `BatchItemOutcome` distinguishes
completed output (including explicit null), failed items and unknown outcomes.

`BatchConstraints` declares batch size, provider maximum items/payload bytes,
maximum total items/batches and a provider constraint reference. Optional input
token bounds require exact `ContextTokenCount` results from a declared counter;
word estimates or unknown counts cannot satisfy them. The byte bound covers the
canonical item envelope. The host must account for additional provider prompts,
headers, model-specific limits and hidden work in its counter/configuration.

Each batch retains item identities, input/output hashes, statuses, known usage and
latency. A whole-batch exception leaves affected item outcomes unknown, not
successful. Completed/failed/unknown/not-started counts remain separate. There are
no automatic retries. Default `fail_task` stops after a partial item failure and
retains later unstarted items. Explicit `return_partial` preserves failed positions
as nulls and continues; the independent whole-task scorer decides acceptability.

Reports include observed batch latency distributions and completed-item throughput
derived from the native **end-to-end candidate runtime**, with quality-preserving
subsets separate. Declared batch size is not throughput. Reservations must cover
the whole candidate, and unknown or partially reported usage is not free work.

## Parallelization

`parallel_experiment_runner` reuses the existing [declared tool scheduler](TOOL_SCHEDULING.md).
It requires explicit dependencies, resource reads/writes, concurrency capability
and side-effect declarations for every stage. Unknown evidence, missing dependencies
or cycles abstain before stage dispatch. Known shared-write/read conflicts retain
serial dependencies. A stage that explicitly disallows concurrency stays serial.
No independence is inferred from names, historical success or structural similarity.

The scheduler's native plan, effective dependencies, resource conflicts, ordering,
partial outcomes and observed peak concurrency stay attached to the experiment
trace. Actual stage spans have declared versions/source references. Scheduled
logical shims are distinguished from invoked operations. End-to-end comparisons
include thread/scheduling overhead; a concurrency declaration alone is not a
measured parallel speedup.

Deadlines stop new admission cooperatively and drain already-started callbacks.
Already-completed mutations/results are retained and never automatically repeated.
This is an offline experiment adapter over an existing scheduler, not a replacement
workflow engine or a production concurrency service.

## Caller responsibilities and evidence

Stage factories should only bind callbacks, not perform hidden workload work.
Stage, predicate, batch and assembler usage must be exclusive; duplicate usage IDs
are rejected. Local/no-provider components can explicitly report zero token work,
while unknown operating cost remains unknown. Actual provider tracing inside a
stage is separate from its structural wrapper and is not fabricated by the adapter.

Metadata `agentloop.structural_experiment` keeps the change rationale, declarations,
dependency evidence and per-stage/batch outcomes without requiring raw outputs.
Generic scalar observations preserve planned denominators, route cohorts and
end-to-end rates. Original predictions remain separate from realized native replay
deltas. Independent scorers, permission-safe references and truthful implementation
versioning remain host responsibilities.

```bash
uv run python -m examples.structural_experiments --out runs/structural-experiments
uv run python -m examples.structural_experiments --out runs/structural-experiments
```

The synthetic example retains required-stage removal failure, conditional misses,
safe parallel execution, successful batching and partial batch failure. Repeating
the command reuses completed slots. Tests also cover unsafe dependency traps,
resource conflicts, missing/duplicate batch results, provider constraints, unknown
outcomes and cancellation. No provider calls or production modifications occur.
