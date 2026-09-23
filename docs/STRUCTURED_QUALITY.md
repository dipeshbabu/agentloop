# Structured workflow quality

Quality contract **2.0** compares routing decisions, labels, bounded numbers,
extracted fields and entity pairs. It uses the existing quality, replay,
intervention and study APIs. Legacy fixtures without a version keep their existing
scorers, result format and behavior.

AgentLoop evaluates caller-defined criteria. A pass applies to those criteria
and supplied examples; it does not establish universal correctness or authorize
a production decision.

## Opt in explicitly

Save a versioned fixture suite:

```json
{
  "schema_version": "2.0",
  "fixtures": [
    {
      "id": "route-message-1",
      "expected": "billing",
      "baseline_output": {"route": "support"},
      "candidate_output": {"route": "billing"},
      "input_ref": "task:message-1",
      "expected_ref": "reference:message-1",
      "scorer": {
        "type": "decision",
        "version": "1.0",
        "field": "route",
        "labels": ["billing", "support"]
      }
    }
  ]
}
```

```bash
agentloop quality-report fixtures.json --out quality.md --json-out quality.json
agentloop replay --baseline before.json --candidate after.json --quality-fixtures fixtures.json --min-quality-score 0.9
uv run python examples/structured_quality_workflow.py --out runs/structured-quality
```

The [example](../examples/structured_quality_workflow.py) creates deterministic
synthetic routing, extraction and matching evidence, plus a timeout and an
unmatched example. It preserves original predictions, links four intervention
records, and generates quality/replay/study JSON and Markdown. Three configured
fixture comparisons pass; the timeout cannot win by doing less work. All five
baseline tasks remain visible. These are contract tests, not measured application
benefits or empirical calibration.

`parse_quality_fixtures`/`load_quality_fixtures` copy the wrapper version onto each
fixture. Direct Python and HTTP lists declare `"schema_version": "2.0"` on each
fixture. Every fixture in a structured suite must use the same contract and a
unique nonempty `id`. Mixed/unknown versions and unknown configuration fields
fail explicitly. New scorer types require the version marker.

## Scorers and criteria

Built-in scorer implementation version is `1.0`, separate from the `2.0` report
contract. Omission selects that built-in version. Trusted custom scorers require
an explicit implementation version.

| Type | Configuration and behavior |
| --- | --- |
| `decision` | Nonempty typed `labels`; exact equality to the reference label. Unknown predictions are explicit failures, not dropped observations. |
| `multilabel` | Declared label domain and unique list outputs; set F1 with TP/FP/FN counts. Duplicate predictions are invalid. Two empty sets score 1 by the declared empty-set convention. |
| `numeric` | Finite `minimum < maximum`, nonnegative `tolerance`, optional `unit`; score 1 within the reference tolerance, otherwise 0. Decimal tolerance comparisons use declared values; unrepresentable absolute-error magnitudes remain unavailable. |
| `fields` | Nonempty expected object; fraction of expected top-level fields with exactly matching typed JSON values. Extra top-level fields are allowed by default; `allow_extra: false` adds them to the denominator. Nested values compare exactly. This is not full JSON Schema validation. |
| `matches` | Unique two-element entity-ID pairs; set F1. Pairs are directed unless `symmetric: true`. Typed IDs remain distinct. |
| Legacy types | `exact_match`, `contains`, bounded `glob`, `required_fields`, and `json_subset` can also opt into the new evidence contract. Their configured predicates remain the same. |
| `custom` | Synchronous trusted local `module:function`, explicit `version`, optional JSON `configuration`; returns a boolean or `{passed, score, detail}`. HTTP and stored-intervention requests continue to reject custom executable scorers. |

Structured types accept an optional `field`, a literal top-level output-object
key. Object/set/matching scorers can read JSON-encoded container strings. Decision
labels themselves are not coerced: `false`, `0`, `1`, `1.0` and `"1"` remain typed
values. Explicit JSON null is distinct from missing output.

`pass_score` defaults to 1 and applies to each assessed case. Unknown labels or
invalid output shapes cannot pass merely through a lower threshold. A custom
scorer's explicit rejection remains a rejection. The suite's `min_score` also
applies to its aggregate. Gates use unrounded scores and exact arithmetic over
declared decimal score values; presentation rounding cannot turn a miss into a
pass. Users must select meaningful task-specific criteria and thresholds.

