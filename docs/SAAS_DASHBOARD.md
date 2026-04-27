# AgentLoop Cloud Dashboard

The Streamlit dashboard is now a local-first SaaS control panel backed by the same persistent store used by the API server.

## Run locally

```bash
pip install -e ".[all,dev]"
agentloop init-store
agentloop demo-all
agentloop store-trace runs/research_agent_baseline.json --project-id demo
streamlit run dashboard/app.py
```

Open the dashboard and choose project `demo` in the sidebar.

## Dashboard pages

- **Overview**: usage, run count, total runtime, token volume, model/tool calls, retry count, recent runs, runtime chart.
- **Traces**: stored trace table, event timeline, trace JSON download.
- **Optimization**: graph-aware optimization plan, bottlenecks, parallelizable groups, plan JSON download.
- **API Keys**: create project-scoped API keys.
- **Ingest**: generate demo traces, upload trace JSON, store traces into a project.
- **Setup**: local, hosted API, and Postgres deployment commands.

## Hosted-style flow

```bash
agentloop create-api-key --project-id acme --name local-dev
agentloop server --host 127.0.0.1 --port 8000
agentloop upload runs/research_agent_baseline.json --api-url http://127.0.0.1:8000 --api-key al_xxx
agentloop remote-usage --api-url http://127.0.0.1:8000 --api-key al_xxx
```

Then run:

```bash
streamlit run dashboard/app.py
```

Use project `acme` in the sidebar.

## Postgres mode

```bash
export AGENTLOOP_STORE_BACKEND=postgres
export AGENTLOOP_DATABASE_URL=postgresql://agentloop:agentloop@localhost:5432/agentloop
agentloop init-store
agentloop server --host 0.0.0.0 --port 8000
streamlit run dashboard/app.py
```

## Product meaning

This changes AgentLoop from a profiler library into a product surface:

1. Developers instrument their agents.
2. Traces are uploaded to a project.
3. The dashboard shows cost, latency, retries, bottlenecks, and optimization cards.
4. Teams use the optimization cards to reduce production agent costs and latency.

The next product layer should be account auth, hosted deployment, billing, and framework-specific auto-instrumentation for LangGraph, CrewAI, OpenAI Agents SDK, and Vercel AI SDK.
