# Deploy to the cloud

A cloud deployment gives a team one shared AgentLoop server. Developers and CI
upload traces to it with the Python or TypeScript client, and everyone reviews
the same traces, findings and reports. The CLI, SDKs and guided workflow keep
running where your agents run; only the shared server moves to the cloud.

The server has three parts, all started from one container image:

| Part | What it does | Port inside the stack |
| --- | --- | --- |
| API | Stores traces and serves reports under `/v1`, with project API keys | 8000 |
| Dashboard | Streamlit console for traces, findings and comparisons | 8501 |
| Postgres | Shared storage; schema migrations run automatically on startup | 5432 |

## Container image

From AgentLoop 0.2.7, every release publishes an image to GitHub Container
Registry, built from the validated release tag:

```bash
docker pull ghcr.io/dipeshbabu/agentloop:0.3.0
```

The image runs as a non-root user. Its default command starts the API on port
8000; the dashboard uses the same image with a `streamlit run dashboard/app.py`
command. From 0.2.8, each tag covers `linux/amd64` and `linux/arm64`, so ARM
servers such as AWS Graviton, Google Axion and Azure Cobalt pull the right image
automatically; 0.2.7 is `linux/amd64` only. Pin an exact version in
deployments; `latest` follows the newest release.

## Option 1: one server on any cloud

This is the recommended starting point. It works on any Linux virtual machine,
such as an AWS EC2 instance, a Google Compute Engine VM, an Azure VM or a
DigitalOcean droplet, and it is the setup CI runs on every change.

The `deploy/` directory contains the stack:

