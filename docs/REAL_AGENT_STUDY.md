# Real-agent study: a faster routing candidate fails quality

The September 2026 exploratory study rejects replacing its baseline model with
the tested smaller model. Across 36 held-out pairs, baseline task success was
16/36 and candidate success was 12/36. The candidate finished sooner, but that
did not establish an acceptable optimization. Twelve individual pairs passed
their configured native replay gates, with cost marked indeterminate and
non-gating. The full study fails its stricter quality-preservation criterion;
none of those pair passes establishes cost savings.

This study uses actual local model inference, the published
`agentloop-profiler==0.7.0`, real `langgraph==1.2.11`, and application-owned Python
agents. It is not production traffic or evidence of universal recommendation
precision. The repository owner delegated workload/scorer selection; public
source licenses permit the retained data. No external provider was charged.

## Tasks, intervention and result

Each workload has two pilot tasks and six distinct evaluation tasks, each
evaluated twice. Repetitions remain within tasks for uncertainty estimates.
The candidate changes every model decision from Qwen3-4B-Instruct-2507 Q4_K_M
to Qwen2.5-1.5B-Instruct Q4_K_M. Prompts, tools, bounds and scorers stay fixed.

| Workload | Execution and independent criterion | Baseline successes | Candidate successes | Mean baseline / candidate task time |
| --- | --- | --- | --- | --- |
| Repository questions | Real LangGraph model/tool loop over AgentLoop 0.7 source; correct file, symbol and an exact quoted source line inside that symbol, with tool use | 0/12 | 0/12 | 6.763 / 1.696 s |
| Wine data analysis | Custom Python SQL agent; read-only queries over the real UCI measurements, compared with independently specified SQL results (numeric tolerance 1e-6) | 12/12 | 8/12 | 4.246 / 1.829 s |
| Math word problems | Custom Python calculator agent; reference GSM8K numeric answer and matching successful calculator output | 4/12 | 4/12 | 3.367 / 1.493 s |

The equal aggregate math scores hide two lost successful attempts and two newly
successful attempts. SQL loses four successes. The baseline repository agent
reaches its four-call limit on all evaluation attempts; the candidate returns
answers sooner, but none meets the source-evidence criterion. Faster failure is
retained as failure. The study does not recommend deploying either repository
agent or adopting the routing change.

All 72 held-out attempts and all 36 pairs are present. No held-out attempt timed
out or was unmatched. There are 12 baseline call-limit failures and additional
completed-but-incorrect answers. Operating cost is unknown in all 72 traces;
token counts come from the local server's actual usage responses. Native retry
spans are zero: the application uses a bounded tool loop rather than explicit
retry spans. Tool-error/recovery turns remain visible in the tool histories.

The [evidence index](../research/real-agent-2026-09/index.json) identifies every
checksummed archive part. The bundle includes native traces, original diagnoses
and optimization plans, 36 intervention records, native paired studies, replay
reports, HTML exports, source data, protocols, dependency versions and the exact
study implementation. It also retains pilot and onboarding failures.

## Protocol and selection

Before inference, task sources and labels were pinned and a pilot task pool was
written. The first pilot exposed an ambiguous action/output contract: six
baseline attempts failed, and the six candidate pilot slots were not executed.
Those six unmatched cases, the retired protocol and its original implementation
are retained under `onboarding-v1/`. They are not silently replaced by later runs.

An explicit per-workload JSON response schema repaired the application contract.
The revised pilot passed 4/6 baseline and 3/6 candidate tasks. No held-out task
was used for this repair. The fixed resource rule retained all six evaluation
tasks per workload because every completed pilot finished within 60 seconds.
The pilot range and every original attempt remain available; this small resource-
limited sample is exploratory, not a powered production benchmark.

The routing family was selected from archived baseline pilot findings, without
requiring favorable pilot outcomes. Each evaluation repetition runs its baseline
block first, immediately exporting original predictions, and only then allows
its candidate block. Task order is fixed by a hash of task ID and repetition.
Baseline predictions are read from those saved artifacts when creating native
intervention records; they are not regenerated after seeing candidate results.

Other findings are retained as rejected for this experiment. In particular,
batching repeated model decisions is invalid for this tool loop: each later
decision consumes earlier tool results. This is a workload-specific false-positive
review, not a population precision estimate. Routing findings can cover only a
subset of model calls while the replacement affects all calls; this scope limit
must accompany any later savings-calibration analysis.

