# Three guided fixture agents: the candidate is rejected

On 2026-11-12 the [guided validation suite](GUIDED_VALIDATION.md) ran real local
Qwen2.5-1.5B-Instruct Q4_K_M inference through Windows llama.cpp b10964. The
tool-heavy workload executed a real LangGraph 1.2.11 StateGraph. Repository
navigation and retrieval used application-owned Python agents. These were small
owned fixtures, with no external users, paid provider calls or production traffic.
AgentLoop was the source preparation stack before its version bump; its 0.1.1
development metadata does not identify the published 0.1.1 wheel. The protocol
records the actual source hashes used for these guided commands.

The proposed intervention reused immutable tool results within each request.
The baseline performed three identical reads; the candidate performed one.
Model, prompt, tasks and independent JSON-subset criteria were unchanged.
The candidate did not pass the full correctness/performance requirements.

## Retained result

All 45 planned captured attempts completed: three pilot tasks and six distinct
held-out tasks repeated twice, under baseline, candidate and deliberately wrong
conditions. Completion does not mean correctness. None of the three pilot
baseline/candidate pairs passed its independent answer check. The unchanged
held-out protocol supplied further diagnostic evidence, not promotion approval.

| Held-out workload | Pairs | Baseline / candidate correct | Pairs passing every gate | Mean baseline / candidate task time |
| --- | --- | --- | --- | --- |
| Repository navigation | 4 | 4 / 4 | 1 | 1099.565 / 1317.944 ms |
| Retrieval | 4 | 2 / 2 | 2 | 2070.273 / 1599.585 ms |
| LangGraph quoting workflow | 4 | 0 / 0 | 0 | 1386.707 / 1364.304 ms |

The means include incorrect attempts and are descriptive. Baseline and candidate
each passed correctness on 6/12 held-out attempts, and only 3/12 pairs passed
every gate. The overall candidate was rejected. All 15 deliberately wrong
controls were rejected. Pilot failures included choosing the wrong function,
returning a unit-bearing string instead of the requested numeric contract, and
an incorrect quote total. Their actual outputs remain in the traces.

Held-out baseline/candidate measurements each contain 12 actual model calls,
1,412 input tokens and 180 output tokens from server usage. Observed tool reads
fell from 36 to 12. Explicit retry spans were zero. These structural counts do
not establish an accepted improvement. Self-hosted operating cost is unknown;
no dollar saving is claimed.

Twelve additional traced/untraced pilot controls alternate order across two
repetitions, producing six pairs. Their outputs agreed, but all failed the
declared correctness criteria. Raw receipts/timings are retained; all six
qualified overhead differences and the overhead estimate remain unavailable.
There were 57 measured model requests plus one separately retained warm-up.
The owned local server was stopped after the experiment.

## Reproduce without inference

The [checksummed evidence index](../research/guided-2026-11/index.json) retains
300 files: every capture/attempt, original frozen diagnosis and plan, raw native
trace, quality/replay report, 30 linked intervention records, native study data,
12 overhead receipts, source implementation hashes, dependency versions, model
identity, runtime command/log and warm-up. No model weights or binaries are
included. The archive's original machine paths are provenance, not commands to
execute.

```bash
python examples/real_agent_study/archive.py research/guided-2026-11/index.json --out /fresh/guided-evidence
agentloop improve finish --session /fresh/guided-evidence/study/held_out/session/session.json --candidate /fresh/guided-evidence/study/held_out/candidate/capture.json --change "Recompute retained request-reuse comparison" --out /fresh/recomputed
```

`finish` returns exit code 1 for this rejected comparison. The archived evidence
was unpacked and recomputed through native finish with the same 3/12 passing
pairs and overall rejection, without new inference. Open the retained or
recomputed `report.html` for each gate and source trace.

## Limits and next step

The fixed baseline-first condition order, shared host, tiny fixtures, one model,
strict answer format and six held-out tasks limit interpretation. Timers exclude
worker imports, graph/model setup, export and analysis. The runtime requested
99 GPU layers, two CPU threads and a 4096-token context; decoding used seed
20261112, temperature zero, no prompt caching and 96 output tokens. The archive
records the exact artifact hashes and launch command.

Reuse was a developer-chosen alternative to the original repeated-tool
concurrency finding. This experiment does not calibrate that estimator or
establish a causal speed effect. Its useful outcome is a reproducible rejection
that preserves failed attempts and unknowns. Improve the agent's pilot answer
contract first, retire any task set used for tuning, then freeze a new held-out
set before evaluating another optimization. The earlier
[Hermes example](HERMES_OPTIMIZATION_CASE.md) remains a separate small successful
case; it does not erase this negative result.