- `deploy/docker-compose.yml` starts Postgres, the API, the dashboard and
  [Caddy](https://caddyserver.com/), which obtains and renews HTTPS
  certificates automatically;
- `deploy/Caddyfile` serves the API on one hostname and the dashboard on
  another, with a login in front of the dashboard. Inside, the dashboard also
  asks for a project API key and shows only that project;
- a `retention` service deletes traces older than `AGENTLOOP_RETENTION_DAYS`
  once a day, when that is set;
- `deploy/.env.example` lists every setting.

Only ports 80 and 443 are published. The API, dashboard and database are
reachable only through Caddy or the private Compose network.

### Prepare the server

1. Create a Linux VM and install Docker Engine with the Compose plugin.
2. Allow inbound TCP 80 and 443 (and UDP 443 for HTTP/3) in the cloud
   firewall or security group. Keep SSH restricted to your own addresses.
3. Create DNS records for two hostnames, for example
   `agentloop-api.example.com` and `agentloop.example.com`, pointing at the VM's
   public address. Caddy can only issue certificates once these resolve.

### Configure and start

```bash
git clone --depth 1 --branch v0.3.0 https://github.com/dipeshbabu/agentloop.git
cd agentloop
cp deploy/.env.example deploy/.env
```

Create the dashboard password hash and random secrets:

```bash
docker run --rm caddy:2 caddy hash-password --plaintext 'choose-a-strong-password'
openssl rand -hex 32
```

Edit `deploy/.env`: set `AGENTLOOP_IMAGE` to the release you want, both
domains, `AGENTLOOP_ADMIN_API_KEY`, `POSTGRES_PASSWORD`, the dashboard user and
the bcrypt hash. Keep the hash in single quotes so Compose does not expand its
`$` characters. Never commit `deploy/.env`.

```bash
docker compose -f deploy/docker-compose.yml up --detach
docker compose -f deploy/docker-compose.yml ps
curl https://agentloop-api.example.com/v1/readyz
```

Open the dashboard hostname in a browser and enter the user and password you
chose. The dashboard then asks for a project API key (created in the next
step) and shows only that key's project.

### Create a project key and upload traces

The admin key only creates project keys. Create one per team or CI pipeline:

```bash
curl -X POST https://agentloop-api.example.com/v1/api-keys \
  -H "X-AgentLoop-Admin-Key: $AGENTLOOP_ADMIN_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"project_id": "my-team", "name": "ci"}'
```

Use the returned key from Python:

```python
from agentloop import AgentLoopClient

client = AgentLoopClient(base_url="https://agentloop-api.example.com", api_key="...")
client.upload_trace(trace)
```

or from the TypeScript SDK with
`new AgentLoopClient({ baseUrl: "https://agentloop-api.example.com", apiKey: "..." })`.
To send traces from CI automatically, see
[sending traces to your AgentLoop server](CI.md#send-the-traces-to-your-agentloop-server).
Sign in to the dashboard with the same key to see that project's traces. Each
team can only see its own project; there is no project switcher.

### Review the audit log

The server records key creation and revocation, trace deletions and retention
purges, with who acted. Review it with the admin key:

```bash
agentloop remote-audit-log --api-url https://agentloop-api.example.com
```

### Revoke a key

If a key leaks or a pipeline is retired, list the project's keys and revoke the
one by its prefix (the first 10 characters of the key):

```bash
export AGENTLOOP_ADMIN_API_KEY=...
agentloop remote-list-api-keys --project-id my-team --api-url https://agentloop-api.example.com
agentloop remote-revoke-api-key --project-id my-team --prefix al_AbCdEfG \
  --api-url https://agentloop-api.example.com
```

The API rejects the key immediately, and dashboard sessions signed in with it
end within a minute.

### Delete traces and set a retention period

Set `AGENTLOOP_RETENTION_DAYS` in `deploy/.env` to delete traces once they are
older than that many days. The stack's `retention` service applies it once a day;
leave it empty to keep traces until they are deleted by hand. After changing it,
run `docker compose -f deploy/docker-compose.yml up --detach` again.

To delete one trace, use the dashboard's **Delete this trace** control on the
Traces page, or with the project's key in `AGENTLOOP_API_KEY`:

```bash
agentloop remote-delete-trace RUN_ID --api-url https://agentloop-api.example.com
```

Deleting a trace also removes its findings, usage records and any interventions
that compare it. It cannot be undone except by restoring a backup.

### Upgrade and back up

Back up the database, change `AGENTLOOP_IMAGE` to the new version, then
recreate the containers. Pending migrations run on startup.

```bash
docker compose -f deploy/docker-compose.yml exec db \
  pg_dump --format=custom --username agentloop --file /tmp/agentloop.dump agentloop
docker compose -f deploy/docker-compose.yml cp db:/tmp/agentloop.dump ./agentloop.dump
docker compose -f deploy/docker-compose.yml pull
docker compose -f deploy/docker-compose.yml up --detach
```

Copy backups off the server and test restoring them. Migrations are
forward-only; to roll back, restore the backup and redeploy the previous image.
See [database schema migrations](PRODUCTION.md#database-schema-migrations).

### What CI verifies

Every pull request starts this exact stack with the image built from that
commit, using Caddy's local certificate authority for `*.localhost` names. It
checks that:

- the API is ready over HTTPS and stores and returns a trace with a project key;
- plain HTTP redirects to HTTPS, and uploads over the size limit are refused;
- the dashboard returns 401 without the login or with a wrong password, loads
  with the right one, and runs with project sign-in enabled;
- the API, dashboard and database ports are not published on the host.

## Option 2: managed container platforms

Managed platforms run the image without a server to maintain. Pair the API with
the platform's managed Postgres:

| Platform | Containers | Postgres |
| --- | --- | --- |
| Google Cloud | Cloud Run | Cloud SQL for PostgreSQL |
| AWS | App Runner or ECS on Fargate | Amazon RDS for PostgreSQL |
| Azure | Container Apps | Azure Database for PostgreSQL |
| Others | Fly.io, Render, Railway | Their managed Postgres |

These deployments are not exercised in CI. The same image and settings apply:

1. Some platforms pull only from their own registry, for example Cloud Run from
   Artifact Registry and App Runner from Amazon ECR. Copy the image there:

   ```bash
   docker pull ghcr.io/dipeshbabu/agentloop:0.3.0
   docker tag ghcr.io/dipeshbabu/agentloop:0.3.0 REGISTRY/agentloop:0.3.0
   docker push REGISTRY/agentloop:0.3.0
   ```

2. Run the API with container port 8000 and its default command, and set:

   ```bash
   AGENTLOOP_STORE_BACKEND=postgres
   AGENTLOOP_DATABASE_URL=postgresql://USER:PASSWORD@HOST:5432/agentloop
   AGENTLOOP_REQUIRE_API_KEY=true
   AGENTLOOP_ADMIN_API_KEY=<long random secret>
   AGENTLOOP_CORS_ORIGINS=https://<dashboard hostname>
   AGENTLOOP_API_DOCS=false
   ```

   Store the secrets in the platform's secret manager rather than plain
   environment settings, and percent-encode reserved characters in the URL
   password. Use `/readyz` as the health check path.

3. Optionally run the dashboard as a second service from the same image with
   the command `streamlit run dashboard/app.py --server.address=0.0.0.0
   --server.port=8501 --server.headless=true`, the same database settings,
   `AGENTLOOP_API_URL` set to the API's URL, and
   `AGENTLOOP_DASHBOARD_REQUIRE_KEY=true` so users sign in with a project key.
   Also put it behind the platform's identity layer, such as Identity-Aware
   Proxy on Google Cloud or built-in authentication on Azure Container Apps, or
   keep it on a private network. Do not expose it publicly without one.

Multiple API replicas can share one database; migrations are safe to run
concurrently. From 0.2.8 the API refuses request bodies over
`AGENTLOOP_MAX_REQUEST_BYTES` (16 MiB by default) with `413`, even without a
proxy in front; raise it, and any platform limit, to fit your largest trace
uploads.

## Kubernetes

From 0.2.9, [`deploy/kubernetes/`](KUBERNETES.md) has hardened manifests for
the API, the dashboard, a retention CronJob and an evaluation Postgres, applied
with `kubectl apply -k`. CI deploys them to a kind cluster on every change.

## Security checklist

- Require API keys (`AGENTLOOP_REQUIRE_API_KEY=true`) and keep the admin key
  out of clients; give each team or pipeline its own project key, and revoke
  keys that leak or are no longer used.
- Serve only HTTPS, never expose the dashboard without a login, and keep
  project sign-in enabled so teams only see their own traces.
- Keep Postgres off the public internet and back it up.
- Traces can contain prompts, tool inputs and outputs. Decide what you capture
  ([trace retention](TRACE_RETENTION.md)) and how long the server keeps it
  (`AGENTLOOP_RETENTION_DAYS`) before sending production data.

The [production guide](PRODUCTION.md) covers every setting, health checks and
password rotation in detail.
