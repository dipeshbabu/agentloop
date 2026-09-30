# Context reduction and result reuse experiments

These **Unreleased source-checkout templates** create `ExperimentRunner` candidates
for the [generic experiment contract](OPTIMIZATION_EXPERIMENTS.md). Baselines and
candidates use the same frozen case inputs, expected answers and pairing keys.
Native traces, interventions, budgets, quality gates and JSON/HTML/Markdown exports
remain the result format. No production caching or automatic rollout is enabled.

## Explicit context changes

`context_reduction_runner` wraps an existing `ExperimentRunner`. Configure one to
64 `ContextSelection` objects, each naming an opaque selection ID, explicit JSON
path, source reference and `remove` or `compress` action:

```python
from agentloop.context_experiments import ContextSelection, context_reduction_runner

candidate = context_reduction_runner(
    "remove_optional", backend,
    selections=[ContextSelection("optional-notes", ("context", "notes"), "segment:notes:v1")],
    protected_paths=[("context", "required_evidence")],
)
```

Paths use dictionary keys or nonnegative list indexes. Selection paths cannot
overlap each other or protected paths. Array removals use original positions.
Inputs are owned copies; unrelated fields and protected context remain unchanged.
Missing selections fail by default. Explicit `on_missing="keep_original"` uses the
original request and records that no context change was applied.

Compression requires an explicit trusted synchronous `compressor(value, request)`
and versioned `compressor_ref`. It returns the existing `ExperimentResult`, including
its usage. Compressor and backend usage must be exclusive: duplicate usage IDs
are rejected, known tokens/costs are combined, and unknown components keep the
total unknown. Partial compression usage is retained if later work fails. Configure
a reservation covering all compression and backend work; the helper does not
assume the backend-only bound also covers a compressor.

Optional token counters return `ContextTokenCount` with an explicit `counter_ref`.
Provider/tokenizer/user-supplied exact counts remain distinct from `estimated_words`
and unavailable counts. Counter references and before/after bases must match for a
reduction comparison. A local tokenizer is appropriate; hidden work or fees inside
a trusted counter are not automatically intercepted. Include any such work in the
host's declared usage/budget accounting. Time checks occur before callbacks, and
the generic experiment still rejects late results cooperatively.

Trace metadata `agentloop.context_experiment` preserves selection IDs/references,
path/content hashes, action, replacement hashes, token-count provenance and usage.
It does not require raw removed or compressed context. Hashes of predictable
sensitive content are not anonymization; use permission-safe opaque references.

`context_applied` and `exact_token_reduction` are observations about the
transformation, **not quality conclusions**. Independent frozen quality scoring
can reject a shorter prompt. Reports keep estimated token counts labelled as
estimated and show quality-preserving subsets alongside the full denominator.

## Frozen result reuse

`result_reuse_runner` uses a bounded, read-only snapshot of prior `ReuseEntry`
results. It does not build a semantic cache, populate a cache on misses, or manage
a production cache service. This makes state reproducible across experiment resume.

```python
from agentloop.reuse_experiments import result_reuse_runner

candidate = result_reuse_runner(
    "reuse", backend, entries=frozen_entries,
    key_paths=[("task", "entity_id"), ("task", "locale")],
    key_version="1", data_ref="dataset:v3", invalidation_epoch="epoch-2",
    as_of="2026-09-15T00:00:00Z",
)
```

The lookup key is the exact canonical hash of the ordered selected values; absent
fields differ from explicit nulls. Entries must match key version, backend
implementation reference/version, configuration reference, data reference and
invalidation epoch. They also require a source reference and valid creation/expiry
metadata. Bumping any binding invalidates old entries.

`as_of` freezes the scenario's evaluation time. It is not a promise that an entry
will remain fresh in production. Entries created after that time, expired entries
(including the exact expiry boundary), inverted lifetimes and missing provenance
are ineligible. Among eligible matches, the newest creation time wins; conflicting
outputs at that time are ambiguous and cannot be used. Equal valid duplicate
outputs have a deterministic reference tie-break.

Default fallback recomputes through the declared backend. `on_unavailable="error"`
instead fails the candidate. Metadata `agentloop.reuse_experiment` retains key and
entry hashes/references, hit/miss/stale/invalidated/unknown/ambiguous states and
whether fallback actually ran. No raw key or cached output is needed in these
metadata records. The host retains the private prior-result snapshot and its
source artifacts.

A snapshot hit performs no new provider token work. Lookup operating cost remains
unknown, and prior cache-fill cost is explicitly excluded from the per-access
comparison. Do not treat an empty model profile as zero workflow cost or infer
amortized savings without accounting for fill/invalidation/storage work.

Key completeness is a host contract. A declared key can still be too coarse for
the task and return a wrong answer. Independent quality checks expose that case;
a mechanical hit alone is never counted as quality-preserving reuse.

## Shared observations and comparison reports

The generic experiment's bounded `agentloop.experiment_observations` namespace
stores scalar values with `observed`, `estimated` or `declared` kind, units and
source references. Entries cannot be rewritten during a trial. Summaries only
combine matching kinds, units and source references; mixed or unknown bases stay
explicit. Missing attempts remain in every planned denominator.

Boolean observations expose true/false/missing counts, fraction of all planned
attempts and an independently quality-preserving true count/fraction. Cache hit
rate describes key/provenance matching; quality-preserving hit rate additionally
requires the independent task gate. Successful recomputation after a stale/missing
entry is task success, not reuse success. Overall replay gates evaluate the complete
candidate, including fallback, and do not establish that caching caused a benefit.

Numeric observations show recorded values and conditional quality-preserving
summaries. Each candidate report keeps realized native pair deltas separate from
the original finding predictions. Recorded-model estimated costs remain separate
from exclusive caller-reported runner usage and unknown operating costs.

## Offline validation

```bash
uv run python -m examples.context_reuse_experiments --out runs/context-reuse
uv run python -m examples.context_reuse_experiments --out runs/context-reuse
```

The synthetic example compares optional-context removal, harmful required-context
removal, valid reuse and an incomplete reuse key. It retains quality regressions
even when token counts or callback latency fall. The second invocation executes
zero completed slots. The character tokenizer and timings are fixture evidence,
not real-model savings. No provider request is made.