## Missing data, failures and provenance

Output lookup is explicit: side-specific fixture output, candidate `output`, or
`trace.metadata.output`. Contract 2.0 does not guess a workflow's final output from
its last model span. References are not fetched automatically. Applications that
keep trace bodies private can supply evaluation outputs through separate fixture
artifacts while retaining only references/hashes in reports.

Known wrong or malformed outputs score zero with an explicit status. Missing
outputs/reference outcomes, unknown reference labels, scorer failures and scorer
timeouts remain unassessed (`score: null`). Per-case statuses and side-specific
assessed/unavailable denominators remain visible. A side's aggregate `score` is
unavailable unless every case was assessed and execution is complete/unreported;
`observed_score_mean` describes only the available assessments and cannot bypass
the gate.

Execution status can be declared per fixture with `baseline_status` or
`candidate_status`; workflow status is used when available. Known task failure
metadata cannot be overridden by a fixture claiming completion. Correct partial
output from a failed/cancelled/timed-out run does not make the overall quality
gate pass. Standalone output scoring uses `unreported` execution status and makes
no claim about whether an agent actually ran.

Custom exceptions/timeouts and built-in parsing limits are explicit unknown
evaluations. Exception bodies and custom details are omitted by default; custom
`capture_detail: true` opts into bounded supplied detail. No hard timeout or
sandbox is inserted around trusted Python code. The host must bound scorer work;
host cancellation and other fatal exceptions propagate. Inputs/configuration are
copied so one scorer invocation cannot accidentally mutate the next one's data.

Reports preserve contract/scorer versions, implementation references,
configuration hashes, expected/output hashes, reference IDs and source trace IDs.
They omit raw input/output bodies and raw scorer configuration by default.
Keep the original fixture/configuration artifacts alongside a report for
reproduction. Hashes identify evidence; they do not authenticate a grader, prove
independence, or anonymize predictable labels.

## Classification counts

`decision` reports optionally include confusion-derived metrics grouped by scorer
configuration. Matrices are sparse `[expected_index, predicted_index, count]`
triples with `matrix_format: "sparse_row_column_count"`; the final prediction
column represents invalid/out-of-domain predictions. Storage grows with labels
and observed pairs rather than the square of the declared label count.

Accuracy/precision/recall/F1 describe available labelled outputs. Missing outputs,
unlabelled cases, invalid predictions, unassessed outputs and coverage remain
explicit. Missing references and outputs can overlap. These conditional metrics
are not substitutes for end-to-end task success; an incomplete suite still cannot
pass. Other scorer families do not receive an imposed confusion matrix.

## Attach results for studies

```python
from agentloop import attach_quality_report
from agentloop.quality import build_quality_report, parse_quality_fixtures

# suite is a caller-owned {"schema_version": "2.0", "fixtures": [...]} object.
fixtures = parse_quality_fixtures(suite)
quality = build_quality_report(
    fixtures, baseline_trace=baseline, candidate_trace=candidate, min_score=0.9
)
attach_quality_report(baseline, quality, side="baseline")
attach_quality_report(candidate, quality, side="candidate")
```

Attachment writes versioned `agentloop.quality` metadata without overwriting host
metadata. It validates report identity/hash and any trace binding. Attach before
saving the final traces and creating ledger records, so their fingerprints include
the selected evidence. A report produced without source trace IDs is an explicit
caller-provided association when attached later.

Replay uses the new evidence, leaves unknown score/delta values null, and adds
fail-closed quality gates. It does not fall back to an older scalar score when
attached evidence is invalid or incomplete. Original output predicates and
unversioned quality behavior stay intact. Intervention records preserve the
quality report without changing prediction snapshots; deterministic results keep
same-input ledger creation idempotent.

Existing paired studies consume attached side evidence. They retain unmatched,
failed and unscored runs and export scorer provenance alongside quality metrics.
Versioned reports also expose recorded classifier/rule decision-span counts and
error-span counts with existing model/tool/resource metrics. These count recorded
spans, not all possible hidden decisions or unique business outcomes. Caller
pairing keys still determine experiment identity; repeated tasks need appropriate
task-aware uncertainty analysis, as described in [studies](STUDIES.md) and
[ablations](HARNESS_ABLATIONS.md).
