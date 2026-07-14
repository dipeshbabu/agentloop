# AgentLoop

[![CI](https://github.com/dipeshbabu/agentloop/actions/workflows/ci.yml/badge.svg)](https://github.com/dipeshbabu/agentloop/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-3776AB.svg)](https://www.python.org/downloads/)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

AgentLoop is a profiler and optimization layer for AI agent execution graphs. It traces model calls, tool calls, retries, token usage, repeated context, and latency bottlenecks, then turns the trace into concrete workflow rewrite recommendations, patch plans, and replay proof.

AgentLoop is not memory for agents. It is performance engineering for agent loops.

The design premise is simple: agents do not execute like single prompt-response
apps. They loop, call tools, retry, branch, summarize, and carry context.
AgentLoop measures that loop, proposes targeted rewrites, and proves whether the
rewrite improved the result.

Trace in. Rewrite plan out. Replay proof in the PR.

AgentLoop is under active pre-1.0 development. Public interfaces may evolve,
with user-facing changes recorded in the [changelog](CHANGELOG.md).

## Install from source

The `agentloop` name on PyPI currently belongs to an unrelated project. Until an
official distribution name is announced, do not use `pip install agentloop` for
this repository. Install the source checkout with [`uv`](https://docs.astral.sh/uv/):

```bash
git clone https://github.com/dipeshbabu/agentloop.git
cd agentloop
uv sync --locked --all-extras --no-dev
uv run agentloop --help
```

Shell examples below assume a source checkout and use `uv run` so they execute
inside the locked project environment. The prefix is optional only when that
environment is already active.

For a development installation, local quality checks, and pull request guidance,
see [CONTRIBUTING.md](CONTRIBUTING.md).

## Quickstart

```python
from agentloop import trace_agent, trace_model_call, trace_tool_call

with trace_agent("research_agent") as trace:
    with trace_model_call("plan", model="gpt-4.1", input_tokens=1200, output_tokens=200):
        pass
    with trace_tool_call("search_web"):
        pass

trace.print_report()
trace.export_json("runs/research_agent.json")
```

### Timing semantics

`total_runtime_ms` is the end-to-end elapsed duration of a trace, including work
that was not wrapped in an event. `cumulative_span_time_ms` is the sum of all
instrumented event durations, so it can exceed elapsed runtime when spans overlap
or are nested. Native trace JSON stores `ended_at` and the monotonic-clock
`elapsed_ms`. Timestamped imports use the earliest span start and latest span end.
Legacy traces without trustworthy end-time metadata fall back to their cumulative
event duration for compatibility.

### Markdown export safety

AgentLoop treats trace-derived values as untrusted in every Markdown exporter.
Names, findings, recommendations, statuses, paths, and other captured strings are
rendered as escaped single-line text in headings and prose, and table values cannot
add rows or columns. Inline and fenced code use delimiters longer than any backtick
run in their content. Raw HTML is escaped in prose and tables and remains literal
inside code spans and fences. Exporters do not turn trace values into Markdown links.

Keep the Markdown renderer's normal safe mode or HTML sanitizer enabled when reports
are converted to HTML. These guarantees protect AgentLoop's generated structure;
they do not make separately appended Markdown or renderer extensions trusted.

## Framework integrations

AgentLoop now includes dependency-free wrappers for the agent frameworks and SDKs teams are already using:

- OpenAI SDK: `instrument_openai_client(...)` records model latency and usage from `responses.create` and `chat.completions.create`.
- LangGraph: `instrument_state_graph(...)` and `trace_runnable(...)` trace graph nodes and compiled runnables.
- CrewAI: `instrument_crew(...)`, `instrument_agent(...)`, and `instrument_task(...)` trace crew, agent, and task execution methods.
- Vercel AI SDK / JS agents: export telemetry dictionaries and convert them with `trace_from_vercel_ai_events(...)`.

See `docs/INTEGRATIONS.md` for copy-paste examples.

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

## Run the end-to-end demo

```bash
uv run agentloop demo-all
uv run agentloop compare
uv run agentloop demo --kind proof
uv run agentloop diagnose --path runs/agentloop_proof_baseline.json --out runs/diagnosis.md --json-out runs/diagnosis.json
uv run agentloop patch --path runs/agentloop_proof_baseline.json --repo . --out runs/patch_plan.md --json-out runs/patch_plan.json
uv run agentloop replay --baseline runs/research_agent_baseline.json --candidate runs/research_agent_optimized.json --out runs/replay_report.md --json-out runs/replay_report.json
uv run agentloop quality-report examples/quality_fixtures.json --out runs/quality_report.md --json-out runs/quality_report.json --min-score 0.9
uv run agentloop audit --out runs/audit.md
uv run agentloop optimize --out runs/optimization_plan.md --json-out runs/optimization_plan.json
uv run agentloop ci --out runs/agentloop_ci.md --json-out runs/agentloop_ci.json
uv run agentloop value-report --out runs/value_report.json --runs-per-month 5000
uv run agentloop init-store
uv run agentloop store-trace --path runs/research_agent_baseline.json --project-id demo
uv run streamlit run dashboard/app.py
```

The dashboard is a local-first control panel backed by the same persistent store
used by the API server. It includes:

- project overview and usage metering
- stored traces
- trace event timelines
- graph-aware optimization plans
- machine-actionable diagnosis findings
- persistent optimization queue across stored traces
- trace-to-patch dry-run plans
- before/after replay proof and PR comment previews
- operational value and pricing-scenario reports
- API key creation
- trace ingest and upload flow
- local, HTTP API, and Postgres setup commands

You can still run individual demos:

```bash
uv run agentloop demo --kind baseline
uv run agentloop demo --kind optimized
uv run agentloop demo --kind langgraph
uv run agentloop demo --kind proof
uv run python examples/langgraph_auto_instrumentation_demo.py
```

`agentloop compare`, `agentloop audit`, `agentloop optimize`, and `agentloop value-report` auto-generate missing demo traces by default, so missing `runs/research_agent_baseline.json` should not block the local workflow.

## Replay gates

```bash
uv run agentloop replay \
  --baseline runs/research_agent_baseline.json \
  --candidate runs/research_agent_optimized.json \
  --out runs/replay_report.md \
  --json-out runs/replay_report.json \
  --min-latency-improvement-pct 20 \
  --min-cost-improvement-pct 5 \
  --max-latency-regression-pct 0 \
  --max-cost-regression-pct 0 \
  --require-retry-non-increase \
  --quality-fixtures examples/quality_fixtures.json \
  --min-quality-score 0.9
```

`agentloop replay` compares a baseline trace with a candidate trace and turns the
result into pass/fail gates for CI and PR comments. It reports latency, cost,
token, retry, tool-call, model-call, schema, and quality deltas, then exits
non-zero when gates fail unless `--no-fail-on-gate` is used.

## Quality gates

```json
{
  "fixtures": [
    {
      "id": "required_summary_fields",
      "input": "Summarize one source.",
      "baseline_output": {"summary": "AgentLoop proves rewrites.", "sources": ["source-a"]},
      "candidate_output": {"summary": "AgentLoop proves rewrites.", "sources": ["source-a"]},
      "scorer": {"type": "required_fields", "required": ["summary", "sources"]}
    }
  ]
}
```

Quality fixtures support dependency-free scorers:

- `exact_match`
- `contains`
- `glob` with a maximum 256-character pattern
- `required_fields` / `json_schema`
- `json_subset`
- `custom` with `module:function`

Use fixtures with `agentloop quality-report`, `agentloop replay --quality-fixtures`,
or `agentloop ci --quality-fixtures`. This makes the PR proof show whether the
candidate is faster, cheaper, and still correct.

Custom scorers import and execute Python code. Use them only with trusted local
fixture files; the HTTP `/quality-report` endpoint rejects custom scorers.
Raw regular-expression scorers are also rejected because caller-controlled
patterns can cause regular-expression denial of service. Use `glob` or
`contains` for untrusted pattern matching.

## Value reports

```bash
uv run agentloop value-report --path runs/research_agent_baseline.json \
  --out runs/value_report.json \
  --runs-per-month 5000 \
  --engineer-hourly-rate-usd 150 \
  --incident-cost-usd 500
```

A value report converts trace data into operational ROI metrics:

- direct model-cost savings per month
- latency saved per run and per month
- engineering hours saved from removing repeated bottlenecks
- reliability risk score based on retries, context duplication, and optimization opportunities
- optional pricing scenarios and modeled value-to-price ratios
- a concise summary of measured savings and reliability impact

The pricing scenario is intentionally conservative. It maps measured
monthly value into one of `free`, `pro`, `team`, `growth`, or `enterprise`, then
shows the suggested monthly price and modeled value-to-price ratio. Treat this
as a configurable planning estimate rather than a billing or procurement source
of truth.

The Streamlit dashboard includes a dedicated `Value & Pricing` page. Select a
stored trace, adjust assumptions such as monthly run volume and engineering
rate, then download the value report JSON for capacity planning or optimization
prioritization. The Optimization page also has an expandable value estimate so
the same trace can connect a technical bottleneck to its estimated operational
impact.

Remote value reports are available through both API and CLI:

```bash
uv run agentloop remote-value-report RUN_ID --api-url http://127.0.0.1:8000 --api-key al_xxx
curl --fail --header "X-AgentLoop-Key: al_xxx" \
  "http://127.0.0.1:8000/traces/RUN_ID/value?runs_per_month=5000"
```

## Optimization plans

```bash
uv run agentloop optimize --path runs/research_agent_baseline.json --out runs/optimization_plan.md --json-out runs/optimization_plan.json
```

AgentLoop reconstructs an execution graph, identifies bottlenecks, and emits optimization cards such as:

- parallelize independent tool calls
- cache repeated prompt/context prefixes
- batch repeated model calls
- route small steps to cheaper models
- reduce retry loops with structured outputs
- split or compress oversized reasoning steps

Each card includes a reason, affected nodes, confidence, rewrite hint, and estimated latency or cost savings.

## Diagnosis and OpenTelemetry interop

```bash
uv run agentloop diagnose --path runs/research_agent_baseline.json --out runs/diagnosis.md --json-out runs/diagnosis.json
uv run agentloop patch --path runs/research_agent_baseline.json --repo . --out runs/patch_plan.md --json-out runs/patch_plan.json
uv run agentloop list-findings --project-id demo
uv run agentloop optimization-queue --project-id demo --json-out runs/optimization_queue.json
uv run agentloop github-issue-drafts --project-id demo --out runs/agentloop_issue_drafts.md
uv run agentloop export-otel runs/research_agent_baseline.json runs/research_agent_baseline.otel.json
uv run agentloop import-otel runs/research_agent_baseline.otel.json runs/imported_trace.json --name imported_agent
uv run agentloop diagnose --path runs/research_agent_baseline.otel.json --otel --out runs/otel_diagnosis.md
```

`agentloop diagnose` turns optimization opportunities into machine-actionable findings:
stable finding IDs, affected spans, evidence rows, savings formulas, patchability, and
replay acceptance criteria. `export-otel` and `import-otel` let AgentLoop sit above
OpenTelemetry GenAI-style spans instead of requiring a proprietary trace source.
`agentloop patch --dry-run` turns supported findings into framework-aware patch
plans for parallel tool execution, context caching, structured-output repair,
batching, model routing, split/compress rewrites, runaway-loop guardrails, and
tool-oscillation guards without modifying source files.
When traces are stored through the local or deployed API, AgentLoop persists the
diagnosis findings and clusters them into an optimization queue ranked by
severity, frequency, patchability, and estimated savings.
`agentloop github-issue-drafts` turns the top patchable queue items into
GitHub-ready issue titles, labels, bodies, and acceptance criteria.

## CI and PR performance gates

```bash
uv run agentloop ci \
  --baseline runs/research_agent_baseline.json \
  --candidate runs/research_agent_optimized.json \
  --out runs/agentloop_ci.md \
  --json-out runs/agentloop_ci.json \
  --min-latency-improvement-pct 20 \
  --min-cost-improvement-pct 5 \
  --max-latency-regression-pct 0 \
  --max-cost-regression-pct 0 \
  --quality-fixtures examples/quality_fixtures.json \
  --min-quality-score 0.9
```

`agentloop ci` combines replay gates with candidate diagnosis findings. It emits
Markdown for PR summaries and JSON for automation, then exits non-zero when the
configured cost, latency, or retry gates fail. The included GitHub Actions
workflow runs this against the demo baseline and optimized traces and appends
the report to the workflow step summary and pull request comments.

For a real repository, have your agent test job write trace artifacts, then point
the workflow at those paths:

```bash
uv run agentloop ci --baseline artifacts/agentloop/baseline.json --candidate artifacts/agentloop/candidate.json
```

The workflow also supports manual inputs for baseline path, candidate path, and
gate thresholds. Use `examples/ci_pr_trace_demo.py` to generate a local trace
artifact pair and preview the PR report.

## Dashboard

```bash
uv run streamlit run dashboard/app.py
```

Dashboard pages:

- `Overview`: usage, run count, runtime, cost, token volume, model/tool calls, retry count, recent runs, runtime chart
- `Traces`: stored trace table, event timeline, trace JSON download
- `Optimization Queue`: ranked recurring findings across stored traces
- `Optimization`: optimization cards, bottlenecks, parallelizable groups, value estimate, plan JSON download
- `Diagnosis`: evidence-backed findings with severity, affected spans, savings, and validation criteria
- `Patch Plan`: framework-aware dry-run rewrite plans tied to likely files and replay gates
- `Replay Proof`: before/after metrics, quality/schema gates, and PR comment preview
- `Quality Gates`: fixture scoring for exact match, bounded glob matching, JSON fields, JSON subset, and custom scorers
- `Value & Pricing`: operational ROI, reliability risk, pricing scenarios, and value report JSON download
- `API Keys`: project-scoped API key creation
- `Ingest`: generate demo traces, upload trace JSON, store traces under a project
- `Setup`: local, HTTP API, and Postgres deployment commands

See the [dashboard guide](docs/DASHBOARD.md) for setup and operating details.

## Local API server

```bash
uv run agentloop server --host 127.0.0.1 --port 8000
```

Endpoints:

- `GET /health`
- `GET /readyz`
- `POST /api-keys`
- `POST /traces`
- `GET /traces`
- `GET /traces/{run_id}/report`
- `GET /traces/{run_id}/optimize`
- `GET /traces/{run_id}/diagnose`
- `GET /findings`
- `GET /optimization-queue`
- `GET /optimization-queue/github-issues`
- `POST /quality-report`
- `GET /traces/{run_id}/value`
- `GET /usage`

This is the HTTP API path: SDK traces can be sent to an API, stored, metered,
and turned into optimization plans and value reports.

For production deployment, use Postgres, API-key auth, an admin key for API-key
creation, and the Docker/Compose scaffolding in `docs/PRODUCTION.md`.

```bash
uv run agentloop production-check
uv run python scripts/smoke_api.py
```

## Persistent store and metering

AgentLoop supports a local SQLite store and a Postgres store for shared or
self-hosted deployments.

SQLite is the default:

```bash
uv run agentloop init-store
uv run agentloop demo-all
uv run agentloop store-trace --path runs/research_agent_baseline.json --project-id demo
uv run agentloop list-stored-traces --project-id demo
uv run agentloop usage-summary --project-id demo
```

Postgres mode:

```bash
uv sync --locked --extra server --extra postgres --no-dev
export AGENTLOOP_STORE_BACKEND=postgres
export AGENTLOOP_DATABASE_URL=postgresql://agentloop:agentloop@localhost:5432/agentloop
uv run agentloop init-store
uv run agentloop create-api-key --project-id acme --name local-dev
uv run agentloop server --host 0.0.0.0 --port 8000
```

Then upload traces with the project API key:

```bash
uv run agentloop upload --path runs/research_agent_baseline.json --api-url http://127.0.0.1:8000 --api-key al_xxx
uv run agentloop remote-usage --api-url http://127.0.0.1:8000 --api-key al_xxx
uv run agentloop remote-value-report RUN_ID --api-url http://127.0.0.1:8000 --api-key al_xxx
```

### Optional single-secret API key protection

For simple local demos, you can still use one static key:

```bash
export AGENTLOOP_REQUIRE_API_KEY=true
export AGENTLOOP_API_KEY=dev-secret
uv run agentloop server --host 127.0.0.1 --port 8000
```

Then upload with:

```bash
uv run agentloop upload --path runs/research_agent_baseline.json --api-url http://127.0.0.1:8000 --api-key dev-secret
```

### Upload traces from Python

```python
from agentloop import AgentLoopClient

client = AgentLoopClient(base_url="http://127.0.0.1:8000", api_key="al_xxx")
response = client.upload_trace("runs/research_agent_baseline.json")
plan = client.get_optimization_plan(response["run_id"])
value = client.get_value_report(response["run_id"], runs_per_month=5000)
usage = client.usage_summary()
```

Or use environment variables:

```bash
export AGENTLOOP_API_URL=http://127.0.0.1:8000
export AGENTLOOP_API_KEY=al_xxx
uv run python examples/upload_trace_demo.py
```

## LangGraph usage

### Decorate individual nodes

```python
from agentloop import trace_agent
from agentloop.integrations.langgraph import trace_node

@trace_node("retrieve_sources")
def retrieve_sources(state):
    return state

with trace_agent("research_graph") as trace:
    retrieve_sources({})

trace.export_json("runs/research_graph.json")
```

### Auto-instrument a StateGraph builder

```python
from agentloop.integrations.langgraph import instrument_state_graph, trace_runnable

builder = StateGraph(State)
instrument_state_graph(builder)

builder.add_node("retrieve", retrieve)
builder.add_node("synthesize", synthesize)

app = trace_runnable(builder.compile(), name="research_graph")
app.invoke({"question": "What is agent profiling?"})
app.export_last_trace("runs/research_graph.json")
```

This integration is dependency-free. AgentLoop does not import LangGraph directly; it wraps StateGraph-like builders and compiled runnable-style objects.

## Project direction

Profiler -> Execution graph -> Optimization findings -> Replay proof -> Reliable deployment

The core scope is cost, latency, and reliability engineering for multi-step
agent workflows. AgentLoop remains local-first and self-hostable while keeping
its trace, analysis, and replay formats useful in CI and deployed environments.

See the contributor-facing [roadmap](docs/ROADMAP.md) for current capabilities,
priorities, and non-goals.

## Community

- Read [CONTRIBUTING.md](CONTRIBUTING.md) before proposing or submitting a
  change.
- Use the structured [issue forms](https://github.com/dipeshbabu/agentloop/issues/new/choose)
  for bugs, feature requests, and usage questions.
- Follow [SUPPORT.md](SUPPORT.md) for usage help and
  [SECURITY.md](SECURITY.md) for private vulnerability reports.
- Project decisions follow [GOVERNANCE.md](GOVERNANCE.md), and all participation
  is covered by the [Code of Conduct](CODE_OF_CONDUCT.md).
- Repository owners should complete the
  [open-source launch checklist](docs/OPEN_SOURCE_CHECKLIST.md) before announcing
  the public launch.

## License

Copyright 2026 Dipesh Babu and AgentLoop contributors.

AgentLoop is licensed under the [Apache License 2.0](LICENSE). Dependencies keep
their own terms; see the [third-party software inventory](THIRD_PARTY_LICENSES.md).
