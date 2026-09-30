# Cross-workload evidence, September 2026

The [report](REPORT.md) and [JSON results](results.json) cover 222 fresh synthetic
reference comparisons, 42 archived real-agent comparisons, and six retired
onboarding baselines whose six candidate slots were not executed. All named
variants, failed controls, rejected findings and unknown operating costs remain
in the evidence. The [protocol](protocol.json) fixes tasks, splits, scorers,
versions and selection rules before the fresh reference run.

The current analyzer emits six batching false positives on the six independently
labeled repository-agent tasks. Routing cases remain ambiguous; their correctness
is not assumed from a faster candidate. The historical held-out candidate meets
quality criteria on 0/12 repository, 8/12 SQL and 4/12 math pairs, including four
SQL and two math quality regressions. Those are source-study results, not effects
credited to the current retrospective diagnoses.

Reference hybrid/optimized variants meet their quality gates on every reference
task in this run. Cheap and failing variants retain their quality losses and
execution failures. Synthetic timing, token and billing results stay separate
from actual local-model observations; neither group represents production traffic.

The full [checksummed archive](index.json) includes the source snapshots, licenses,
native traces, original predictions, selections, quality/replay/intervention
records, independent finding-label corpora, raw overhead measurements and reports.
It also contains the original real-agent/calibration evidence without rewriting
their predictions or outcomes. The benchmark's measured core is commit
`8af65da781029a581c6cf01cf8700cdea99da12c`; the exact benchmark code has its own
normalized source hashes in the protocol.

Offline reconstruction produces result hash
`36ccc38e730a87e3e2b406299ada01a3776d0e9e73286e39614786f193b0b702`.
The protocol hash is
`8b5ec58ad690cd8ecc046c234e858d4274e45d97feadfd54f625d7a543d888a7`.

```bash
python examples/real_agent_study/archive.py research/cross-workload-2026-09/index.json --out /tmp/usefulness-evidence
python -I /tmp/usefulness-evidence/implementation/examples/usefulness_benchmark/run.py report \
  /tmp/usefulness-evidence --json-out /tmp/usefulness-rebuilt.json --markdown-out /tmp/usefulness-rebuilt.md
```

Use an environment with the dependencies in the retained `pyproject.toml` and
`uv.lock`. Reporting executes no workload, model or judge. The archive loader
checks part/file hashes and path/expanded-size bounds. A packaging-only support
manifest binds the unchanged archive-loader modules to the pinned revision;
no measurements were rerun or replaced for that correction.

See the [methodology](../../docs/USEFULNESS_BENCHMARK.md) for task-cluster intervals,
partial instrumentation controls, preserved calibration attribution failures,
public-task limitations and untimed operator effort. Model weights/runtime
binaries are intentionally not redistributed. Original dataset/source licenses
and reproduction instructions remain in the historical source directories.
