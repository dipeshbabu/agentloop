# Improve an agent workflow

The `agentloop improve` commands connect task capture, baseline findings, a
developer-applied change, and a quality-checked before/after report. These commands
are available in current source and are **unreleased**.

Install the checkout in your AgentLoop environment with `python -m pip install .`.
Your agent runs in its own environment through an explicit command supplied to
`capture`. The command receives one JSON request on stdin per task attempt and
writes a native AgentLoop trace to the requested path.

## Define tasks and correctness first

Save a workload such as this in `tasks.json`. Choose expected values independently
of your agent's answers.

```json
{
  "schema_version": "1.0",
  "name": "Repository metadata questions",
  "repetitions": 2,
  "tasks": [
    {
      "id": "package-name",
      "input": {"question": "What is the distribution name?"},
      "expected": {"answer": "agentloop-profiler"},
      "scorer": {"type": "json_subset"}
    }
  ]
}
```

Supported scorers are `exact_match`, `contains`, `json_subset`, `required_fields`,
and bounded `glob`. Each planned baseline and candidate answer must pass its
check. Expected answers and scorer configuration stay out of the runner's stdin
request. The supported bound is 1,000 planned attempts with 1–20 repetitions.

## Adapt your agent

An adapter around your application can follow this pattern. Replace the
`my_agent` import with your implementation and retain your existing instrumented
model and tool calls.

```python
import json
import sys

import agentloop
from my_agent import answer

request = json.load(sys.stdin)
with agentloop.trace_agent("repository_agent") as trace:
    trace.metadata["output"] = answer(request["input"])
trace.export_json(request["trace_path"])
```

The request contains `task_id`, zero-based `repetition`, `input`, and an absolute
`trace_path`. The trace must have finalized timing. Save the final observed output
in `metadata.output` so the quality scorer can evaluate it. Existing native model
outputs are also supported through the usual quality-report lookup.

## Capture and review the baseline

```bash
agentloop improve capture --tasks tasks.json --out runs/baseline -- python your_agent.py
agentloop improve start --tasks tasks.json --baseline runs/baseline/capture.json --out runs/optimization
```

Read `runs/optimization/plan.md`. It shows the selected finding family, affected
spans, observed evidence, suggested changes, assumptions, and validation criteria.
The default focus is the first baseline finding family. Select another observed
family with `--focus cache_context`, for example.

`start` saves original diagnoses, selected finding IDs, trace snapshots, task
definitions, and gate settings in a content-hashed `session.json` before any
candidate capture. The default objective is latency. Use `--objective cost` to
require a measured cost improvement, or `--minimum-improvement 10` to require a
10% improvement in the selected objective.

## Apply a change and compare the same tasks

Edit your agent, then run it against the unchanged task set.

```bash
agentloop improve capture --tasks tasks.json --out runs/candidate -- python your_agent.py
agentloop improve finish --session runs/optimization/session.json --candidate runs/candidate/capture.json --change "Describe the change applied to the agent" --out runs/results
```

Open `runs/results/report.html`. The report shows per-attempt quality, latency,
token provenance, model/tool counts, available model-cost estimates, and native
replay gate decisions. JSON and Markdown accompany it. Pairs with matching frozen
findings also produce native intervention records. Available baseline and candidate
traces produce a paired study. Study views retain the original trace fingerprints.

Acceptance requires every planned pair to have completed execution, passing
baseline and candidate quality checks, complete analysis, passing native replay
gates, and a positive measured improvement in the selected objective. Unknown
cost stays unavailable. A latency comparison can pass with an indeterminate,
non-gating cost estimate. A required cost improvement fails when pricing or
usable usage is missing.

## Failures, evidence and execution

`capture` retains every planned attempt, including failed, timed-out, cancelled,
missing-trace and unstarted attempts. `finish` retains these cases in its
denominator. It returns exit code 1 when the change fails its checks and saves
the report for inspection. Interrupted or incomplete Hermes turns fail the
capture-integrity checks.

Each command writes to a fresh directory. Completed captures and frozen sessions
are kept for review. Trace hashes, task/input hashes, distinct run identities,
and candidate capture times are checked before comparison. Runner fingerprints
include the explicit argv and file-based script content hashes. Retain your
source revision, model configuration, and environment alongside these artifacts
for reproduction. Hashes establish consistency of supplied evidence.

Execution uses the explicit argv after `--` with shell expansion disabled.
Each attempt has a timeout, with an owned process group/tree terminated on timeout
or interruption. The runner inherits the caller's environment and performs its
ordinary application actions. JSON workload files contain task data and built-in
scorer configuration. Standard output and error are discarded during capture by
default. Pass `--show-runner-output` to stream them when diagnosing a failing runner.

Raw task inputs, expected answers, traces, and outputs are retained in the local
artifacts. Review them before sharing. HTML escapes supplied text and shows
metrics and decisions. A fixed-order comparison describes the supplied tasks and
run conditions. Repeat across independent tasks and inspect variability before
generalizing a result.

## Reproduce the Hermes example with local inference

The checked-in [runner](../examples/hermes_repository_runner.py) answers three
questions about this repository's Python metadata. The
[task set](../examples/hermes_repository_tasks.json) declares two repetitions and
independent JSON-subset checks. The baseline repairs a free-text response into the
required JSON contract through a second model turn. The candidate requests
constrained JSON from its first turn and uses the same quality checks. A
third condition deliberately asks for an incorrect answer.

On Linux or WSL, prepare the pinned Hermes v0.22.7 environment and export the
observer as described in the [runtime pilot](HERMES_RUNTIME_PILOT.md). Then run
the following from the AgentLoop checkout.

```bash
python scripts/prepare_hermes_model.py --out runs/hermes-local-model
python scripts/run_hermes_adoption.py --hermes-root /path/to/pinned/hermes --plugin-dir /path/to/exported/plugin --model-runtime runs/hermes-local-model/runtime.json --out runs/hermes-adoption
```

The preparation downloads checksum-verified llama.cpp `b10964` and the official
Qwen2.5-0.5B-Instruct Q4_K_M model from pinned URLs. Inference runs on CPU through
an owned loopback server. Profiles and provider settings are isolated per attempt.
The runner uses real Hermes dispatch and normalized model usage. Public repository
context is explicitly retained by this application example for repeated-context
analysis. This leaves the observer's normal prompt-privacy behavior intact.

`protocol.json` records artifact hashes, decoding settings, request counts, warm-up,
and fixed run order. Results measure the effect of requesting structured output
and avoiding the format-repair call. Self-hosted operating cost remains
unknown. This small controlled workload supplies a reproducible product example.

The Hermes CI job executes the same guided commands with actual local inference,
checks the incorrect-answer rejection, and uploads the complete reports and
evidence. The [original deterministic runtime pilot](HERMES_RUNTIME_PILOT.md)
continues to cover streaming, tools, retries, interruption, privacy, and delegation.
