# Deploy on Kubernetes

AgentLoop 0.2.9 and newer include Kubernetes manifests in
[`deploy/kubernetes/`](https://github.com/dipeshbabu/agentloop/tree/main/deploy/kubernetes),
applied with the kustomize support built into `kubectl`. They run the same
container image as the [single-server stack](CLOUD_DEPLOY.md):

| Resource | Purpose |
| --- | --- |
| `agentloop-api` Deployment and Service | The HTTP API on port 8000, two replicas, API keys required, interactive docs off |
| `agentloop-dashboard` Deployment and Service | The dashboard on port 8501, with project sign-in required |
| `agentloop-retention` CronJob | Deletes traces older than `AGENTLOOP_RETENTION_DAYS` once a day |
| `agentloop-postgres` StatefulSet and Service | A single Postgres for evaluation |
| `agentloop-postgres` NetworkPolicy | Only the API, dashboard and retention pods may connect to Postgres |

Every AgentLoop container runs as user 1000 with a read-only root filesystem,
no privilege escalation, all capabilities dropped, the runtime default seccomp
profile and no service account token. Writable paths are `emptyDir` volumes.

## Deploy

```bash
cp deploy/kubernetes/config.env.example deploy/kubernetes/config.env
cp deploy/kubernetes/secrets.env.example deploy/kubernetes/secrets.env
# Edit both files, and pin the image tag in deploy/kubernetes/kustomization.yaml.
kubectl create namespace agentloop
kubectl apply -k deploy/kubernetes --namespace agentloop
kubectl -n agentloop rollout status deployment/agentloop-api
```

`config.env` holds the dashboard origin for CORS, the retention period, the
request-size limit and the database host. `secrets.env` holds the admin key and
the database password. Both are ignored by Git; never commit them. Kustomize
turns them into the `agentloop-config` ConfigMap and the `agentloop-secrets`
Secret, and restarts the pods when their contents change.

Create a project key through the API, as in the
[cloud guide](CLOUD_DEPLOY.md#create-a-project-key-and-upload-traces), for
example after `kubectl -n agentloop port-forward service/agentloop-api 8000`.

## Production database

The evaluation StatefulSet has one replica, no backups and no high
availability. For production, use a managed Postgres: remove `postgres.yaml`
and `network-policy.yaml` from `kustomization.yaml` (restrict database access in
your provider instead), set `PGHOST`, `PGPORT`, `PGDATABASE` and `PGUSER` in
`config.env`, and the password in `secrets.env`. Schema migrations run
automatically, and API replicas can start together safely.

## Expose it

`ingress.example.yaml` shows HTTPS Ingresses for ingress-nginx with
cert-manager. Adjust the hostnames, the issuer and the body-size limit (match
`AGENTLOOP_MAX_REQUEST_BYTES`), then apply it. The dashboard has no user
accounts of its own: keep it behind an authenticating proxy, such as oauth2-proxy
through the `auth-url` annotations in the example, or on a private network.

## What CI verifies

Every change deploys these manifests to a kind cluster with the image built
from that commit, then checks that:

- Postgres, both API replicas and the dashboard become ready;
- the API stores and returns a trace through its Service with a project key, and
  `/docs` returns 404;
- every dashboard page renders inside its pod;
- the API runs as user 1000 and cannot write to its root filesystem;
- the retention CronJob runs to completion with the configured period;
- a pod outside AgentLoop resolves Postgres but cannot connect to it.

The NetworkPolicy needs a network plugin that enforces policies; CI's kind
cluster does. Ingress, cert-manager and managed databases are not exercised in
CI.
