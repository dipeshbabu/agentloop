# Batch extraction, classification and matching reference

This deterministic synthetic workload profiles repeated inference over records:

```text
record -> extract fields -> classify -> entity/pair match -> output reference
```

```console
python examples/batch_data_pipeline.py --records 24 --chunk-size 8 --capture-prompts --out runs/batch-data-reference
```

Use a fresh/empty output directory. No external model API, vector database,
warehouse, ETL engine or dataset service is required. The record generator and
reference backends are synthetic; this example does not establish real-model
quality, spend or production throughput.

## Records, implementations and quality

Each generated record has an opaque fixture ID, a small structured text input and
independent expected extracted fields, category and entity pairs. Golden categories
and matches come from a fixed fixture pattern separate from the implementation's
lookup tables. Unknown/ambiguous catalog aliases have an expected empty pair set;
that known unmatched outcome is distinct from a failed or missing match.

| Configuration | Execution |
| --- | --- |
| `baseline` | Per-record extraction, classification and matching through model-style local fixtures |
| `optimized` | One batched extraction call per chunk, then local classification and indexed matching rules |
| `cheap` | Incorrect unit truncation, guessed categories and overconfident entity matches |
| `failing` | A declared match timeout on one record, with other records retained |

All configurations execute the same frozen records. Quality contract 2.0 uses
explicit fields, closed-decision and matching scorers for every record. Completed
extraction/classification outputs survive a later match failure; the absent match
remains unavailable. Records are not removed from the denominator. The primary
quality gate requires the full labelled comparison, so a cheap or failed variant
cannot become an optimization win by dropping inconvenient records.

Each **chunk** is a generic workflow, not an agent per record. Stage names remain
stable across records while event IDs are unique. Dependencies preserve each
record's path; a batched extraction span explicitly names its record coverage.
Studies pair the same chunks and corpus hash. Batch-level billing is never divided
into invented per-record costs.

## Profiling and optimization evidence

The summary aggregates per-stage call/model-call counts, failure count/rate,
fixture input/output token units, cost completeness and latency/cost distributions
(including median and p05/p95). Raw trace/report/diagnosis artifacts remain
available per chunk and configuration.

Model-style fixtures report fees with their synthetic rate-card provenance and
the documented `json-whitespace-fixture-v1` tokenizer. These are fixture units,
not real LLM tokens or actual provider charges. Timings measure the local callbacks
and retain normal replay gates; no artificial sleep or guaranteed speedup is used.

With `--capture-prompts`, actual serialized synthetic model inputs are retained.
Their shared schema instructions make repeated context and cache hypotheses
observable. Repeated named model roles expose batch candidates; the fixture also
declares common schemas, independent records and no side effects. Those declarations
support investigating batching, not a general claim that every repeated call is
safe to batch. The executed optimized configuration is separately checked against
the independent record labels.

The `substitution/` directory demonstrates the [offline model-substitution
workflow](MODEL_SUBSTITUTION.md) for classification. A model-style fixture, a
correct kind rule and a cheap guess use frozen extracted records and a versioned
decision scorer. Independent quality, baseline agreement and usage remain separate;
no implementation is activated automatically.

## Larger runs and retention tradeoffs

Required CI runs only a small corpus. A larger optional run is:

```console
python examples/batch_data_pipeline.py --records 1000 --chunk-size 100 --html-samples 1 --out runs/batch-data-1000
```

Record count is bounded between 4 and 10,000; chunk size between 1 and 1,000.
`--html-samples` keeps zero to ten leading chunk analyses per configuration.
Without `--capture-prompts`, traces retain input hashes/references rather than full
model prompts. Counts, timings, outcomes and all workflow events are still recorded.
The synthetic source corpus and parsed outputs remain separate local artifacts.

These options illustrate diagnostic/payload volume tradeoffs. This reference is
not a streaming aggregation or statistically representative trace sampler: event
and outcome retention remain linear in record count, and leading HTML samples
are not a random sample. For production, choose an explicit sampling/retention
policy, preserve failures and representative cases, disclose coverage, and keep
independent quality denominators separate from a retained-trace sample. Never
assume every production record must retain full payloads.

Outputs include `fixtures.json`, `summary.json`, per-stage distributions,
record failure inventories, original traces/outputs/reports/diagnoses, sampled
HTML analyses, per-chunk quality/replay artifacts, native paired studies and the
classification substitution evidence. All empirical interpretations remain
exploratory and specific to the supplied workload and provenance.
