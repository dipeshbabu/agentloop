# HTTP API v1

Application routes are served under `/v1`. The Python client adds that prefix
centrally; configure `base_url` or `AGENTLOOP_API_URL` as the server origin or
deployment mount path, without `/v1`:

```python
from agentloop import AgentLoopClient

client = AgentLoopClient(base_url="http://127.0.0.1:8000", api_key="your-project-key")
traces = client.list_traces(page_size=20)
```

This sends `GET /v1/traces?page_size=20`. A mount such as
`https://example.com/agentloop` produces `/agentloop/v1/traces`. The current client
requires a server with v1 support; upgrade the server before upgrading clients.
Older clients can continue using the deprecated unversioned aliases during 0.x.

## Routes and migration

| Versioned route | Deprecated compatibility route |
| --- | --- |
| `POST /v1/api-keys` | `POST /api-keys` |
| `GET/POST /v1/traces` | `GET/POST /traces` |
| `GET /v1/traces/{run_id}/report` | `GET /traces/{run_id}/report` |
| `GET /v1/traces/{run_id}/optimization` | `GET /traces/{run_id}/optimize` |
| `GET/POST /v1/traces/{run_id}/diagnosis` | `GET/POST /traces/{run_id}/diagnosis` |
| `GET /v1/findings` | `GET /findings` |
| `POST /v1/findings/{run_id}/{finding_id}/status` | `POST /findings/{run_id}/{finding_id}/status` |
| `POST /v1/interventions` | `POST /interventions` |
| `GET /v1/interventions/{intervention_id}` | `GET /interventions/{intervention_id}` |
| `POST /v1/quality-reports` | `POST /quality-report` |
| `GET /v1/optimization-queue` | `GET /optimization-queue` |
| `GET /v1/optimization-queue/github-issues` | `GET /optimization-queue/github-issues` |
| `GET /v1/traces/{run_id}/value` | `GET /traces/{run_id}/value` |
| `GET /v1/usage` | `GET /usage` |

The older `GET /traces/{run_id}/diagnose` alias also remains read-only. Use
`GET /v1/traces/{run_id}/diagnosis` to preview and `POST` to persist findings.
There is no `/v1/.../diagnose` or `/v1/.../optimize` alias; v1 uses resource names.

Health and readiness are operational endpoints: `/health` and `/readyz` remain
supported without deprecation, alongside `/v1/health` and `/v1/readyz`.
Existing load-balancer and container probes need no migration.

All application aliases are deprecated in OpenAPI but remain available throughout
the pre-1.0 series. **Their earliest removal is 1.0, with notice in that release's
changelog.** No removal date is scheduled and this change removes no route.
New features need not add unversioned aliases.

## Compatibility policy

API version and package version are separate. OpenAPI at `/openapi.json` identifies
v1 through route prefixes and the `API v1` tag. Its `info.version` remains the
installed server/package version. Deprecated routes have their own tag and
`deprecated: true`; `/docs` shows both surfaces for migration.

Within v1, additive optional fields, optional query parameters, and new resources
can ship without a namespace change. Clients should ignore fields they do not
recognize. Removing/renaming fields, changing required inputs, or changing
existing request/response semantics requires a documented migration and a new
major API namespace rather than silently changing v1. Native trace, intervention,
and study schema versions continue to describe their respective artifact formats.

Project selection, authentication, validation errors, pagination cursors, and
diagnosis write/read behavior are shared by the v1 and legacy handlers. Project
credentials still use `X-AgentLoop-Key`; administrative key creation still uses
`X-AgentLoop-Admin-Key`. Versioning does not broaden either credential's scope.

For an authenticated server, a direct request is:

```bash
curl -H "X-AgentLoop-Key: $AGENTLOOP_API_KEY" \
  "http://127.0.0.1:8000/v1/traces?page_size=20"
```

See [production setup](PRODUCTION.md), [finding lifecycle](FINDING_LIFECYCLE.md),
and [intervention evidence](INTERVENTIONS.md) for the resource behavior.
