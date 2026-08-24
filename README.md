# AgentLoop

[![CI](https://github.com/dipeshbabu/agentloop/actions/workflows/ci.yml/badge.svg)](https://github.com/dipeshbabu/agentloop/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/agentloop-profiler.svg?cacheSeconds=300)](https://pypi.org/project/agentloop-profiler/)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-3776AB.svg)](https://www.python.org/downloads/)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

**Find slow, expensive, and repetitive steps in AI agents, then verify that your fix actually works.**

AgentLoop is an open-source profiler, optimization, and intervention-evaluation layer for agentic systems. It traces model calls, tool calls, retries, tokens, context reuse, latency, errors, and execution structure. It turns those traces into evidence-backed findings and lets you compare a baseline with a changed agent under performance and quality gates.

AgentLoop is not memory for agents. It is performance engineering for agent loops.

It is local-first, works without a hosted account, and can sit around an existing custom agent or framework instead of replacing it.

## First useful result

Install the Python package:

```bash
python -m pip install agentloop-profiler
```

The distribution is `agentloop-profiler`; the import and CLI are `agentloop`:

```python
import agentloop
```

Verify the install and get a real finding without an account, API key, database, network call, or paid model API:

```bash
agentloop quickstart
```

`quickstart` writes a deterministic trace marked as synthetic and prints concrete bottlenecks. Then analyze any AgentLoop trace with one command:

```bash
agentloop analyze runs/agentloop_quickstart.json
agentloop analyze runs/my_agent.json --json-out runs/my_agent_analysis.json
```

See [First useful result](docs/FIRST_USE.md) for the full beginner path.

## What people use AgentLoop for

| Use case | What AgentLoop provides |
| --- | --- |
| Coding agents | Find repeated tool/file operations, retry loops, unnecessary model calls, context growth, and regressions after workflow changes. |
| Research agents | Measure retrieval, synthesis, tool, model, and retry behavior; compare sequential and parallel designs; verify quality after optimization. |
| Customer or operations agents | Compare routing, tool sequences, structured-output reliability, latency, retries, and model cost. |
| Multi-agent systems | Inspect execution structure, duplicated work, serial coordination, repeated handoffs, and recurring findings. |
| Platform teams | Import or export OpenTelemetry traces, persist findings in SQLite/Postgres, and gate agent changes in CI. |
| Researchers | Run paired agent interventions with trace-level experiment metadata and retain execution evidence alongside task evaluation. |

## The workflow

### 1. Trace an existing agent

For custom Python agents:

```python
import agentloop

@agentloop.trace_model(name="planner", model="gpt-4.1-mini")
def plan(question: str) -> str:
    return call_model(question)

@agentloop.trace_tool(name="web_search")
def search(query: str) -> list[str]:
    return search_web(query)

@agentloop.traceable(root=True, agent_name="research_agent")
def run_agent(question: str) -> str:
    plan_text = plan(question)
    results = search(question)
    return synthesize(plan_text, results)

run_agent("Compare three vector databases")
```

You can also use the lower-level context managers directly:

```python
from agentloop import trace_agent, trace_model_call, trace_tool_call

with trace_agent("research_agent") as trace:
    with trace_model_call("plan", model="gpt-4.1", input_tokens=1200, output_tokens=200):
        pass
    with trace_tool_call("search_web"):
        pass

trace.export_json("runs/research_agent.json")
```

### 2. Find the bottleneck

```bash
agentloop analyze runs/research_agent.json
```

Or use individual stages when you need more control:

```bash
agentloop report runs/research_agent.json
agentloop diagnose --path runs/research_agent.json --json-out runs/diagnosis.json
agentloop optimize --path runs/research_agent.json --json-out runs/optimization.json
agentloop patch --path runs/research_agent.json --repo . --json-out runs/patch_plan.json
```

AgentLoop currently detects patterns such as:

- independent tool calls that may be parallelized;
- repeated prompt/context prefixes;
- repeated model calls that may be batched;
- small model steps that may be routed to a cheaper model;
- retry loops that may benefit from structured outputs;
- oversized reasoning/context steps;
- runaway loops;
- tool oscillation.

Recommendations are hypotheses to test, not proof that the proposed rewrite is better.

### 3. Change the agent

Apply one focused intervention such as parallel retrieval, context compression, a schema validator, a loop guard, or model routing. AgentLoop's patch command produces dry-run plans; it does not silently modify source code.

### 4. Prove the change

```bash
agentloop replay \
  --baseline runs/baseline.json \
  --candidate runs/candidate.json \
  --min-latency-improvement-pct 10 \
  --max-cost-regression-pct 0
```

When faster or cheaper is not sufficient evidence, add task-grounded quality fixtures:

```bash
agentloop replay \
  --baseline runs/baseline.json \
  --candidate runs/candidate.json \
  --quality-fixtures evaluation/fixtures.json \
  --min-quality-score 0.9
```

Replay can compare runtime, cost, tokens, retries, model/tool calls, schema validity, and configured quality evidence.

For pull-request gating:

```bash
agentloop ci \
  --baseline artifacts/agentloop/baseline.json \
  --candidate artifacts/agentloop/candidate.json \
  --quality-fixtures evaluation/fixtures.json
```

## Framework integrations

AgentLoop includes instrumentation for existing agent stacks:

- OpenAI SDK
- OpenAI Agents SDK
- LangGraph
- CrewAI
- Vercel AI SDK telemetry
- OpenTelemetry GenAI-style traces
- custom Python agents through decorators and context managers

For copy-paste examples and streaming/cancellation semantics, see [Framework integrations](docs/INTEGRATIONS.md).

OpenAI example:

```python
from openai import OpenAI
from agentloop import trace_agent
from agentloop.integrations.openai import instrument_openai_client

client = instrument_openai_client(OpenAI())

with trace_agent("research_agent") as trace:
    client.responses.create(model="gpt-4.1-mini", input="Research three competitors.")

trace.export_json("runs/openai_agent.json")
```

## Research use

AgentLoop can be used as execution-level instrumentation and intervention evidence for agent-systems research.

Good fits include:

- ReAct versus planner-executor comparisons;
- single-agent versus multi-agent workflows;
- sequential versus parallel tool execution;
- reflection and retry strategies;
- context compression/caching;
- model routing;
- loop/tool-oscillation guardrails;
- execution signals associated with success or failure;
- quality, latency, cost, token, and reliability tradeoffs.

One trace should represent one task attempt under one condition. Use trace metadata for experiment, condition, task, dataset, seed, prompt/config version, model, and source commit. Run enough tasks or repetitions to characterize variability and aggregate results according to the study design.

AgentLoop deliberately does not choose a statistical test for a paper and does not replace model-training frameworks, mechanistic interpretability tooling, benchmark dataset management, or human evaluation systems.

See [Using AgentLoop for research](docs/RESEARCH.md). The repository also includes a deterministic offline paired-intervention example:

```bash
python examples/research_experiment_demo.py
```

## Measured evidence versus estimates

Keep these separate when using AgentLoop for engineering decisions or research.

**Measured** evidence includes observed runtime, tokens, model/tool/retry counts, status/errors, task quality, and complete/provider-reported cost.

**Estimated** evidence includes optimizer savings, rewrite recommendations, priority scores, and modeled value/pricing scenarios.

Optimization cards are useful hypotheses. A proposed saving is not an experimental result until the change is implemented and measured on a candidate run.

Savings aggregation also records whether compatible-card selection was proven exact or used the documented bounded approximation. See [Savings selection accuracy](docs/SAVINGS_SELECTION.md).

## Quality gates

Quality fixtures support dependency-free scorers:

- `exact_match`
- `contains`
- bounded `glob`
- `required_fields` / `json_schema`
- `json_subset`
- trusted local `custom` scorers with `module:function`

Use them with `agentloop quality-report`, `agentloop replay`, or `agentloop ci`. Invalid suites fail closed. Custom Python scorers are for trusted local fixture files and are rejected by the HTTP quality endpoint.

## Trace and data compatibility

AgentLoop's native trace JSON is a public versioned compatibility surface used by the CLI, API, stores, and telemetry adapters. Custom metadata is preserved through native serialization and supported OpenTelemetry round trips.

See [Native trace schema and compatibility](docs/TRACE_SCHEMA.md).

Timing note: `total_runtime_ms` is end-to-end elapsed duration. `cumulative_span_time_ms` sums instrumented spans and can exceed elapsed time when spans overlap or nest.

Trace-derived values are treated as untrusted in AgentLoop Markdown exporters. Keep the downstream renderer's normal safe mode or HTML sanitizer enabled.

## Dashboard and persistence

Run the local dashboard:

```bash
python -m pip install "agentloop-profiler[dashboard]"
streamlit run dashboard/app.py
```

The dashboard covers stored traces, event timelines, optimization findings, patch plans, replay proof, quality gates, value reports, and setup guidance. See [Dashboard guide](docs/DASHBOARD.md).

AgentLoop uses SQLite by default and also supports Postgres for shared/self-hosted use. The HTTP API provides project-scoped trace storage, diagnosis, findings, optimization queues, quality reports, value reports, and usage summaries.

For deployment details, see [Production deployment](docs/PRODUCTION.md). For pricing-data semantics, see [Pricing guide](docs/PRICING.md).

## Advanced CLI

Useful commands include:

```text
quickstart
analyze
report
diagnose
optimize
patch
replay
quality-report
ci
value-report
export-otel
import-otel
init-store
store-trace
list-stored-traces
list-findings
optimization-queue
server
doctor
production-check
```

Run:

```bash
agentloop --help
```

for the current command surface.

## Standalone CLI

Tagged releases also include self-contained executables for Linux x86-64, Windows x86-64, macOS Intel, and macOS Apple silicon. These bundle Python and the core CLI runtime. Optional dashboard, server, Postgres, and Python-SDK integrations use the Python package.

Download the matching file and `SHA256SUMS` from the [GitHub Releases page](https://github.com/dipeshbabu/agentloop/releases).

Linux example, replacing `X.Y.Z` with the release version:

```bash
set -euo pipefail
version=X.Y.Z
asset="agentloop-v${version}-linux-x86_64"
curl -LO "https://github.com/dipeshbabu/agentloop/releases/download/v${version}/${asset}"
curl -LO "https://github.com/dipeshbabu/agentloop/releases/download/v${version}/SHA256SUMS"
mapfile -t checksum_lines < <(awk -v asset="$asset" '$2 == asset { print }' SHA256SUMS)
if [ "${#checksum_lines[@]}" -ne 1 ]; then
  echo "Expected exactly one checksum for ${asset}" >&2
  exit 1
fi
printf '%s\n' "${checksum_lines[0]}" | sha256sum --check --strict
chmod +x "$asset"
```

Windows PowerShell:

```powershell
$Version = "X.Y.Z"
$Asset = "agentloop-v$Version-windows-x86_64.exe"
Invoke-WebRequest "https://github.com/dipeshbabu/agentloop/releases/download/v$Version/$Asset" -OutFile agentloop.exe
Invoke-WebRequest "https://github.com/dipeshbabu/agentloop/releases/download/v$Version/SHA256SUMS" -OutFile SHA256SUMS
$ChecksumLines = @(Get-Content SHA256SUMS | Where-Object {
    $_ -match "^[0-9a-fA-F]{64}  $([regex]::Escape($Asset))$"
})
if ($ChecksumLines.Count -ne 1) { throw "Expected exactly one checksum for $Asset" }
$ExpectedHash = ($ChecksumLines[0] -split "  ", 2)[0]
$ActualHash = (Get-FileHash .\agentloop.exe -Algorithm SHA256).Hash
if ($ActualHash -ne $ExpectedHash) { throw "Checksum verification failed for $Asset" }
.\agentloop.exe --help
```

Each standalone release includes platform-specific third-party notices. macOS executables are ad-hoc signed but not notarized, so local Gatekeeper policy may require explicit first-launch approval.

## Source checkout and contribution

For a source checkout:

```bash
git clone https://github.com/dipeshbabu/agentloop.git
cd agentloop
uv sync --locked --all-extras --no-dev
uv run agentloop quickstart
```

For development setup, architecture boundaries, validation commands, review expectations, security/privacy guidance, and contribution policy, see [CONTRIBUTING.md](CONTRIBUTING.md).

Project direction and non-goals are in [docs/ROADMAP.md](docs/ROADMAP.md).

## Project status

AgentLoop is under active pre-1.0 development. Public interfaces may evolve, with user-facing changes recorded in [CHANGELOG.md](CHANGELOG.md).

Core scope: trace agent execution, find evidence-backed optimization opportunities, and verify interventions against performance and quality evidence.

## Community

- Use the structured [issue forms](https://github.com/dipeshbabu/agentloop/issues/new/choose) for bugs, feature requests, and usage questions.
- Follow [SUPPORT.md](SUPPORT.md) for usage help.
- Follow [SECURITY.md](SECURITY.md) for private vulnerability reports.
- Project decisions follow [GOVERNANCE.md](GOVERNANCE.md).
- Participation is covered by the [Code of Conduct](CODE_OF_CONDUCT.md).
- Repository owners should complete the [open-source launch checklist](docs/OPEN_SOURCE_CHECKLIST.md) before announcing a public launch.

## License

Copyright 2026 Dipesh Tharu Mahato and AgentLoop contributors.

AgentLoop is licensed under the [Apache License 2.0](LICENSE). Dependencies retain their own terms; see [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md).

PyPI: https://pypi.org/project/agentloop-profiler/
