# Optimization benchmark (November 2026)

Does AgentLoop help a developer make an agent faster without making it worse?
This benchmark tests that on three kinds of agent:
- whose answers are checked by public datasets, not by AgentLoop;
- against simpler methods;
- with a negative control.

It also records one first-use pilot. The evidence is in
[`research/optimization-benchmark-2026-11`](../research/optimization-benchmark-2026-11/README.md),
and the code is in [`examples/optimization_benchmark`](../examples/optimization_benchmark/run.py).

## Short answer

- **The top-ranked finding was harmful.** On all three agents, AgentLoop's
  top-ranked finding was "route to a smaller model". Applying it made each
  agent faster, but cut answer quality on 31 of 75 held-out tasks.
- **The new search stopped it.** The guarded
  [optimization search](OPTIMIZATION_SEARCH.md) detected that regression on
  development tasks and recommended no change, so it neither gained nor lost.
- **The real waste went unflagged.** Every agent had an extra verification
  call, and one had a redundant rephrase-and-retrieve step. AgentLoop flagged
  neither, because detecting them needs semantic evidence that plain traces
  don't carry.
- **Timing noise is large at this scale.** Runs with identical settings
  differed by up to 44% in median time. Quality, with deterministic decoding,
  was exactly reproducible.

## What was compared

| Workload | Agent | Independent labels | Tasks |
| --- | --- | --- | --- |
| Math | Calculator tool loop with a final verification call | GSM8K test answers (MIT) | 6 tuning, 4 validation, 25 held-out |
| Extraction | Rephrase, retrieve twice, answer, verify | SQuAD v2 dev answers (CC BY-SA 4.0), unanswerable questions included | 6 + 4 + 25 |
| Coding | Generate, self-test on the prompt's examples, repair up to 3 times, review | HumanEval hidden tests (MIT), run in an isolated child process | 6 + 4 + 25; the **unseen application** (see below) |
| Negative control | One model call; nothing for a configuration change to remove | SQuAD v2 dev | 25 held-out |

The agents are written the way developers commonly write them, and their
waste is typical. AgentLoop is installed as a library, and no agent was written
for it.

| Method | How it was chosen |
| --- | --- |
| `none` | The baseline configuration |
| `heuristic` | Apply the change mapped to AgentLoop's most frequent **top-ranked** finding on the development traces (existing ranking, no measurement) |
| `agentloop` | Run the [optimization search](OPTIMIZATION_SEARCH.md) over every mapped finding seen on development traces. It tunes on 6 tasks, checks 4 more, and allows no quality drop on any task. |
| `manual` | Switch to the smaller model, the common change a developer tries without a profiler |

The models were Qwen3-4B-Instruct-2507 Q4_K_M (large) and
Qwen2.5-1.5B-Instruct Q4_K_M (small), on local llama.cpp b10964 with deterministic
decoding.

Everything was **frozen before any run**: tasks, splits, baseline settings, the
mapping from finding type to configuration change, and the source, model and
code hashes. The coding agent's methods came from the same automatic procedure,
with no one inspecting its traces, so it stands in for an application the
methods were never tuned on.

## Held-out results

| Workload | Method | Mean quality | Median runtime | Tasks worse than `none` | Tasks better |
| --- | --- | --- | --- | --- | --- |
| Math | none | 0.64 | 8.4 s | | |
| | agentloop | 0.64 | 4.3 s* | 0 | 0 |
| | heuristic | **0.20** | 4.2 s | 11 | 0 |
| | manual | **0.20** | 0.9 s | 11 | 0 |
| Extraction | none | 0.61 | 5.2 s | | |
| | agentloop | 0.61 | 3.5 s* | 0 | 0 |
| | heuristic | **0.44** | 2.5 s | 11 | 6 |
| | manual | **0.44** | 1.3 s | 11 | 6 |
| Coding | none | 0.88 | 26.6 s | | |
| | agentloop | 0.88 | 22.9 s* | 0 | 0 |
| | heuristic | **0.52** | 6.0 s | 9 | 0 |
| | manual | **0.52** | 5.6 s | 9 | 0 |
| Negative control | none | 0.64 | 2.1 s | | |
| | agentloop | 0.64 | 1.3 s* | 0 | 0 |
| | heuristic | 0.64 | 1.3 s* | 0 | 0 |
| | manual | 0.46 | 3.5 s | 8 | 2 |

