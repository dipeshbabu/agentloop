# AgentLoop

AgentLoop is a profiler and optimization layer for AI agent execution graphs. It traces model calls, tool calls, retries, token usage, repeated context, and latency bottlenecks, then turns the trace into concrete workflow rewrite recommendations, patch plans, and replay proof.

AgentLoop is not memory for agents. It is performance engineering for agent loops.

The product thesis is simple: agents do not execute like single prompt-response apps. They loop, call tools, retry, branch, summarize, and carry context. AgentLoop measures that loop, proposes targeted rewrites, and proves whether the rewrite paid off.

Trace in. Rewrite plan out. Replay proof in the PR.

## Install

```bash
pip install -e ".[all,dev]"
```

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

## Run the sellable demo

```bash
agentloop demo-all
agentloop compare
agentloop demo --kind proof
agentloop diagnose --path runs/agentloop_proof_baseline.json --out runs/diagnosis.md --json-out runs/diagnosis.json
agentloop patch --path runs/agentloop_proof_baseline.json --repo . --out runs/patch_plan.md --json-out runs/patch_plan.json
agentloop replay --baseline runs/research_agent_baseline.json --candidate runs/research_agent_optimized.json --out runs/replay_report.md --json-out runs/replay_report.json
agentloop quality-report examples/quality_fixtures.json --out runs/quality_report.md --json-out runs/quality_report.json --min-score 0.9
agentloop audit --out runs/audit.md
agentloop optimize --out runs/optimization_plan.md --json-out runs/optimization_plan.json
agentloop ci --out runs/agentloop_ci.md --json-out runs/agentloop_ci.json
agentloop value-report --out runs/value_report.json --runs-per-month 5000
agentloop init-store
agentloop store-trace runs/research_agent_baseline.json --project-id demo
streamlit run dashboard/app.py
```

The dashboard now works as a local-first SaaS control panel backed by the same persistent store used by the API server. It includes:

- project overview and usage metering
- stored traces
- trace event timelines
- graph-aware optimization plans
- machine-actionable diagnosis findings
- persistent optimization queue across stored traces
- trace-to-patch dry-run plans
- before/after replay proof and PR comment previews
- buyer-facing value and pricing reports
- API key creation
- trace ingest and upload flow
- local, hosted API, and Postgres setup commands

You can still run individual demos:

```bash
agentloop demo --kind baseline
agentloop demo --kind optimized
agentloop demo --kind langgraph
agentloop demo --kind proof
python examples/langgraph_auto_instrumentation_demo.py
```

`agentloop compare`, `agentloop audit`, `agentloop optimize`, and `agentloop value-report` auto-generate missing demo traces by default, so missing `runs/research_agent_baseline.json` should not block the local workflow.

## Replay gates

```bash
agentloop replay \
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
- `regex`
- `required_fields` / `json_schema`
- `json_subset`
- `custom` with `module:function`

Use fixtures with `agentloop quality-report`, `agentloop replay --quality-fixtures`,
or `agentloop ci --quality-fixtures`. This makes the PR proof show whether the
candidate is faster, cheaper, and still correct.

## Value reports

```bash
agentloop value-report runs/research_agent_baseline.json \
  --out runs/value_report.json \
  --runs-per-month 5000 \
  --engineer-hourly-rate-usd 150 \
  --incident-cost-usd 500
