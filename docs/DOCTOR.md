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

From 0.2.8 it also checks the settings for shared servers:

| Check | Fails when | Warns when |
| --- | --- | --- |
| `cors_origins` | an origin is not HTTPS (except `localhost`; `--allow-http` downgrades this to a warning) | |
| `request_size_limit` | `AGENTLOOP_MAX_REQUEST_BYTES` is not a valid size | the limit is disabled with `0` |
| `retention` | `AGENTLOOP_RETENTION_DAYS` is not a positive number | it is unset, so traces are kept forever |
| `dashboard_access` | | `AGENTLOOP_DASHBOARD_REQUIRE_KEY` is not enabled |
| `shared_api_key` | | `AGENTLOOP_API_KEY` is set; it maps to project `default` and cannot be revoked or audited |
| `api_docs` | `AGENTLOOP_API_DOCS` is not true or false | the interactive API docs are enabled |

Warnings do not change the exit code. Run the check with the settings of the
service you are checking; dashboard and retention settings usually live on their
own services. CI runs it inside the cloud deployment stack on every change.

Do not use `doctor` as a production security checklist, and do not interpret `production-check` failures as evidence that local AgentLoop profiling is unusable.
