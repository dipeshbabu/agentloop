# Optimization benchmark, November 2026

The evidence for the [optimization benchmark](../../docs/OPTIMIZATION_BENCHMARK.md)
and its first-use pilot ([#406](https://github.com/dipeshbabu/agentloop/issues/406)).

| File | Contents |
| --- | --- |
| `protocol.json` | Frozen before any run: seed, task IDs per split, source, model and code SHA-256, baseline settings, the finding-to-change mapping, the manual method |
| `dev-baseline.json` | Baseline runs on the 10 development tasks per agent |
| `search-{math,extraction,coding}.json` | Full optimization search reports, with every attempt kept |
| `derivation.json` | The findings seen, and the methods chosen from them |
| `held-out.json` | All 400 held-out runs: quality, runtime, calls, tokens and status |
| `results.json` | `compare_methods` output and per-workload summaries |
| `traces.zip` | All 472 native traces: development, search and held-out |
| `pilot/` | The pilot agent before and after the change, its traces and analysis (`pilot/traces`), and the pilot record |

The dataset text (GSM8K, HumanEval, SQuAD v2) isn't included. `protocol.json`
records the SHA-256 of each source file, so `run.py freeze` rebuilds the same
tasks. No trace contains model inputs or outputs.

`tests/test_optimization_benchmark.py` recomputes `results.json` from
`held-out.json`. It also checks that the development and held-out tasks don't
overlap, and that the pilot record is valid.