```

A value report converts trace data into buyer-facing ROI metrics:

- direct model-cost savings per month
- latency saved per run and per month
- engineering hours saved from removing repeated bottlenecks
- reliability risk score based on retries, context duplication, and optimization opportunities
- suggested pricing plan and value-to-price ratio for pilots
- a short sales summary for pilots and investor demos

The pricing recommendation is intentionally conservative. It maps measured monthly value into one of `free`, `pro`, `team`, `growth`, or `enterprise`, then shows the suggested monthly price and modeled value-to-price ratio. This is useful for sales calls because the buyer can see why a trace justifies a paid plan instead of only seeing raw latency metrics.

The Streamlit dashboard includes a dedicated `Value & Pricing` page. Select a stored trace, adjust pilot assumptions such as monthly run volume and engineering rate, then download the value report JSON for sales follow-up. The Optimization page also has an expandable value estimate so the same trace can move from technical bottleneck to buyer ROI without leaving the dashboard.

Hosted value reports are available through both API and CLI:

```bash
agentloop remote-value-report RUN_ID --api-url http://127.0.0.1:8000 --api-key al_xxx
GET /traces/{run_id}/value?runs_per_month=5000
```

## Optimization plans

```bash
agentloop optimize runs/research_agent_baseline.json --out runs/optimization_plan.md --json-out runs/optimization_plan.json
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
agentloop diagnose --path runs/research_agent_baseline.json --out runs/diagnosis.md --json-out runs/diagnosis.json
agentloop patch --path runs/research_agent_baseline.json --repo . --out runs/patch_plan.md --json-out runs/patch_plan.json
agentloop list-findings --project-id demo
agentloop optimization-queue --project-id demo --json-out runs/optimization_queue.json
agentloop github-issue-drafts --project-id demo --out runs/agentloop_issue_drafts.md
agentloop export-otel runs/research_agent_baseline.json runs/research_agent_baseline.otel.json
agentloop import-otel runs/research_agent_baseline.otel.json runs/imported_trace.json --name imported_agent
agentloop diagnose --path runs/research_agent_baseline.otel.json --otel --out runs/otel_diagnosis.md
```

`agentloop diagnose` turns optimization opportunities into machine-actionable findings:
stable finding IDs, affected spans, evidence rows, savings formulas, patchability, and
replay acceptance criteria. `export-otel` and `import-otel` let AgentLoop sit above
OpenTelemetry GenAI-style spans instead of requiring a proprietary trace source.
`agentloop patch --dry-run` turns supported findings into framework-aware patch
plans for parallel tool execution, context caching, structured-output repair,
batching, model routing, split/compress rewrites, runaway-loop guardrails, and
tool-oscillation guards without modifying source files.
When traces are stored through the local or hosted API, AgentLoop persists the
diagnosis findings and clusters them into an optimization queue ranked by
severity, frequency, patchability, and estimated savings.
`agentloop github-issue-drafts` turns the top patchable queue items into
GitHub-ready issue titles, labels, bodies, and acceptance criteria.

## CI and PR performance gates

```bash
agentloop ci \
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
agentloop ci --baseline artifacts/agentloop/baseline.json --candidate artifacts/agentloop/candidate.json
```

The workflow also supports manual inputs for baseline path, candidate path, and
gate thresholds. Use `examples/ci_pr_trace_demo.py` to generate a local trace
artifact pair and preview the PR report.

## Dashboard

```bash
streamlit run dashboard/app.py
```

Dashboard pages:

- `Overview`: usage, run count, runtime, cost, token volume, model/tool calls, retry count, recent runs, runtime chart
- `Traces`: stored trace table, event timeline, trace JSON download
- `Optimization Queue`: ranked recurring findings across stored traces
- `Optimization`: optimization cards, bottlenecks, parallelizable groups, value estimate, plan JSON download
- `Diagnosis`: evidence-backed findings with severity, affected spans, savings, and validation criteria
- `Patch Plan`: framework-aware dry-run rewrite plans tied to likely files and replay gates
- `Replay Proof`: before/after metrics, quality/schema gates, and PR comment preview
- `Quality Gates`: fixture scoring for exact match, regex, JSON fields, JSON subset, and custom scorers
- `Value & Pricing`: buyer-facing ROI, reliability risk, pricing recommendation, value report JSON download
- `API Keys`: project-scoped API key creation
- `Ingest`: generate demo traces, upload trace JSON, store traces under a project
- `Setup`: local, hosted API, and Postgres deployment commands

See `docs/SAAS_DASHBOARD.md` for the operating guide.

## Local API server

```bash
agentloop server --host 127.0.0.1 --port 8000
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

This is the hosted product path: SDK traces can be sent to an API, stored, metered, and turned into optimization plans and value reports.

For production deployment, use Postgres, API-key auth, an admin key for API-key creation, and the Docker/Compose scaffolding in `docs/PRODUCTION.md`.
For the first paid design-partner workflow, use the readiness checklist in `docs/PILOT.md`.

```bash
agentloop production-check
python scripts/smoke_api.py
```

## Hosted store and metering

AgentLoop now supports a local SQLite store and a Postgres store for hosted deployments.

SQLite is the default:

```bash
agentloop init-store
agentloop demo-all
agentloop store-trace runs/research_agent_baseline.json --project-id demo
agentloop list-stored-traces --project-id demo
agentloop usage-summary --project-id demo
```

Postgres mode:

```bash
pip install -e ".[server,postgres]"
export AGENTLOOP_STORE_BACKEND=postgres
export AGENTLOOP_DATABASE_URL=postgresql://agentloop:agentloop@localhost:5432/agentloop
agentloop init-store
agentloop create-api-key --project-id acme --name local-dev
agentloop server --host 0.0.0.0 --port 8000
```

Then upload traces with the project API key:

```bash
agentloop upload runs/research_agent_baseline.json --api-url http://127.0.0.1:8000 --api-key al_xxx
agentloop remote-usage --api-url http://127.0.0.1:8000 --api-key al_xxx
agentloop remote-value-report RUN_ID --api-url http://127.0.0.1:8000 --api-key al_xxx
```

### Optional single-secret API key protection

For simple local demos, you can still use one static key:

```bash
export AGENTLOOP_REQUIRE_API_KEY=true
export AGENTLOOP_API_KEY=dev-secret
agentloop server --host 127.0.0.1 --port 8000
```

Then upload with:

```bash
agentloop upload runs/research_agent_baseline.json --api-url http://127.0.0.1:8000 --api-key dev-secret
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
python examples/upload_trace_demo.py
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

## Product path

Profiler -> Execution graph -> Optimization cards -> Value report -> Hosted AgentLoop Cloud

The sellable wedge is cost and latency reduction for production agent loops. The first paid user should be a team already running multi-step agents where each run has measurable latency, tool-call, and token waste.

See `docs/PRODUCT.md` for the go-to-market wedge.
