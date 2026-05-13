# Production Guide

AgentLoop has three production surfaces:

- Python package: install the tracing SDK and CLI into agent services.
- API service: receive traces, enforce API keys, store runs, and serve reports.
- Dashboard: inspect traces, usage, optimization plans, and value reports.

## Required Secrets

Set these outside source control:

```bash
AGENTLOOP_STORE_BACKEND=postgres
AGENTLOOP_DATABASE_URL=postgresql://USER:PASSWORD@HOST:5432/agentloop
AGENTLOOP_REQUIRE_API_KEY=true
AGENTLOOP_ADMIN_API_KEY=<long random admin secret>
AGENTLOOP_CORS_ORIGINS=https://dashboard.example.com
```

`AGENTLOOP_ADMIN_API_KEY` protects hosted API-key creation. When API auth is enabled,
`POST /api-keys` returns `503` unless the admin secret is configured.

Before deploying or promoting an environment, run:

```bash
agentloop production-check --no-check-api --no-check-store
```

After the API is deployed and the database is reachable, run the full gate:

```bash
agentloop production-check
```

The production check fails unless Postgres, API-key auth, a strong admin key,
explicit CORS origins, and an HTTPS API URL are configured.

## Docker Compose

For a local production-like stack:

```bash
copy .env.example .env
# Edit .env and replace every placeholder secret.
docker compose up --build
```

Services:

- API: `http://localhost:8000`
- Dashboard: `http://localhost:8501`
- Postgres: internal `db:5432`

Create a project API key:

```bash
agentloop remote-create-api-key acme prod \
  --api-url http://localhost:8000 \
  --admin-api-key "$AGENTLOOP_ADMIN_API_KEY"
```

Upload a trace:

```bash
agentloop upload runs/research_agent_baseline.json \
  --api-url http://localhost:8000 \
  --api-key al_xxx
```

Run the production smoke check:

```bash
AGENTLOOP_API_URL=http://localhost:8000 \
AGENTLOOP_ADMIN_API_KEY="$AGENTLOOP_ADMIN_API_KEY" \
python scripts/smoke_api.py
```

Use `agentloop production-check --allow-http` only for local staging URLs such as
`http://localhost:8000`; public production traffic should use HTTPS.

## Health Checks

Use these probes in hosting platforms:

```bash
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
