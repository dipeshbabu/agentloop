# Offline semantic waste investigations

AgentLoop can investigate five forms of potentially unnecessary work through its
[typed judgment contract](SEMANTIC_JUDGMENTS.md) and canonical finding engine.
The feature is opt-in: ordinary reports only read saved investigations and never
invoke a judge. It does not remove steps, stop/replan a run, or enforce completion.

```console
python examples/semantic_waste.py --out runs/semantic-waste
```

The example uses synthetic traces and deterministic local fake judges. It
produces five finding families and retains repeated-name traps, intentional
verification, missing evidence, low confidence, disagreement and judge failure.
These fixtures test evidence handling; they do not establish real-workload
quality or savings.

## Explicit investigations

```python
from agentloop import SemanticInvestigation, build_diagnosis, evaluate_semantic_waste

case = SemanticInvestigation(
    investigation_id="redundancy-review-1",
    family="semantic_redundancy",
    target_spans=("later-step",),
    reference_spans=("earlier-step",),
    task_criteria="Preserve the independently checked routing label",
    criteria_ref="quality:routing-scorer-v2",
    summaries={
        "earlier-step": "Approved summary of the existing result",
        "later-step": "Approved summary of the repeated computation",
    },
)
evidence = evaluate_semantic_waste(
    trace, [case], judges=[my_backend], enabled=True, timeout_s=2,
)
diagnosis = build_diagnosis(trace)
```

Backends implement the same provider-independent protocol used by the
[adapter benchmark](JUDGMENT_BENCHMARKS.md). Supply trusted local callbacks or
explicit host-owned adapters. Execution defaults to `enabled=False`; timeouts
are cooperative and the host owns provider budgets and cancellation.

Targets identify the work under investigation. References identify prior work,
downstream outcomes, context or task state, as appropriate. Both sets must be
nonempty, unique and disjoint. Criteria text and the criteria reference are
required. References remain opaque: AgentLoop never fetches a URL, file or
scorer from a trace declaration. Supply permission-cleared criteria, summaries
and identifiers; those explicit values are forwarded and stored.

The helper accepts at most 100 investigations and eight backends per call, with
at most 64 selected spans per investigation. Native requests contain source
hashes and approved summaries, not automatically extracted prompts, tool
payloads or error messages. All selected spans need approved summaries before
a semantic judge is called. Missing evidence and already-required retention
skip judgment execution. No hidden repair, retry or automatic investigation
selection occurs.

## Finding families and evidence requirements

| Rule ID, version 1.0 | Question | Required context |
| --- | --- | --- |
| `semantic_redundancy` | Does target work materially repeat an already available result? | Reliable ordering with reference results available before targets |
| `low_contribution` | Does target work add no material information to the supplied downstream result? | At least one reference outcome recorded after the target steps |
| `semantic_no_progress` | Does the target sequence fail to advance the task criteria? | At least two ordered, nonoverlapping target spans and reference task context |
| `retry_usefulness` | Do target retries repeat failure/input without useful new evidence? | A recorded retry event or target metadata `retry_of` naming a selected reference span |
| `context_relevance` | Is supplied context unrelated to the recorded decision? | A model or declared classifier/rule target and approved contextual evidence |

Every family uses a versioned, purpose-bound boolean question. Caller criteria,
target/reference roles and the selected source fingerprints are bound into the
request. The criteria reference digest also changes the request/cache identity
when a referenced criterion version changes. Arbitrary saved judgments are not
silently repurposed into findings.
Repeated tool/model names alone never establish semantic waste. Ordering gaps,
absent retry relations and incompatible target kinds remain explicit unknowns.

Deterministic evidence is preferred only for a narrow exact-reuse case. Declare
`reuse_safe=True` and a `reuse_contract_ref` if the host guarantees complete
inputs, unchanged operation semantics and safe reuse without required side
effects or an independent check. AgentLoop then compares one prior and one
target span: reliable order, successful status, equal names/model/kind,
nonempty exact inputs/outputs, equal usage counts and equivalent metadata.
Transport IDs and known billing fields are ignored in the metadata comparison.
Other changed metadata, such as model revision or parameters, prevents this
shortcut. The contract remains an explicit caller assumption, not something
inferred from matching names or bodies. Successful exact reuse skips judges and
produces at most medium confidence.

