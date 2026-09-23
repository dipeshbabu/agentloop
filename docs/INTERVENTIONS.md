# Intervention evidence

Interventions can retain [versioned structured-output quality](STRUCTURED_QUALITY.md)
for generic workflows without changing their original finding snapshots.

An intervention records which findings motivated a change and what happened in
a baseline/candidate comparison. It is a versioned evidence artifact, independent
of the native trace schema. It does not modify application code or tune rules.

## Schema 1.0

| Field | Meaning |
| --- | --- |
| `schema_version` | Intervention contract version, `1.0` |
| `intervention_id` | Deterministic `int_` identity defined below |
| `baseline_run_id`, `candidate_run_id` | Distinct runs being compared |
| `target_finding_ids` | Sorted, unique, nonempty IDs from the baseline's diagnosis |
| `intervention_type` | Nonempty application label, such as `context_compression` |
| `configuration` | JSON object describing the applied change |
| `predicted.findings` | Original finding snapshots, including savings and estimator provenance |
| `measured` | Full replay report, with observed run summaries, deltas, quality, and gates |
| `gates_passed` | The replay gate decision, also present in `measured.gates.passed` |
| `trace_fingerprints` | SHA-256 hashes of the source native trace JSON |
| `metadata` | Task, seed, experiment, source revision, or other application metadata |

Prediction snapshots are captured before comparison from a supplied diagnosis or
the persisted baseline findings. They are never reconstructed from measured
deltas. Per-finding predictions are alternatives when affected spans overlap;
do not sum them. Export the original optimization plan's `savings_aggregation`
alongside the record if you need its compatible-selection result.

The complete replay report preserves failed quality checks and indeterminate
cost comparisons. A gate pass with no configured quality check does not establish
quality equivalence. `null` cost deltas remain unavailable and are not replaced
with zero. The artifact retains the current estimator's uncalibrated label.

## Identity and immutability

Identity is SHA-256 over canonical JSON containing schema version, baseline and
candidate run IDs, sorted target finding IDs, intervention type, and configuration.
JSON object key order and input finding order do not change identity. Metadata,
predictions, trace fingerprints, and measured evidence are outside identity.

Within a project, writing exactly the same record again returns the saved record.
Writing different evidence with the same identity raises a conflict; it never
overwrites the earlier result. Use a distinct candidate run or a changed
configuration for a different intervention. Reusing a run ID for changed trace
data is detected through its fingerprint. No wall-clock creation timestamp is
part of the exported evidence, so retries remain deterministic.

Persistence requires both runs and every baseline finding to exist in the same
project. A missing or inaccessible reference is reported as missing without
revealing another project's data. Findings keep their existing identity and
lifecycle status. Retrieving a record returns its stored snapshot even after
explicit re-diagnosis replaces current findings.

## Create and retrieve stored evidence

Upload or save both traces first. Saving a trace also persists its diagnosis.
Create a request JSON file with the run IDs and baseline finding IDs from your
stored data:

```json
{
  "baseline_run_id": "run_baseline",
  "candidate_run_id": "run_candidate",
  "target_finding_ids": ["al_cache_context_example"],
  "intervention_type": "context_compression",
  "configuration": {"max_context_tokens": 2000},
  "metadata": {"task_id": "task-01", "seed": 7},
  "gates": {"min_latency_improvement_pct": 5, "min_quality_score": 1},
  "quality_fixtures": [
    {"id": "answer", "expected": "Paris", "scorer": {"type": "exact_match"}}
  ]
}
```

Replace the example IDs and quality fixture with your own evidence. `gates`
accepts the fields of `ReplayGates`; omitted fields use replay's defaults.
`quality_fixtures` uses the existing [quality scorer contract](../README.md).
Stored requests reject executable custom Python scorers. Invalid requests return
HTTP 422, unavailable references return 404, and conflicting writes return 409.

```bash
agentloop intervention-create request.json --out intervention.json
agentloop intervention-get int_YOUR_ID --out saved-intervention.json
```

These commands use the configured SQLite/Postgres store and accept `--project-id`
for local project selection. For a remote server, add `--api-url` and supply its
project key through `AGENTLOOP_API_KEY` or `--api-key`; the key selects the remote
project. The endpoints are `POST /v1/interventions` and
`GET /v1/interventions/{intervention_id}`. `AgentLoopClient.create_intervention()`
and `.get_intervention()` expose the same operations.

Creation preserves evidence even when gates fail; successful persistence returns
HTTP 200 and CLI exit 0. Inspect `gates_passed` when deciding whether to accept the
change. Retrieval/export returns the complete JSON record without recomputation.
There is no bulk list or statistical aggregation endpoint in this version.

When either source trace contains [harness decisions](HARNESS_EVIDENCE.md), creation
adds their historical snapshots to `metadata["agentloop.harness_evidence"]`. This key is reserved
for derived evidence and cannot be supplied in request metadata. Only enforced,
applied policy decisions are listed as applied candidate decisions; shadow,
failed, and intrinsic admission records remain available without becoming executed
interventions. A linked comparison does not establish an individual policy's causal
effect. Existing artifacts without this additive metadata remain valid.

## Export directly from replay

For an offline pair, add these options to the usual replay command:

```bash
agentloop replay --baseline baseline.json --candidate candidate.json \
  --intervention-out intervention.json --intervention-type context_compression \
  --target-finding al_cache_context_example --baseline-diagnosis diagnosis.json
```

Repeat `--target-finding` for multiple baseline finding IDs. The optional
`--baseline-diagnosis` file preserves predictions from recommendation time;
omitting it computes a diagnosis from the baseline with the current rules.
`--intervention-config` and `--intervention-metadata` accept JSON object files.
Export happens before replay exits nonzero for failed gates, retaining failures
as research evidence. This writes an artifact only; it does not persist the runs
or ledger record.

Run [the offline example](../examples/intervention_ledger.py) to generate a
synthetic pair, request, and ledger artifact. For a paper, archive the intervention
JSON with its source traces, original diagnosis, task/seed metadata, and quality
fixtures. Trace fingerprints link the artifact to the exact inputs. Keep failed
and indeterminate cases in the dataset; a single pair does not establish a
general improvement or empirically calibrate an estimator family.
