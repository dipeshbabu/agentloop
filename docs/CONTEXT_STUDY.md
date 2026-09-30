# Optional tool-result summary study

This study tests the [explicit context transformation](CONTEXT_CONTROLS.md) on
a host-prepared SQL answer stage. The application supplies an older background
sample, marks that result optional, and protects the current query result.
AgentLoop summarizes only the optional result when enforcement is enabled.
The final model answer is checked against frozen query-result labels.

This is an unreleased source-checkout feature. The study extends the native
[harness ablation protocol](HARNESS_ABLATIONS.md), using the same public Wine
Quality data and pinned local Qwen3-4B model as the
[earlier harness study](REAL_HARNESS_STUDY.md). These are new task questions.
It evaluates answer construction from provided results, not autonomous SQL
planning, arbitrary summarizer fidelity or production policy promotion.

## Results

All 80 planned observations completed with correct final answers. All have exact
server token counts and unknown self-hosted operating costs; no paid provider
was used. There were no missing, cancelled or failed real observations. Offline
tests separately retain and reject malformed summaries, wrong answers and
interrupted attempts.

| Held-out condition | Correct answers | Mean calls | Mean total tokens | Mean task time |
| --- | --- | --- | --- | --- |
| `off` | 8/8 | 1 | 863.75 | 5.981 s |
| `trace` | 8/8 | 1 | 863.75 | 5.939 s |
| `shadow_context` | 8/8 | 1 | 863.75 | 5.921 s |
| `enforce_context` | 8/8 | 2 | 1,125.75 | 10.407 s |
| `shadow_budget` | 8/8 | 1 | 863.75 | 6.008 s |
| `enforce_budget` | 8/8 | 1 | 863.75 | 5.929 s |
| `shadow_combined` | 8/8 | 1 | 863.75 | 5.949 s |
| `enforce_combined` | 8/8 | 2 | 1,125.75 | 10.569 s |

Summarization reduces mean final-call input from 843.5 to 289.5 tokens (65.7%),
but adds 816 tokens for the summary call. Every matched enforced-summary pair
uses 262 more total tokens. Relative to tracing alone, total tokens increase
30.3% and mean context-only task time increases 75.2%. The resource regression
is retained: this configuration preserves the sampled answers but demonstrates
no overall token or latency saving. The budget remains nonbinding.

Mean context preparation takes 7.752 seconds under enforcement and 0.391 ms in
shadow mode; shadow mode executes no summarizer. Combined enforcement takes
7.891 seconds for preparation and 2.100 ms in policy callbacks. Those callback
times exclude inference and must not be presented as the total overhead.

Tracing minus uninstrumented task time averages -42.086 ms, with a task-bootstrap
interval of [-83.136, 29.341] ms. Shared-host variation is larger than many small
wrapper differences; this is not evidence of negative intrinsic tracing overhead.
The full report retains task-weighted intervals and all comparison denominators.

The [checksummed evidence index](../research/context-transform-2026-09/index.json)
contains all host receipts, 70 native traces, context/budget decisions, original
source data, frozen implementation, native reports and HTML examples. The ten
uninstrumented observations have host receipts without fabricated traces.

## Frozen design

The protocol contains one pilot and four held-out tasks, two repetitions and
eight conditions: 80 planned observations, including 64 held-out observations.
The held-out schedule balances each condition across every order position.
Task labels, source/configuration/model/scorer/environment versions, execution
code hashes and acceptance criteria are fixed before the first observation.

| Condition | Execution |
| --- | --- |
| `off` | Host receipts without AgentLoop tracing or policy wrappers |
| `trace` | Native model/tool spans without policies |
| `shadow_context` | Record the context proposal and send the original request; no summary call |
| `enforce_context` | Summarize the optional background result, validate it, then answer |
| `shadow_budget`, `enforce_budget` | Model-call and token limits without context transformation |
| `shadow_combined`, `enforce_combined` | Context and budget policies together |

The count limit is two model calls and the token limit is 18,000. Main and summary
calls each reserve an upper bound of 4,288 tokens within the same run. Parent
admission occurs before preparing summaries, so both reservations can be held
at once. These limits allow the intended summary-plus-answer path; they are not
chosen after observing held-out outcomes.