\* The settings were identical to `none`, because the search recommended no
change. The runtime differences are measurement noise, not gains. The
`heuristic` and `manual` methods used the same configuration on every agent
workload, so they also produced identical quality.

`compare_methods` flags the same problem: `agentloop` "improved" 13 of 25
negative controls while running exactly the baseline. **A runtime difference
under about 50% is not evidence at this sample size.** The quality numbers are
reliable: identical settings gave identical quality on all 100 held-out tasks.

### Follow-up: safer default ranking

Since this benchmark, the default ranking no longer puts "route to a smaller
model" first. Until a quality check for the smaller model is declared `passed`,
the finding ranks after every other finding, and its title and rewrite hint say
that a quality check is required first. See
[quality prerequisites](FINDING_RANKING.md#quality-prerequisites). The change was
not evaluated on these held-out tasks, because they informed it. On at least 7
of each agent's 10 development traces it was the only finding
([`derivation.json`](../research/optimization-benchmark-2026-11/derivation.json)),
so a method that takes the top finding would still reach it, now titled as
needing a quality check.

## Prediction, overhead and recall

- **Prediction error.** For "route to a smaller model", AgentLoop predicted a
  25% time saving. The measured median saving was 50% for math, 54% for
  extraction and 73% for coding. The estimate is labelled uncalibrated, and it
  was too low, while the change cost quality. This was recomputed from the saved
  development traces with
  [`predictions.py`](../examples/optimization_benchmark/predictions.py), because
  the frozen runner read the wrong field and recorded 0. That doesn't affect the
  methods, which were chosen without it.
- **Search overhead.** 18 attempts and 84 s for math, 12 attempts and 50 s for
  extraction, 12 attempts and 221 s for coding. That's on top of running the
  development baselines and diagnosing them: 80 s, 80 s and 359 s.
- **Recall.** No finding pointed at the verification calls or the rephrase
  retrieval. The rules that can see redundant or low-contribution steps need
  saved semantic judgments, and this benchmark had none.
  Since then, [content fingerprints](CONTENT_FINGERPRINTS.md) give two
  deterministic rules for these patterns when the application records them.
  They weren't part of this benchmark and weren't evaluated on its tasks.

## First-use pilot

One first-use pilot was run, by **an AI coding agent: the agent that built
AgentLoop**, at the maintainer's request. It shows the workflow end to end. It
isn't evidence of independent developer adoption, since the participant already
knew the documentation.

| Milestone | Minutes from start |
| --- | --- |
| Installed 0.2.10 from PyPI in a fresh environment and ran `quickstart` | 1.5 |
| First valid trace from an existing, uninstrumented QA agent, using three decorators | 2.7 |
| First useful finding: "Investigate repeated `lookup` tool calls" (low confidence) | 3.0 |
| Change accepted on 8 held-out SQuAD questions: F1 0.58 → 0.71, model calls 24 → 16 | 8.8 |

Recorded friction:
- **A setup pitfall created false positives.** The pilot recorded all eight
  questions in one trace, so two high-confidence "loop guardrail" findings
  fired. The batching suggestion was also wrong: the calls depend on each other.
- **The plain decorator records no token counts** for a local model.

The record is in
[`pilot/pilot-record.json`](../research/optimization-benchmark-2026-11/pilot/pilot-record.json),
in the [pilot record](ADOPTION_GAPS.md#recording-pilot-observations) format.

## Limits

- Three small agents, 25 held-out tasks each, one machine, one run per task.
- Local models only: no hosted-provider latency or cost. Every cost is unknown.
- The agents and the finding-to-change mapping were written by the same author
  as AgentLoop. The labels are independent; the agent designs are not.
- Timing comparisons are not reliable at this scale (see the negative control).
- HumanEval's hidden tests run generated code in a child process, with a time
  limit and an empty working directory. That isn't a security sandbox.

## Reproduce

The dataset text isn't redistributed. Download the three public sources and
check their SHA-256 against `protocol.json`. Start the two llama.cpp servers on
ports 8766 and 8767 with the recorded model files, then run:

```bash
python -m examples.optimization_benchmark.run freeze --sources SRC --out OUT --large-model LARGE.gguf --small-model SMALL.gguf
python -m examples.optimization_benchmark.run derive --out OUT
python -m examples.optimization_benchmark.run evaluate --out OUT
python -m examples.optimization_benchmark.run report --out OUT
```

The protocol's code hashes are of the files as checked out on Windows, with
CRLF line endings.
