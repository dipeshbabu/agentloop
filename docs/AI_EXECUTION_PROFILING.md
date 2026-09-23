# Profile AI execution from trace to verified change

**Availability:** the workflow APIs and examples on this page are unreleased
additions on `main`. They are not included in the published `0.7.0` package. Run
them from a source checkout:

```bash
git clone https://github.com/dipeshbabu/agentloop.git
cd agentloop
uv sync --locked --all-extras --dev
uv run python examples/email_workflow.py --out runs/email-reference
```

AgentLoop profiles AI-powered software, including agents, classifiers, routing
rules and extraction pipelines. Your application chooses its models and executes
its decisions. AgentLoop records that execution, connects findings to evidence,
and compares an explicit candidate with a baseline under your quality criteria.

## Follow one optimization through the evidence

1. **Record the baseline.** Use `trace_workflow`, `WorkflowInfo` and `StageInfo`
   to identify the workflow, stages and input references. Record physical model
   calls and generic operations separately, with explicit dependencies where
   known. Preserve task IDs for later pairing. See [workflow tracing](WORKFLOW_TRACING.md).
2. **Define acceptable outputs.** Freeze independent labels or a versioned
   scorer before comparing implementations. [Structured quality](STRUCTURED_QUALITY.md)
   supports decisions, labels, numbers, extracted fields and matching. Missing
   outputs remain unavailable; a failed run does not pass by omitting a result.
3. **Inspect findings.** [Canonical rules](FINDING_RULES.md) link recommendations
   to their source spans. Optional [semantic investigations](SEMANTIC_WASTE.md)
   can assess repetition, contribution, progress, retries and context relevance.
   Ordinary analysis reads saved judgments; it never calls a judge automatically.
4. **Execute an explicit candidate.** Change the application and record the same
   inputs again, or run an [offline substitution experiment](MODEL_SUBSTITUTION.md)
   for one decision step. Declare the baseline, candidates and frozen criteria.
   Agreement with the baseline is separate from independently measured quality.
5. **Compare and retain the evidence.** Use replay gates and [paired studies](STUDIES.md)
   to assess quality and performance together. Retain failed, unmatched and
   indeterminate trials. The [intervention workflow](EVIDENCE_WORKFLOW.md) links
   original findings and predictions to the change and measured outcome.

A finding is a reason to investigate. Estimated savings are conditional on their
recorded assumptions; they are not measured improvements. A passing comparison
applies to its supplied cases and criteria. Application owners review the evidence
and decide whether to adopt a change.

## Choose the evidence contract

| Need | Contract and guide |
| --- | --- |
| Trace an agent or a non-agent workflow | Existing `trace_agent`, plus [generic workflow and stage metadata](WORKFLOW_TRACING.md) |
| Assess output correctness | Caller-defined [structured quality](STRUCTURED_QUALITY.md), including unavailable outcomes |
| Ask an optional semantic question | [Typed judgments](SEMANTIC_JUDGMENTS.md), with identity, source references, uncertainty and known usage |
| Connect a local model, rule or service | [Host-owned adapters and judgment benchmarks](JUDGMENT_BENCHMARKS.md) |
| Investigate unnecessary semantic work | [Evidence-bound semantic findings](SEMANTIC_WASTE.md) |
| Compare replacements for one decision | [Frozen offline substitution experiments](MODEL_SUBSTITUTION.md) |
| Review before/after evidence | [Intervention records](INTERVENTIONS.md), [studies](STUDIES.md) and [HTML reports](HTML_REPORTS.md) |

Native traces retain the existing schema and storage path; `ExecutionTrace` is
an alias of `AgentTrace`. Existing agent APIs and default behavior remain
supported. No hosted service or preferred model provider is required. Native
cost and token totals cover recorded model calls, not arbitrary service billing
or all application compute. Missing usage stays unknown. Substitution reports
identify their separate declared decision-step accounting scope.

Judgment execution is explicitly enabled by the caller. The host supplies trusted
callbacks or service adapters and controls provider spending and cancellation;
timeouts are cooperative. Judgment agreement and confidence are evidence, not
independent ground truth. Preserve backend versions and configuration provenance
when reviewing a result.

## Try the reference workflows

Each example uses synthetic inputs, independent fixture labels, local callback
implementations, native traces and baseline/candidate evidence. Use a fresh or
empty output directory for each command.

| Workflow | Run from the checkout | What to inspect |
| --- | --- | --- |
| [Email](EMAIL_WORKFLOW.md) | `uv run python examples/email_workflow.py --out runs/email-reference` | Spam, category, priority and routing; required verification retained |
| [Payment review](PAYMENT_REVIEW.md) | `uv run python examples/payment_review.py --out runs/payment-reference` | False positives, false negatives and explicit manual review |
| [Incident triage](INCIDENT_TRIAGE.md) | `uv run python examples/incident_triage.py --out runs/incident-reference` | Severity, inert response proposals and required safety checks |
| [Marketplace](MARKETPLACE_WORKFLOW.md) | `uv run python examples/marketplace_workflow.py --out runs/marketplace-reference` | Duplicate, policy, category and final-route quality |
| [Batch data](BATCH_DATA_PIPELINE.md) | `uv run python examples/batch_data_pipeline.py --records 24 --chunk-size 8 --out runs/batch-reference` | Per-record quality, chunk-level pairing and stage distributions |

The examples do not send email, move money, execute infrastructure proposals or
call external model APIs. Model-style fees and token counts are fixture units;
timings measure local callbacks. Cheap and failing variants expose quality traps.
These outcomes verify the contracts, not real-model quality or production savings.
The batch guide describes optional prompt capture and report-volume limits.

## Apply the workflow to your application

Use permission-cleared inputs, source/configuration revisions and independent
quality criteria. Set provider budgets before enabling external callbacks. Keep
the same task population across conditions and report the complete planned
denominator, including failures and missing evidence. Review captured bodies,
metadata and references before sharing artifacts.

The generic contracts and references complete [roadmap #196](https://github.com/dipeshbabu/agentloop/issues/196).
[Hardening roadmap #208](https://github.com/dipeshbabu/agentloop/issues/208) covers
finding trust, experiment design, volume, onboarding, drift and real-world proof.
Real-agent validation in [#181](https://github.com/dipeshbabu/agentloop/issues/181)
and the [runtime roadmap #182](https://github.com/dipeshbabu/agentloop/issues/182)
remain separate work; these synthetic examples do not satisfy their empirical
requirements.