Every observation receives a fresh read-only database, request, usage collector
and, where configured, harness run. The two source queries are executed by the
host. Labels were computed before inference; the scorer checks final row order,
values and successful SQL-tool evidence independently of model responses. It
does not claim to independently validate the SQLite implementation.

The old sample contains 24 rows. A successful summary must be nonempty, shorter
than its source and at most 480 characters; the summarizer prompt requests 400.
System/user messages, tool-call declarations and current evidence are preserved.
Summary text remains a tool result. Invalid summaries fail explicitly and retain
incurred usage. No fallback or prompt adjustment is chosen from study results.

The same pinned model handles summaries and final answers, with a 192-token
output bound. Provider prompt reuse is disabled. A fixed readiness request is
retained outside measurement; GPU/runtime caches remain warm and the machine is
shared. Seeds are recorded without claiming determinism. Model loading, database
construction, grading and artifact export are outside task timing. Request
construction, source queries, tracing/harness setup, summaries, final inference
and database cleanup are inside it.

## Evidence and interpretation

Total model calls, tokens and task time include the summarizer. The final
answer call's prompt length is a separate diagnostic. A shorter final prompt
does not establish lower total work or preserved quality. Summary preparation
time includes its inference and admission work; policy callback time alone does
not measure the full cost of the transformation.

Actual local server responses supply token counts. Self-hosted operating cost
is unavailable; paid-provider spend is zero. No zero operating-cost rate or
provider-cache discount is invented. Native comparisons retain unknown costs,
failed transformations, incomplete outcomes and explicit planned denominators.

The exporter recomputes independent quality, checks slot identities, resource
totals, trace hashes, and agreement between native policy/context evidence and
host receipts. Empty or interrupted studies retain every planned slot. Unknown
attempt directories and changed evidence require review rather than being
silently dropped. Cancelled observations retain partial call evidence while
their complete-task measurements remain unavailable.

The native study and ablation reporters provide paired comparisons and
task-aware uncertainty. Repetitions do not become additional independent tasks.
Context, budget and combined arms can be compared directly in retained native
study artifacts; the ablation report distinguishes tracing, policy and
enforcement effects. The intervention-ledger list is explicitly empty because
this caller-configured transformation is not attributed to an estimator finding.

Only four held-out tasks and one host are represented. Required current evidence
was deliberately protected, and the host knew the old sample was optional.
Results cannot establish that summarization is safe when required information
is included in the selected text. No production policy is activated.

## Reproduction

The [example package](../examples/context_study/) separates protocol preparation,
measurement and read-only export. `protocol.py` freezes tasks and source/code
hashes; `runner.py` executes only with explicit `--execute` against a verified
loopback model; `export.py` validates retained evidence and delegates to existing
native reports. Required CI uses offline model responses and never starts a
model service.

From a checkout containing the exact frozen core and study source:

```bash
python examples/real_agent_study/archive.py research/context-transform-2026-09/index.json --out /tmp/context-evidence
python -m examples.context_study.runner --plan STUDY_ROOT/plan.json --export NEW_REPORT_DIRECTORY
```

The bundle supplies the source overlay for core revision
`320b47e6a72857359e556d127f749503a466f3ea`. Preserve its exact source bytes, including
the measured runner's Windows line endings, when checking the original hashes.
The source snapshots are an overlay for a complete checkout, not an installable
distribution by themselves.

This command performs no inference. To run a new study, prepare a fresh root
with `python -m examples.context_study.protocol --out NEW_ROOT --sources DATA_DIRECTORY`,
start the pinned server, retain a separate readiness warmup, then explicitly run
`python -m examples.context_study.runner --plan NEW_ROOT/plan.json --model-file MODEL_FILE --execute`.
Changed code, data, models or policies require a new frozen protocol.

The original September 24 setup was interrupted before any task attempts. Its
plan and readiness warmup are retained separately. Before September 30 execution,
the exporter was hardened for interrupted studies and a new implementation hash
was frozen. No task outputs were discarded or used for tuning.

UCI Wine Quality remains under CC BY 4.0, with source attribution in the evidence
bundle; see the [source permissions](REAL_AGENT_STUDY.md#sources-and-permissions).
Study code is Apache-2.0. Model weights and inference binaries are not redistributed.
