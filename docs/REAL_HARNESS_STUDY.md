# Real SQL-agent harness ablation

This study evaluates the same local SQL agent with no tracing, tracing alone,
and matched shadow/enforce conditions for two independent model-call caps. It
uses actual Qwen3-4B inference and independent query-result scorers over the
public UCI Wine Quality measurements. It does not change production policies.

The [preceding intervention study](REAL_AGENT_STUDY.md) established the workload
and retained model-routing failures. This experiment uses nine new questions,
not the held-out questions from that study. It runs the unreleased harness source
at core revision `009c8a475265c41491e47aed002a3d67597e9723`; the published 0.7.0
package does not contain these harness APIs.

## Results

All 108 planned observations are retained: 90 completed correctly and 18 stopped
at the enforced one-call cap. All 108 have exact server token counts and unknown
self-hosted operating cost. There were no missing, cancelled or timed-out real
attempts; offline tests exercise those retention paths. No paid provider was used.

| Held-out condition | Successful tasks | Mean model calls | Mean task time | Mean policy callback time |
| --- | --- | --- | --- | --- |
| `off` | 12/12 | 2 | 6.436 s | 0 |
| `trace` | 12/12 | 2 | 6.147 s | 0 |
| `shadow_2` | 12/12 | 2 | 6.541 s | 2.286 ms |
| `enforce_2` | 12/12 | 2 | 6.579 s | 1.951 ms |
| `shadow_1` | 12/12 | 2 | 6.626 s | 2.172 ms |
| `enforce_1` | 0/12 | 1 | 3.148 s | 1.870 ms |

The one-call cap reduces work but prevents a correct final answer. All 12
held-out enforce-versus-shadow pairs are rejected by the native quality gate;
none becomes a quality-preserving improvement. The two-call cap preserves
sampled quality but never binds: model calls and token counts are unchanged.
It therefore demonstrates no resource-saving benefit on these tasks.

Wall-clock deltas include model/runtime variation. For example, tracing minus
uninstrumented time averages -288.868 ms, with a task-bootstrap interval from
-912.496 to 40.221 ms. That is not evidence that tracing has negative intrinsic
overhead. The separately measured callback times above cover policy evaluation,
not the entire tracing/wrapper path. The complete native report retains all
descriptive and quality-preserving comparisons and their denominators.

The [checksummed evidence index](../research/harness-ablation-2026-09/index.json)
includes the native ablation report, all original host receipts, 90 native traces,
policy decisions/budget snapshots, source data and the frozen implementation.

## Frozen design

The native `AblationProtocol` declares three pilot tasks and six held-out tasks,
two repetitions and six conditions: 108 planned observations. Within each split,
cyclic condition orders balance every condition across every order position.
Task IDs, input hashes, labels, source/config/model/scorer/environment versions,
reset procedure and quality thresholds are frozen before measurement.

| Condition | Measured path |
| --- | --- |
| `off` | Original agent with host receipts, no AgentLoop spans or policy wrapper |
| `trace` | Same agent with native model/tool spans, no policy wrapper |
| `shadow_2`, `enforce_2` | One count-only policy permitting at most two model calls |
| `shadow_1`, `enforce_1` | One-call cap, explicitly designated an overly restrictive negative control |

The two-call cap follows the preceding SQL pilot's two-call pattern, not new
held-out outcomes. The one-call negative control should expose faster incomplete
work: the ordinary agent needs a model call to request SQL and another to answer
from its result. Neither cap changes prompts, model options, tools or scorers.
No policy combination or automatic promotion is tested.

Every condition gets a fresh in-memory read-only database, message history,
usage collector and, where configured, `HarnessRun`. The host explicitly wraps
the model-call boundary. Shadow proposals are recorded without denying dispatch;
enforcement can reject the next call before the inference request is sent.
The host maps an applied control signal to a stopped observation rather than a
successful completion.

The model and Vulkan runtime are the pinned baseline artifacts from #181. Prompt
KV reuse is disabled in every request. A fixed readiness request occurs outside
measurement; GPU/code caches remain warm and the host is shared. Recorded seeds
do not imply bitwise determinism. The timed region includes trace/harness setup
and agent execution, including tool cleanup. Database construction, model loading,
grading and artifact export occur outside it.

## Evidence and interpretation

The runner records external monotonic task time in every arm, including `off`;
it does not infer uninstrumented latency from fabricated spans. Token usage comes
from actual server responses. Self-hosted operating cost remains unavailable and
paid-provider spend is zero. Policy evaluation time sums measured hook callbacks;
it is distinct from wall-clock overhead, which comes from matched arms.

The existing native ablation reporter separates tracing overhead, policy overhead,
and enforcement effects, and uses task-aware uncertainty. Its quality criterion
requires completed, successful, independently correct outputs in both arms.
An early stop therefore cannot count as quality-preserving improvement just
because it saves time or tokens. Repeated runs do not increase the independent
task count. Missing and interrupted slots remain in planned denominators.

Raw host receipts, native traces, original diagnoses/plans, policy decisions and
budget snapshots are retained. The exporter rechecks independent grades, trace
hashes, original diagnoses and agreement between host and native policy evidence.
Partial exports preserve original observations; they explicitly mark unavailable
native study links. Unexpected attempt directories fail review rather than being
silently discarded.

These count caps are explicit safety policies, not implementations of a saved
model-routing, caching or batching recommendation. The native bundle therefore
has an empty intervention-ledger list with an explicit scope explanation.
Creating a recommendation-based ledger entry would invent estimator attribution.
The existing native study and ablation contracts are reused; the earlier #181
study separately retains its 36 recommendation-based intervention records.

This small, single-host study is exploratory. Counterbalanced order reduces one
source of bias but does not remove model nondeterminism, shared-host effects or
task selection limits. A nonbinding cap preserving sampled quality does not prove
it will preserve quality on tasks needing additional calls. No result authorizes
activation on a new workload.

## Reproduction

Verify and unpack without inference:

```bash
python examples/real_agent_study/archive.py research/harness-ablation-2026-09/index.json --out /tmp/agentloop-harness-evidence
```

With the core source/version recorded in the plan and the included study code,
regenerate the report from stored outcomes:

```bash
python -I examples/real_harness_study/export.py --plan /tmp/agentloop-harness-evidence/study/plan.json --out /tmp/agentloop-harness-recomputed
```

The code and data must match the plan's hashes. The bundle contains reproduction
instructions, model identity and server configuration. No model is executed by
the exporter or archive reader.

Study code is in [the example package](../examples/real_harness_study/).
`protocol.py` freezes the design and independent reference SQL; `runner.py`
executes only with `--execute` against a verified loopback model artifact;
`export.py` validates saved evidence and calls the existing native reporters.
The required CI tests use offline model responses and exercise real harness
wrappers; required CI never downloads a model or performs inference.

Use the core/source versions and pinned model/runtime from the retained plan,
not an arbitrary installed version. Changing any frozen code, label, model or
configuration requires a new protocol and renewed evaluation. The same
[dataset attribution and model provenance](REAL_AGENT_STUDY.md#sources-and-permissions)
apply: UCI Wine Quality is CC BY 4.0, study code is Apache-2.0, and model weights
and runtime binaries are not redistributed.