Native study reports retain every repetition. Supplemental intervals bootstrap
the mean paired latency difference for each independent task, using the existing
study bootstrap implementation, 1,000 resamples and seed 20260923. Fixed condition
order, public task selection, six independent tasks per workload and shared
hardware prevent a causal or general performance claim. GSM8K may overlap model
training; the held-out split concerns this experiment's tuning, not model training.
Quality-preserving subsets in the JSON are post-selection diagnostics; the full
planned denominator governs the intervention decision.

## Runtime and accounting

Both conditions use the same Windows host, GTX 1650 with 4 GB VRAM, Vulkan
llama.cpp build `b10964` (`b29c606e2`), six CPU threads, one slot, 4,096-token
context, batch 256 and microbatch 128. A model is loaded alone. Every request
disables prompt caching and uses temperature zero, a recorded seed, a maximum
of 192 output tokens and explicit JSON output constraints. Seeds are not a
guarantee of identical results across hosts or builds.

The application caps each task at four model calls, a 180-second task deadline
checked between steps, and a 90-second request timeout bounded by remaining
task time. These are cooperative application bounds. Tool state resets for each
task; SQL denies writes, attachment and unneeded functions and bounds VM work
and returned rows. The calculator interprets a small numeric AST without `eval`.
Repository access is limited to the frozen in-memory source snapshot.

Model loading and tool/framework setup occur outside measured task execution;
setup time is retained separately. A fixed, separately retained readiness request
warms each evaluation block. Analysis and artifact export also occur after the
task timer. The bundle does not include model weights or runtime binaries; their
public download revisions and hashes are recorded. There is no provider billing
receipt or measured energy/hardware cost, so no price was invented.

Six matched baseline pilot executions with and without span recording are also
retained. Direct span construction/input-serialization time averaged 0.360 ms
per traced task. The mean wall-clock difference was 201.192 ms, including model
and warm-state variation. That difference is not a causal instrumentation overhead
estimate. Direct recording excludes context setup, final export and analysis.

## Reproduce the evidence without model calls

Use a fresh output directory:

```bash
python examples/real_agent_study/archive.py research/real-agent-2026-09/index.json --out /tmp/agentloop-real-evidence
```

The loader verifies every part and file hash, rejects duplicate/unsafe paths,
and bounds total expanded bytes. It does not execute code from an archive.

Create a separate environment with the retained `requirements-frozen.txt`, then
use that environment's Python to regenerate reports from the original outcomes:

```bash
path/to/study-python -I /tmp/agentloop-real-evidence/implementation/real_agent_study/results.py report --protocol /tmp/agentloop-real-evidence/study/evaluation-frozen.json --root /tmp/agentloop-real-evidence/study --out /tmp/agentloop-recomputed
```

This recomputes quality against the frozen labels, validates trace and prediction
hashes, and exports native intervention/study/replay/HTML artifacts. It makes no
model calls. Reports preserve missing/incomplete slots; they do not convert them
into successes or drop them from the planned denominator.

To execute new trials, use the included implementation and source preparation
code, the pinned model artifacts in the protocol, and an explicitly started
loopback server. The actual run command is recorded in the bundled reproduction
instructions. Inference requires `--execute` and refuses reused output slots.
Freeze a new protocol before changing code, tasks, model/configuration or scorer;
do not reuse this study's conclusions for those changes.

## Sources and permissions

- AgentLoop source: Apache-2.0, release commit
  `7eaeaa2a435c59f3f73e6dedd6b173d85b4d2d11`; the license is retained.
- [UCI Wine Quality](https://archive.ics.uci.edu/dataset/186/wine+quality):
  Cortez, Cerdeira, Almeida, Matos and Reis (2009),
  [DOI 10.24432/C56S3T](https://doi.org/10.24432/C56S3T), CC BY 4.0. Original CSVs
  are retained; the tool normalizes column names and adds a color column in memory.
- [GSM8K](https://github.com/openai/grade-school-math): MIT, copyright OpenAI 2021,
  revision `3101c7d5072418e28b9008a6636bde82a006892c`. The complete license and
  source task file are retained. Eight indices were selected by a fixed hash order.
- [Qwen baseline](https://huggingface.co/Qwen/Qwen3-4B-Instruct-2507) and
  [smaller candidate](https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF):
  Apache-2.0 model artifacts; exact quantization repositories, revisions and
  SHA-256 values are in the protocols. Weights and third-party runtime binaries
  are downloaded separately, not redistributed in the evidence archive.

No task outcomes or native traces are redacted. Absolute local installation
paths are omitted from the portable environment summary. Server console logs,
model binaries and weights are excluded; the relevant build, configuration,
model identities and measured usage are retained.
