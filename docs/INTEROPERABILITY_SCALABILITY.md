# Synthetic interoperability scalability measurements

These measurements exercise local AgentLoop import/export/report overhead on
owned generated source data. They establish bounded observed behavior for the
tested sizes, reproducibility and inventory coverage. They do not establish
agent task performance, provider benefit, instrumentation overhead or a causal
speedup from the implementation changes.

The [raw measurements](research/interoperability-scalability-20261009.json)
include applied limits, module byte hashes, import/export/analysis CPU and wall
time, Python allocation peaks and repeated-input fingerprints for every case.
The prior installed wheel was built from
`560191f000f0e41b2f5455b2e0910cb2c1b2cb24`; candidate measurements used the working
implementation's recorded module bytes. Runtime logic was frozen during the
candidate measurement. Python was CPython 3.13.12 on Windows.

## Method and scope

The checked-in [generator](../examples/interoperability_scalability.py) creates
10, 100 and 1000 completed Harbor trials, separate Omnigent sessions and model
spans within one OTLP trace. Harbor cases retain incorrect scores, timed-out
executions, missing trajectories and one planned unidentified attempt. At size
1000, all 1000 trials remain in the inventory: 100 have missing trajectories,
143 have failed score predicates, and the planned denominator is 1001.

Each environment measures the same immutable source directory sequentially.
`time.perf_counter` measures wall time, `time.process_time` CPU time, and
`tracemalloc` Python allocation peaks. Input generation, interpreter startup,
repeat-import fingerprinting and repeat-export checks are outside phase timing.
Lazy initialization may occur in the first API call. Later phases start allocation
tracing afresh, excluding objects retained from import. These peaks are not RSS or
a full-process/pipeline memory bound. Small Windows CPU samples may round to zero.

There is one sample per size/environment, on a shared host. Source filesystem
caches, competing CPU load, measurement overhead and phase/order effects were
uncontrolled. The unchanged OTLP importer also measured different times across
environments, demonstrating why these numbers cannot isolate a code-change effect.
Tracemalloc itself affects absolute performance. No production throughput promise
or statistical interval is inferred from these samples.

## Observed import results

Seconds are wall/CPU; peaks are MiB of Python allocations during that import phase.

| Source | Size | Prior wall / CPU | Candidate wall / CPU | Prior peak | Candidate peak |
| --- | ---: | ---: | ---: | ---: | ---: |
| Harbor job | 10 | 1.576 / 1.172 | 1.071 / 1.016 | 8.31 | 8.31 |
| Harbor job | 100 | 13.132 / 9.781 | 17.835 / 11.344 | 10.83 | 10.89 |
| Harbor job | 1000 | 301.044 / 152.875 | 162.398 / 112.109 | 37.63 | 38.24 |
| Omnigent sessions | 10 | 0.310 / 0.266 | 0.437 / 0.344 | 0.30 | 0.30 |
| Omnigent sessions | 100 | 8.682 / 6.391 | 4.219 / 3.594 | 2.78 | 2.79 |
| Omnigent sessions | 1000 | 58.551 / 40.453 | 16.904 / 16.219 | 27.40 | 27.43 |
| OTLP model spans | 10 | 0.087 / 0.078 | 0.305 / 0.109 | 0.10 | 0.10 |
| OTLP model spans | 100 | 1.210 / 1.125 | 0.780 / 0.750 | 0.73 | 0.72 |
| OTLP model spans | 1000 | 9.942 / 8.281 | 4.168 / 3.938 | 6.73 | 6.73 |

At size 1000, candidate export wall times were 51.001 seconds for Harbor,
14.133 seconds for Omnigent and 0.526 seconds for the single OTLP trace. Their
additional allocation peaks were 30.26, 9.93 and 4.36 MiB. Reporting the large
trace's 1000 recorded model blocks took 0.174 seconds wall / 0.172 seconds CPU,
with a 1.37 MiB additional allocation peak. Raw data includes all smaller cases
and prior export/report measurements.

All nine baseline/candidate imported evidence snapshots match exactly. Every
within-environment repeated import matched its canonical receipt/native/inventory
fingerprint; every repeat export was idempotent. Candidate Harbor export additionally
includes the streamed `harbor-trials.jsonl` inventory. Thus export timing scopes
include that additional artifact and are not identical across revisions.

## Bounded implementation and reproduction

Directory-prefix hash indexes replace repeated scans of previously loaded Harbor
artifacts. A session-membership set replaces repeated searches across Omnigent
groups. Those changes preserve imported artifacts and avoid those two scans growing
quadratically with job/session count. Excluded dependency/build trees are pruned
before repository Markdown traversal; eligible links and failure diagnostics stay
unchanged. The full local repository check completed in 4.72 seconds in a separate
measurement. No prior traversal timing was recorded, so no speedup ratio is claimed.

Core limits remain unchanged. Large generated cases explicitly set
`max_trajectories=max(256, 2*N)` and `max_references=max(1024, 8*N)`; every other
limit remains at its recorded default. Imports retain bounded correlated native
objects; JSONL record handling, streamed inventory export and artifact comparisons
avoid unnecessary whole-file output buffers. These results do not promise the same
behavior at untested counts or larger per-trial/record payloads.

```bash
# Keep larger generated inputs/artifacts outside the repository when practical.
uv run --frozen python examples/interoperability_scalability.py --generate-only --inputs /tmp/agentloop-scalability/inputs --sizes 10 100 1000
uv run --frozen python examples/interoperability_scalability.py --measure-only --inputs /tmp/agentloop-scalability/inputs --out /tmp/agentloop-scalability/candidate --sizes 10 100 1000 --revision working-tree
```

On Windows, use an explicit temporary directory in place of `/tmp`. To compare
revisions, run that same script with an isolated prior-wheel interpreter and a
different fresh `--out`, pointing at the same inputs. The baseline here used
`python -I` in an isolated wheel environment; its packaged modules were verified
against the source bytes before measurement. Do not reuse a measurement output
directory for a new timed run: measured values differ and conflict protection
preserves the prior artifacts.

See [offline adoption](INTEROPERABILITY_READINESS.md) for the full evidence workflow,
privacy and source-version limits. Live models, providers, benchmark execution,
host billing, agent instrumentation controls and general vendor compatibility
remain untested.
