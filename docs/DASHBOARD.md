# Dashboard guide

AgentLoop includes a local-first Streamlit dashboard backed by the same trace
store used by the CLI and API server. It can run with local SQLite data or a
shared Postgres database.

## Set up

From a source checkout, install all optional features with uv:

```bash
uv sync --locked --all-extras --dev
uv run agentloop init-store
uv run agentloop demo-all
uv run agentloop store-trace --path runs/research_agent_baseline.json --project-id demo
uv run streamlit run dashboard/app.py
```

Open the URL printed by Streamlit and select project `demo` in the sidebar.

## Dashboard pages

- `Overview`: run count, runtime, cost, token volume, model and tool calls,
  retries, recent runs, and runtime charts.
- `Traces`: stored trace metadata, event timelines, and trace JSON downloads.
- `Optimization Queue`: recurring findings ranked across stored traces.
- `Optimization`: bottlenecks, parallelizable groups, optimization findings,
  and plan downloads.
- `Diagnosis`: evidence, severity, affected spans, estimated savings, and replay
  criteria.
- `Patch Plan`: dry-run, framework-aware workflow rewrite plans.
- `Replay Proof`: before-and-after metrics, quality gates, and PR comment
  previews.
- `Quality Gates`: fixture scoring with built-in scorer types.
- `Value & Pricing`: operational ROI, reliability risk, and configurable pricing
  scenarios.
- `API Keys`: project-scoped API-key creation.
- `Ingest`: demo generation, trace upload, and project storage.
- `Setup`: local, API, and Postgres command references.

## Use Postgres

Set the shared store configuration before running commands or the dashboard:

```bash
export AGENTLOOP_STORE_BACKEND=postgres
export AGENTLOOP_DATABASE_URL=postgresql://agentloop:agentloop@localhost:5432/agentloop
uv run agentloop init-store
uv run agentloop server --host 127.0.0.1 --port 8000
uv run streamlit run dashboard/app.py
```

The CLI, API, and dashboard must use the same database if they are expected to
show the same projects and traces.

## Send traces through the API

Create a project key, start the API, and upload a trace:

```bash
uv run agentloop create-api-key --project-id acme --name local-dev
uv run agentloop server --host 127.0.0.1 --port 8000
uv run agentloop upload --path runs/research_agent_baseline.json \
  --api-url http://127.0.0.1:8000 \
  --api-key al_xxx
```

Select project `acme` in a dashboard connected to the same store.

## Security boundaries

The dashboard does not provide built-in user authentication. Keep it bound to a
trusted interface for local use. For shared or internet-facing deployments, put
it behind an authenticated reverse proxy and follow
[PRODUCTION.md](PRODUCTION.md), including TLS, explicit CORS, API-key auth,
retention, backups, and database access controls.

Custom quality scorers import Python code and must be used only with trusted
local fixtures. The HTTP API rejects custom scorers.
