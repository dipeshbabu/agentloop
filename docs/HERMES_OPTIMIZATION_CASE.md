# A measured Hermes example

On 2026-11-12, the repository-question example ran through real Hermes v0.22.7
(`b200f5bc60e31588ba2cb60e288029c4a54ba5fa`) and local Qwen2.5-0.5B-Instruct
Q4_K_M inference. AgentLoop source was the implementation merged through
[PR #304](https://github.com/dipeshbabu/agentloop/pull/304).
No paid provider requests were made.

The baseline used a model turn to answer and a second turn to repair its format.
The proposed change requested JSON immediately and validated its value against
the authoritative repository TOML. Independent expected answers were frozen
outside the runner. This measured the combined application change.

| Comparison | Quality-checked pairs | Total model calls, baseline → candidate | Mean task latency, baseline → candidate | Decision |
| --- | --- | --- | --- | --- |
| Source-checked JSON candidate | 6/6 | 12 → 6 | 14.517s → 7.414s | Accepted for these tasks |
| Earlier JSON-only candidate | 5/6 | See retained per-pair measurements | See retained per-pair measurements | Rejected: incorrect answer |
| Deliberately incorrect control | 0/6 | See retained per-pair measurements | See retained per-pair measurements | Rejected |

The [measurement excerpt](evidence/hermes-2026-11-12.json) retains every pair's
run IDs, quality results, timings, calls, retries and token provenance, including
the earlier failed proposal. It records SHA-256 hashes of the original local
reports and the pinned runtime/model assets. It is historical evidence, not a CI
fixture or a replacement for raw traces. Model operating cost remains unknown.

Reproduce the workflow using the commands in
[guided optimization](GUIDED_OPTIMIZATION.md#reproduce-the-hermes-example-with-local-inference).
New runs must retain their own outcomes, including rejections. The local-model
CI lane is opt-in; deterministic runtime conformance remains part of ordinary CI.

These were three repository metadata questions with two repetitions on one host
and one small model, in fixed baseline-first order. The task timer excluded
Hermes startup. There was no instrumentation-overhead control. These results do
not establish performance on coding, research, production traffic or other
models. The earlier failure demonstrates why format validity alone is an
insufficient correctness check.

The source observer accepts Hermes >=0.21.6,<0.23. Contract coverage includes
0.21.6; real-host coverage is pinned to 0.22.7. Hermes 0.23.x support has not been
validated. Published AgentLoop 0.1.1 retains its narrower 0.21.x range. See
[runtime validation](HERMES_RUNTIME_PILOT.md) for the pinned host and scenarios.
