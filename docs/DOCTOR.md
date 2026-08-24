# Diagnostics

AgentLoop has two setup checks with intentionally different scopes.

## `agentloop doctor`

`doctor` validates a normal local installation:

- supported Python version;
- the actual core dependencies, `typer` and `rich`;
- a writable trace output directory;
- the configured local store;
- optional hosted/API wiring when it is actually configured.

A local-only install does not need an API key and does not need auto-upload. Those states are reported as healthy local configuration rather than warnings.

By default, the API health check is skipped when neither `AGENTLOOP_API_URL` nor auto-upload is configured. If an API URL is explicitly configured, `doctor` probes `/health` and reports reachability as a warning rather than turning a usable local install into a failure.

## `agentloop production-check`

`production-check` remains strict for an internet-facing deployment. It checks production-oriented requirements such as Postgres, database configuration, API authentication, admin-key strength, CORS origins, HTTPS, store initialization, and API readiness.

Do not use `doctor` as a production security checklist, and do not interpret `production-check` failures as evidence that local AgentLoop profiling is unusable.
