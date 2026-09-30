# Actual non-agent workload validation

actual learned-model execution on public benchmark data; research applications, not customer production traffic

Protocol: fd0956a9ff584b3c378f5a6b36a6865ec2093e1b3c0f4038340610d698e123cf

| Workload | Phase / candidate | Recorded / planned | Owner quality passes | Quality regressions | Baseline / candidate quality | Instrumented delta (ms) | Unrecorded delta (ms) |
| --- | --- | --- | --- | --- | --- | --- | --- |
| banknote | fit / batched | 16/16 | 16/16 | 0 | 1.0000 / 1.0000 | -0.9796 | -0.4444 |
| banknote | fit / cheap | 16/16 | 2/16 | 14 | 1.0000 / 0.7969 | -0.4025 | -0.5367 |
| banknote | held_out / batched | 64/64 | 64/64 | 0 | 1.0000 / 1.0000 | -1.1250 | -0.5706 |
| banknote | held_out / cheap | 64/64 | 12/64 | 52 | 1.0000 / 0.8086 | -0.6460 | -0.7058 |
| record-linkage | fit / batched | 16/16 | 16/16 | 0 | 1.0000 / 1.0000 | -2.0447 | -0.7536 |
| record-linkage | fit / cheap | 16/16 | 14/16 | 2 | 1.0000 / 0.9926 | -0.0011 | -0.0978 |
| record-linkage | held_out / batched | 64/64 | 64/64 | 0 | 1.0000 / 1.0000 | -2.5553 | -0.9626 |
| record-linkage | held_out / cheap | 64/64 | 58/64 | 6 | 1.0000 / 0.9924 | 0.1464 | 0.0106 |
| dry-bean | fit / batched | 16/16 | 12/16 | 0 | 0.9219 / 0.9219 | -0.8723 | -0.4041 |
| dry-bean | fit / cheap | 16/16 | 12/16 | 0 | 0.9219 / 0.9219 | -0.5329 | -0.6893 |
| dry-bean | held_out / batched | 64/64 | 48/64 | 0 | 0.8945 / 0.8945 | -1.4230 | -0.7250 |
| dry-bean | held_out / cheap | 64/64 | 46/64 | 6 | 0.8945 / 0.8984 | -1.9280 | -1.8681 |

Negative runtime deltas mean faster candidates. Quality passes require the predeclared dataset-label score floor and paired non-regression. A faster low-quality result is not an accepted optimization.

## Finding and estimator evidence

Selection inventory: {'selected': 972, 'rejected': 972}. Native finding and calibration artifacts retain selected and rejected findings; compatible isolated batching estimates are compared with original measured outcomes. Combined candidates remain unattributed and no fitted factor is installed.

## Scope and limits

- Small public-data research applications with fixed condition order and hardware. Learned confidence scores are heuristics, not calibrated probabilities. No operational authentication, identity linkage or agricultural decisions are deployed.
- Instrumented and recording-disabled timings are reported separately; observer overhead can dominate these short CPU calls.
- Task-cluster intervals describe the fixed public-data groups; repetitions are not independent tasks. No quality criterion was tuned on held-out outcomes.
- Current routing heuristics see unavailable token placeholders on non-language models; those findings require investigation, not automatic acceptance.
- Raw record identifiers are omitted. Hash references are not an anonymity guarantee. These applications use numeric comparison/features, not raw personal names, images or customer traffic.
