# Email pipeline reference

This reference executes a small email decision pipeline through the
[generic workflow contract](WORKFLOW_TRACING.md), then compares independent labels
using [structured quality](STRUCTURED_QUALITY.md), replay and paired studies.

```console
python examples/email_workflow.py --out runs/email-reference
```

Use a fresh or empty output directory for each run. No mailbox, mail provider,
model download or external API is involved. All messages, backend fees and token
units are synthetic fixtures. Routes are output labels only: the example never
sends, forwards, deletes or otherwise acts on email.

## Executed configurations

| Configuration | Implementation | Comparison purpose |
| --- | --- | --- |
| `baseline` | Model-style local fixtures for every decision, irrelevant context, and repeated priority classification | A deliberately wasteful reference |
| `hybrid` | Rules for spam/priority/route, a fixture backend for category, and required verification retained | Lower fixture cost with the same labelled outcomes |
| `cheap` | Rules throughout with a naive urgency shortcut and no verification | Lower cost does not establish acceptable quality |
| `failing` | Fixture backends with a declared category timeout on one case | A fast failed run is not an optimization win |

The frozen corpus in `examples/email_workflow.py` includes support, billing, bulk
news, spam, ordinary mail and a required-verification case. Gold outputs cover
`spam`, `category`, `priority`, `route`, and the explicit `verified` policy flag.
Labels are separate from the application classifier functions and are not passed
as their inputs. Traces record the corpus hash and configuration version.

The cheap rule treats "urgent" in bulk news as high priority, which fails the
independent label. It also drops required verification. The hybrid preserves that
check instead of treating every repetition as waste. These are fixture policies,
not a general spam model, security policy or universal email taxonomy.

## Measurements and provenance

Each decision records stage identity/version, declared dependencies, actual
callback duration, outcome and input/output hashes. Calls to the local model-style
backend are marked model calls from `agentloop-fixture`; rule steps remain rule
operations. Workflow identity keeps agent-loop heuristics from interpreting a
pipeline as an agent. Timeout cases retain a failed stage/workflow, no invented
output, and unavailable billing where the fixture returned no billing response.

The mock backend defines `json-whitespace-fixture-v1`: tokenize canonical JSON
input/output by whitespace. Counts are exact **for this fixture tokenizer**, not
measured token counts for a real LLM. Its rate card reports a synthetic fee per
fixture token. Each model-style span names the tokenizer, rate-card reference and
`reported_by_synthetic_backend` cost basis. Every trace is marked synthetic.
The profiler consumes the mock backend's reported fee to exercise accounting;
there are no actual provider charges or demonstrated real-provider savings.

Timings come from executing local functions, without sleeping to invent model
latency. Replay keeps normal latency gates; short callback timing noise can
change a run's overall gate result. Tests assert deterministic labels, fixture
cost reductions and retained failures, not a stable latency improvement. Every
measured delta and gate result remains available in the artifacts.

The classifiers and routing function decide labels. AgentLoop observes those
decisions and checks caller-defined criteria; it does not become the classifier
or execute the returned route.

## Semantic investigations

Baseline traces include explicit [semantic investigations](SEMANTIC_WASTE.md)
for priority repetition and context relevance. The default `LocalCallbackJudge`
is a deterministic fallback over approved fixture summaries. It is not a general
semantic model. To exercise another explicitly configured `JudgmentBackend`, call
`main(out, judge=my_backend)` from Python; the host owns its API usage, budget and
cooperative cancellation.

The verification case declares `retention_required`, so it cannot produce a
semantic redundancy recommendation. Judge failure/unknown results do not become
positive findings. Irrelevant context can produce a low-confidence investigation
hypothesis without unsupported whole-call savings attribution. Task quality comes
from the separate frozen labels, never from agreement with the semantic judge.

## Artifacts

The output contains:

- `fixtures.json` and `summary.json` with the frozen corpus and comparison inventory;
- per-candidate/per-case quality and replay JSON/Markdown, including unsuccessful cases;
- native `study.json` manifests and paired study reports for each candidate;
- original traces, profiling reports, diagnoses and standalone HTML analyses for every run.

Failed and lower-quality candidates remain in their denominators. A timeout has
unavailable quality/cost evidence instead of an invented cheap success. All
variants use the same inputs and scorer version. No candidate is selected or
activated automatically.

To adapt the example, replace application-owned classifiers and independent
labels with permission-cleared inputs and explicit provider usage. Preserve stage,
configuration and scorer versions, then rerun paired evaluation. Do not reuse
fixture fees/token units as real-model measurements or generalize capability from
these small synthetic samples.
