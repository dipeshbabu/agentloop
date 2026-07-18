# Production Guide

AgentLoop has three production surfaces:

- Python package: install the tracing SDK and CLI into agent services.
- API service: receive traces, enforce API keys, store runs, and serve reports.
- Dashboard: inspect traces, usage, optimization plans, and value reports.

## Required configuration

Set secrets outside source control and configure the public origins explicitly:

```bash
AGENTLOOP_STORE_BACKEND=postgres
AGENTLOOP_DATABASE_URL=postgresql://USER:PASSWORD@HOST:5432/agentloop
AGENTLOOP_REQUIRE_API_KEY=true
AGENTLOOP_ADMIN_API_KEY=<long random admin secret>
AGENTLOOP_CORS_ORIGINS=https://dashboard.example.com
AGENTLOOP_API_URL=https://api.example.com
```

`AGENTLOOP_DATABASE_URL` (or `DATABASE_URL`) remains the highest-priority Postgres
connection setting. Percent-encode reserved username and password characters when they are
embedded in a URL. Deployments that can provide separate libpq settings may avoid a
secret-bearing URL entirely:

```bash
AGENTLOOP_STORE_BACKEND=postgres
PGHOST=db.example.com
PGPORT=5432
PGDATABASE=agentloop
PGUSER=agentloop
PGPASSWORD=<database password>
```

For file-backed secrets, set `AGENTLOOP_POSTGRES_PASSWORD_FILE` instead of
`PGPASSWORD`. The file is read only when no complete database URL is configured.

`AGENTLOOP_ADMIN_API_KEY` protects hosted API-key creation. When API auth is enabled,
`POST /api-keys` returns `503` unless the admin secret is configured.

Before deploying or promoting an environment, run:

```bash
uv run agentloop production-check --no-check-api --no-check-store
```

After the API is deployed and the database is reachable, run the full gate:

```bash
uv run agentloop production-check
```

The production check fails unless Postgres, API-key auth, a strong admin key,
explicit CORS origins, and an HTTPS API URL are configured.

## Docker Compose

For a local production-like stack:

```bash
# Copy .env.example to .env, then replace every placeholder secret.
docker compose up --build
```

Compose passes `POSTGRES_PASSWORD` to the database, API, and dashboard through a
Docker secret. It is never interpolated into a connection URL, so URI-reserved punctuation
such as `/`, `?`, `#`, `%`, `@`, and `:` is supported without encoding. Validate
configuration with `docker compose config --quiet`; avoid printing rendered production
configuration because other environment values may still be sensitive.

Services:

- API: `http://localhost:8000`
- Dashboard: `http://localhost:8501`
- Postgres: internal `db:5432`

Create a project API key:

```bash
uv run agentloop remote-create-api-key --project-id acme --name prod \
  --api-url http://localhost:8000 \
  --admin-api-key "$AGENTLOOP_ADMIN_API_KEY"
```

Upload a trace:

```bash
uv run agentloop upload --path runs/research_agent_baseline.json \
  --api-url http://localhost:8000 \
  --api-key al_xxx
```

Run the production smoke check:

```bash
AGENTLOOP_API_URL=http://localhost:8000 \
AGENTLOOP_ADMIN_API_KEY="$AGENTLOOP_ADMIN_API_KEY" \
uv run python scripts/smoke_api.py
```

The official image uses Python 3.13, matching `.python-version` and the highest Python
version exercised by the package CI matrix. The image runs as the non-root `agentloop`
user. Application files under `/app` are read-only to that user; `/data` is the intended
writable path for SQLite deployments.

Use `uv run agentloop production-check --allow-http` only for local staging URLs such as
`http://localhost:8000`; public production traffic should use HTTPS.

## Health Checks

Use these probes in hosting platforms:

```text
GET /health
GET /readyz
```

`/health` confirms the API process is up and returns the package version. `/readyz`
initializes and queries the configured store, so use it for readiness checks.

## Package Release

Tag releases as `vX.Y.Z`. The release workflow builds the Python package and can
publish to PyPI when the repository has a trusted-publishing PyPI environment
configured.

## Deployment Notes

- Use Postgres for hosted API/dashboard deployments.
- Keep SQLite for local demos and single-user development only.
- Set `AGENTLOOP_REQUIRE_API_KEY=true` for internet-facing APIs.
- Set `AGENTLOOP_CORS_ORIGINS` to the exact dashboard origin.
- Rotate `AGENTLOOP_ADMIN_API_KEY` like any other production secret.
- Rotate the Compose database password in both Postgres and the Docker secret, then recreate
  the database, API, and dashboard containers and rerun readiness plus the smoke check.
  `POSTGRES_PASSWORD` initializes only a new, empty Postgres data volume; changing the
  `.env` value alone does not update an existing database role. Use an authenticated
  Postgres password-change procedure before replacing the secret, and keep the old value only
  until the recreated clients connect successfully.
- The Streamlit dashboard does not provide end-user authentication. Treat it as
  an operator console: keep it on a private network or place it behind an
  authenticated reverse proxy. Do not expose it directly to the internet.
- Terminate TLS at a trusted proxy or load balancer and forward only the API and
  authenticated dashboard routes that users need.
- Apply request-body limits, rate limits, timeouts, and access logging at the
  proxy. Trace payloads can be large and may contain sensitive prompts or tool
  data.
- Restrict database network access to the API and dashboard workloads, require
  encrypted database connections in hosted environments, and test backups and
  restores.
- Retain traces only as long as needed. Establish a deletion and redaction policy
  before ingesting production or customer data.

The Compose ports bind to `127.0.0.1` by default to keep the local stack off the
LAN. Use an explicit production deployment configuration instead of weakening
those bindings in the checked-in development file.