Set `retention_required=True` for intentional verification, polling, audit or
other required repetition. Such a case produces no finding, even if outputs
match. Semantic judges must all return known `True` answers to support a finding;
one `False` disagrees with a positive answer, and unknown, failed, timed-out or
stale evidence cannot support a recommendation. An explicitly reported confidence
below `minimum_confidence` (default 0.8) abstains. Missing numeric confidence is
not filled in; a known affirmative remains a low-confidence investigation
hypothesis. Even supplied calibrated confidence cannot automatically create a
high-confidence semantic recommendation. Calibration references remain assertions
to assess independently.

## Conditional savings and unavailable measurements

Without `removal_attribution_ref`, latency, tokens and cost savings remain
unavailable. This reference is the caller's declaration of a specific
whole-target removal scenario, including side-effect and dependency safety.
It does not authorize execution or establish independent task quality.

The `semantic_leaf_removal` estimator, version 1.0, records its formula, observed
inputs, billing/pricing provenance, assumptions and unmodeled metrics:

- Targets must be leaf spans, avoiding nested double counting.
- Elapsed latency is estimated only for a valid, flat, reliably timed execution
  with no overlap anywhere in the trace. The conditional upper bound is the sum
  of selected span durations, assuming the remaining execution stays serial and
  no replacement work or extra overhead is introduced.
- Token removal is estimated only for physical model calls with exact-grade
  token provenance. Approximate or absent usage stays unavailable.
- Model cost requires attributable provider-reported billing, or recorded pricing
  applied to exact token counts, and evaluable trace cost. Generic service/tool
  billing is not fabricated from model rates.
- `context_relevance` does not attribute an entire call's cost/tokens to an
  allegedly irrelevant context fragment. Without fragment attribution, those
  savings remain unavailable.

All estimates are uncalibrated conditional upper bounds, not realized savings.
Required validation is a paired intervention/replay with an independent scorer
implementing `criteria_ref`, preservation of required side effects, and adequate
cost/token evidence. Judges do not replace that scorer.

Unavailable latency/cost is JSON `null`, not zero, in the new findings, stored
rows and queue. The existing nullable database columns need no migration.
Unknown savings sort below known savings within a priority tier consistently in
SQLite and PostgreSQL. Queue clusters retain unmodeled latency counts and
unavailable totals. Compatible modeled candidates alone contribute to plan
aggregates; `latency_estimate_complete=False` and an unmodeled count identify
partial latency estimates, with corresponding cost completeness when applicable.
Views label these totals as covering modeled candidates only.
Findings overlap on their target spans, so the existing
compatible-subset selector still prevents double counting. Semantic hypotheses
do not add engineering-hour or reliability benefits merely by increasing the
finding count in the operational value report.

Semantic findings are never auto-patchable and always require a quality scorer.
Large conditional resource estimates do not upgrade their confidence or severity
to high. Reference spans, judge identities/configuration, receipt hashes,
uncertainty, assumptions and validation criteria remain attached to the finding.

## Saved evidence and analysis views

The metadata namespace `agentloop.semantic_waste`, version `1.0`, stores the run
ID, investigation definitions, frozen requests, evidence basis and references to
native judgment receipts under `agentloop.judgments`. An envelope hash covers
the saved definitions and references. Hashes validate integrity and bindings,
not authenticity or truth. Existing host metadata is preserved.

An investigation ID cannot replace an earlier result; use a new ID for a
reevaluation. Native request and source bindings make changed evidence stale.
If a transport redacts source content, receipts survive but an answer can become
stale. A trace must remain unchanged during explicit evaluation; the helper
rejects a source mutation before committing new metadata. It does not undo host
side effects or provider charges caused by a misbehaving callback.

`read_semantic_waste`, report construction, canonical finding rules, diagnosis,
HTML, Markdown, CLI, storage and dashboard views never run a judge. They expose
supported, unsupported, retained, low-confidence, disagreement, stale and unknown
states. Only supported cases become finding candidates. The separate
`semantic_waste.status` describes investigation completeness; the existing
`analysis_complete` field still describes deterministic finding-rule execution.
Legacy traces without this namespace retain their previous findings and report
shape. Finding identities include investigation/criteria context so different
investigations cannot silently share reviewed status.
