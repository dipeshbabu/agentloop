# Dashboard guide

Parallelization candidates display their evidence level and assumptions in both
Optimization and Diagnosis. See [parallelization evidence](PARALLELIZATION.md)
for the distinction between observed timing, declarations, and inferred actionability.

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
- `Traces`: stored trace metadata and labels, a `KEY=VALUE` label filter
  (such as `commit=abc123` from CI uploads), event timelines, trace JSON
  downloads, and deleting a trace after confirmation.
- `Optimization Queue`: recurring findings ranked across stored traces.
- `Optimization`: bottlenecks, parallelizable groups, optimization findings,
  and plan downloads.
- `Diagnosis`: evidence, severity, affected spans, estimated savings, and replay
  criteria.
- `Patch Plan`: dry-run, framework-aware workflow rewrite plans.
- `Replay Proof`: before-and-after metrics, quality gates, and PR comment
  previews.
- `CI Runs`: traces uploaded by the reusable comparison workflow, grouped by
  their `workflow_run` label, with the run's newest baseline and candidate
  compared in one step using the same gates and PR comment as `Replay Proof`.
- `Quality Gates`: fixture scoring with built-in scorer types.
- `Value & Pricing`: operational ROI, reliability risk, and configurable pricing
  scenarios.
- `Audit Log`: key creation and revocation, trace deletions and retention purges
  in the current project, newest first, with who acted. Signed-in project users
  see only their own project.
- `API Keys`: project-scoped API-key creation.
- `Ingest`: demo generation, trace upload, and project storage.
- `Setup`: local, API, and Postgres command references.

## Projects with many runs

The Overview table and chart, and the trace pickers on the analysis pages,
show the project's newest 200 runs. When a project has more, each picker adds an
"older run ID" box: paste a run ID from the `Traces` page, which pages through
every run, to open an older one. The Optimization Queue counts findings in the
database instead of loading them, and loads finding instances only for the
selected queue item.

To measure a store of your own size, run the scale benchmark. It fills a fresh
store and times each store query, API route and dashboard page:

```bash
uv run --all-extras python scripts/benchmark_scale.py --traces 5000
```

Set `AGENTLOOP_STORE_BACKEND=postgres` and `AGENTLOOP_DATABASE_URL` to measure
Postgres instead (use an empty database; add `--skip-dashboard` to skip the
pages). CI runs it with 2,000 traces against SQLite and Postgres and fails if any
operation takes more than 15 seconds or an API response exceeds 5 MB.

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

## Project sign-in for shared dashboards

Set `AGENTLOOP_DASHBOARD_REQUIRE_KEY=true` when several teams share one
dashboard (AgentLoop 0.2.7 and newer). The dashboard then asks for a project API
key, checks it the same way the HTTP API does, and shows only the traces,
findings and reports of the project that key belongs to. The free-text Project
field is replaced by the signed-in project and a Sign out button. The `API Keys`
page (creating keys is an administrator task) and the `Patch Plan` page (it
reads files on the dashboard host) are hidden, and the `Ingest` page offers only
uploads into the signed-in project, without the demo generators that write files
on the host. After five failed sign-ins in a browser session, further attempts
wait one minute. Open sessions re-check their key at least once a minute, so a
revoked key loses access within a minute.

Create and revoke project keys with the API's admin key, or with
`agentloop create-api-key` and `agentloop revoke-api-key`.
Without the setting, the dashboard keeps its local, single-user behaviour.

Demo generators on the `Ingest` page write trace files to
`AGENTLOOP_DASHBOARD_RUNS_DIR`, or `./runs` when it is unset; when `./runs`
cannot be created the dashboard uses a temporary directory. The container image
sets it to `/data/runs`.

## Security boundaries

Keep a local dashboard bound to a trusted interface. For shared or
internet-facing deployments, enable project sign-in, put the dashboard behind an
authenticated reverse proxy, and follow [PRODUCTION.md](PRODUCTION.md), including
TLS, explicit CORS, API-key auth, retention, backups, and database access
controls. The [cloud deployment stack](CLOUD_DEPLOY.md) does all of this.

Quality fixture validation runs only when the Quality Gates form is submitted.
Empty suites, missing scorer requirements, and malformed JSON are shown beside
the form and do not produce a passing report.

The dashboard accepts `exact_match`, `contains`, bounded `glob`,
`required_fields`, and `json_subset` scorers. Custom quality scorers import
Python code and must be used only with trusted local fixtures. The HTTP API
rejects custom scorers. Migrate removed `regex` fixtures to bounded `glob`,
`contains`, or `exact_match`; migrate `json_schema` fixtures to
`required_fields` or `json_subset`.
