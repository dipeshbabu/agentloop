# Validate the guided workflow across workloads

The source example `examples/guided_validation/` exercises repository navigation,
retrieval and a tool-heavy quoting workflow. It uses existing capture, frozen
findings, quality checks, replay, intervention records and native paired studies.
There is no separate experiment or acceptance engine.

## Offline contract checks

```bash
uv run --frozen python -m examples.guided_validation.run --out runs/guided-offline
```

This performs three pilot tasks, then freezes six distinct held-out inputs and
their independent labels. Held-out tasks run twice by default. Each phase saves
a baseline before its findings are frozen, a candidate, and a deliberately wrong
control. Every condition and rejection is retained. The default policy is
scripted and all of its traces are marked synthetic; its wall-clock values do
not establish AI-agent performance.

The repository agent parses small owned Python source; the retrieval agent
reads owned policy documents; the workflow reads an owned price catalog and
computes basket quotes. These tiny fixtures have no external-user or production
provenance. Labels are defined separately from the runner, never sent on stdin,
and checked through native JSON-subset quality gates.

The baseline fetches identical immutable data three times inside one request.
The candidate reuses the first result within that request and clears its cache
before every task. Model, prompt, data and scoring stay fixed. Repeated-tool
findings are retained before candidate execution. Reuse is a manually chosen
alternative to the native concurrency suggestion: these results must not be
used to calibrate the parallelization estimator as though that exact rewrite
had been applied.

## Real LangGraph execution

```bash
uv run --isolated --frozen --with langgraph==1.2.11 python -m examples.guided_validation.run --framework langgraph --out runs/guided-graph
```

This executes the tool-heavy workload through a real two-node StateGraph while
retaining the same tool and correctness contracts. The other workloads remain
application-owned Python. Ordinary CI runs the offline suite; the existing
pinned LangGraph integration lane verifies actual graph execution.

## Actual local model inference

The example reuses the existing real-agent study's bounded loopback model client
and model-identity verification. Supply the pinned
Qwen2.5-1.5B-Instruct Q4_K_M artifact declared in
[`MODELS`](../examples/real_agent_study/run.py), and explicitly start your own
llama.cpp server on `127.0.0.1`, with alias `local-study`, one parallel slot,
4,096-token context and prompt caching disabled. Read the
[earlier runtime/source record](REAL_AGENT_STUDY.md#runtime-and-accounting)
for artifact provenance. No model download or provider account is required by
the offline suite.

```bash
uv run --isolated --frozen --with langgraph==1.2.11 python -m examples.guided_validation.run --framework langgraph --model-url http://127.0.0.1:8766 --model-file /absolute/qwen2.5-1.5b-instruct-q4_k_m.gguf --out runs/guided-local-model
```

Only an explicit loopback HTTP endpoint is accepted. Before trials, its model
path and SHA-256 must match the pinned identity. These traces identify real local
model execution over owned fixtures. The server's actual usage is retained;
missing usage remains unavailable and self-hosted operating cost stays unknown.
The client caps requests at 96 output tokens and 60 seconds, and capture bounds
each worker at 90 seconds. Model failures produce failed traces and rejected
pairs. LangSmith tracing is disabled for this isolated example.

## Read the evidence

`protocol.json` records source hashes, package/environment identity, the proposed
intervention, run order, planned splits and overhead slots. A package version
from a development checkout does not identify a published wheel. Each split
retains its own native `capture.json`, frozen `session.json`, original diagnoses,
trace snapshots and candidate/wrong `*-results/report.{json,md,html}`. Native
intervention and study artifacts accompany each comparison. Token usage, calls,
retries, latency, available model cost and declared quality are in those reports.

`overhead/` retains twelve attempted controls: each of three pilot tasks runs
twice, once traced and once untraced, with the order reversed for the second
repetition. `summary.json` reports six paired differences using the native
summary helper, with output agreement and independent correctness. Tracing wall
time includes context/span recording and model variation; graph setup, final
export and analysis are excluded. These descriptive controls cannot isolate a
causal overhead estimate. Failed controls remain visible.

Use fresh output roots. An interrupted run retains planned slots, attempts and
receipts already written; it does not silently resume or overwrite them. A
candidate may be correct and still fail to improve the measured objective. The
runner retains that result and never requires a positive timing outcome for a
test to pass. The deliberately wrong candidate must always be rejected.

Fixed baseline-first condition order, one model/host, six held-out tasks and
simple public fixture logic limit generalization. A held-out split means unused
for tuning this example; it does not establish separation from model training.
Expand representative task coverage and freeze a new protocol before making a
decision about an actual developer's agent. See [remaining gaps](ADOPTION_GAPS.md).
