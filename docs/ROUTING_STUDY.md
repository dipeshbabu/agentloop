# Capability-checked model-routing study

This study measures one manually configured route through the
[routing adapter](MODEL_ROUTING.md). Both conditions use the same enforced
wrapper, capability checks and independent numeric-answer scorer. The original
condition selects Qwen3-4B; the candidate selects Qwen2.5-1.5B. No model is selected
from token counts, no semantic fallback runs, and no production route is changed.

The route fails the quality requirement. Across six held-out tasks repeated twice,
the original model answers 6/12 correctly and the candidate answers 0/12.
All six losses are retained. Faster incorrect or malformed answers are not
quality-preserving optimizations.

## Results

All 32 planned trials are recorded: eight pilot and 24 held-out trials. Eighteen
return a valid bounded numeric answer, while 14 fail output handling: two JSON
decode failures and twelve invalid answer values/types. Valid numeric output
alone does not imply the answer is correct. All 32 tokenizer counts match actual
provider prompt usage. All operating costs remain unknown; paid-provider spend
is zero.

| Held-out condition | Correct | Valid numeric output | Mean tokens | Mean measured decision time |
| --- | --- | --- | --- | --- |
| Original 4B model | 6/12 | 10/12 | 108 | 992.489 ms |
| Routed 1.5B model | 0/12 | 4/12 | 100 | 317.129 ms |

The descriptive latency difference is -675.360 ms, with a task-bootstrap interval
of [-1,275.991, -329.760] ms. There are no pairs where both outputs meet the
independent criterion, so no quality-preserving latency estimate is available.
These are small, fixed-order, single-host observations, not a causal estimate or
a general comparison of model families.

The timed decision includes capability tokenization, dispatch, usage inspection,
budget hooks and answer normalization. Mean tokenization time is 19.728 ms for
the original and 9.274 ms for the candidate; corresponding inference-call means
are 969.237 and 305.044 ms. Native trace runtime additionally includes per-task
client/harness setup. Model loading and fixed readiness requests are excluded
and retained separately.

## Frozen protocol

Eight new GSM8K tasks were selected by fixed hash ordering, excluding task indices
used in the earlier agent and calibration studies. Two are pilot tasks and six
are held out. Source bytes, labels, model artifacts, prompts, scorer version,
capabilities, budgets and execution-code hashes were frozen before any task run.
Pilot outcomes did not change the route or the held-out protocol.

Each model receives the same text request with a 64-token output bound, temperature
zero, recorded seed, JSON-object mode and disabled prompt reuse. Quality requires
an `answer` field containing a bounded plain decimal string that matches the
independent dataset label after the declared normalization. Numeric JSON values,
explanations, missing fields and truncated output do not pass that contract.
This is a direct-answer decision task, not a new autonomous-agent benchmark.

The local adapter applies the loaded model's chat template and tokenizer before
dispatch, then compares that count with reported usage afterward. These endpoints
perform formatting/tokenization rather than inference, as specified in the
[pinned server documentation](https://github.com/ggml-org/llama.cpp/blob/b10964/tools/server/README.md).
Each block verifies the server's loaded model path and model-file SHA-256.
Provider-reported aliases and selected-model identities are recorded separately.

One model call and a 4,160-token reservation are permitted per task. Both conditions
use the same checks; baseline configuration selects the original model. This study
configures no failure fallback. SDK-shaped offline tests separately cover rate
limits, failed fallback, unsupported capabilities, budget exhaustion, streamed
partial output, cancellation and charged stream-close errors.

For each repetition, all baseline predictions are archived before any candidate
run. The original single-call routing findings feed the existing intervention
ledger rather than being regenerated after outcomes. This fixed ordering leaves
time/order confounding; repeated seeds are not independent tasks. A fresh run,
request and usage collector are created per trial. Neither model's weights nor
prompt/schema settings are changed after seeing held-out answers.

## Retained evidence and reproduction

The [evidence index](../research/model-routing-2026-09/index.json) contains every
trial, failed answer, trace, capability result, budget decision, original prediction,
intervention and native paired study. Missing or cancelled trials remain explicit
in planned denominators. The exporter independently regrades original answers,
checks native/host evidence agreement and verifies prediction chronology.

An initial driver launch stalled before any task or warmup ran because a nested
process wrapper waited for the detached server. Its server was stopped, the
PowerShell launch corrected, and the unchanged frozen protocol executed. No task
outcome was discarded. Setup tokenizer probes and fixed readiness responses are
separate from the 32 measured trials.

```bash
python examples/real_agent_study/archive.py research/model-routing-2026-09/index.json --out /tmp/routing-evidence
python -m examples.routing_study.export --plan /tmp/routing-evidence/study/plan.json --out /tmp/routing-report
```

Use the complete core checkout at `bb027c2824cdd5a43ffbb95ecfac5d09df2e61b7`
with the archived source overlay. The reader/exporter performs no inference.
Changed code, data, models, prompts or routing settings require a new protocol.
The example's `runner.py` requires explicit `--execute` and a verified loopback
model; required CI uses offline fixtures and never downloads or executes a model.

GSM8K is MIT-licensed and its license and pinned source are retained. Study code
is Apache-2.0. Model weights and inference binaries are not redistributed. Public
benchmark questions may overlap model training; these results do not establish
production quality, universal capability or a safe deployment decision.
