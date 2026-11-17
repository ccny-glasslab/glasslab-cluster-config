# ROBOJAN on Glasslab

[ROBOJAN](https://github.com/ccny-glasslab/robojan) (Bob, Roz, Janet) is a
separate project that runs on Glasslab hardware. It lives in its own
namespace, `robojan`, and borrows two shared services from `glasslab-v2`:
Postgres (its own database and role) and MinIO (its own bucket and scoped
user). It does not touch the research orchestrator or workflow-api.

`scripts/deploy-glasslab-v2.sh` does not apply this directory. Apply it by
hand from the provisioner, as below. The only shared files it changes are the
Postgres and MinIO ingress policies, which admit Bob's pods, the one-shot
provisioner Jobs, and the existing workflow-api scoped-user provisioner.

## Layout

| File | Namespace | What it is |
| --- | --- | --- |
| `00-namespace.yaml` | — | `robojan`, Pod Security `restricted` |
| `10-secret.example.yaml` | both | Secret contracts (documentation only) |
| `20-provision-postgres-job.yaml` | glasslab-v2 | Creates role and database `robojan_bob` |
| `30-minio-policy.yaml` | glasslab-v2 | Bucket policy: read, create, and abort incomplete uploads in `robojan-originals`, no delete |
| `35-provision-minio-job.yaml` | glasslab-v2 | Creates the versioned bucket and Bob's scoped user |
| `40-bob-deployment.yaml` | robojan | Bob Deployment and ClusterIP Service on 8080 |
| `45-bob-migrate-job.yaml` | robojan | One-shot `python -m bob migrate` before a Bob rollout |
| `50-network-policy.yaml` | robojan | Bob accepts traffic only from the `robojan` namespace |
| `60-portal-deployment.yaml` | robojan | Portal ConfigMap, Deployment, and NodePort Service on 30090 |
| `70-portal-network-policy.yaml` | robojan | Portal ingress from the lab LAN; egress to Bob, DNS, and Google OAuth |
| `80-bob-retention-cronjob.yaml` | robojan | Monthly `python -m bob retain` with the default 365/90/365-day windows |

## First rollout (M1)

Run from the canonical checkout on the provisioner:

```bash
ssh glasslab-provisioner
cd /home/glasslab/cluster-config
export KUBECONFIG=/home/glasslab/.kube/config
```

1. Create the namespace and apply the updated shared ingress policies:

   ```bash
   kubectl apply -f kubeadm/robojan/00-namespace.yaml
   kubectl apply -f kubeadm/glasslab-v2/postgres/50-network-policy.yaml
   kubectl apply -f kubeadm/glasslab-v2/minio/50-network-policy.yaml
   ```

2. Create the four Secrets described in `10-secret.example.yaml`. Until SOPS
   enrollment completes, use local non-committed manifests under
   `kubeadm/glasslab-v2/secrets/` (see
   [Deploy Glasslab v2](../../docs/glasslab-v2/runbooks/deploy-v2.md) step 3)
   and add them to the external vault for
   [restore](../../docs/glasslab-v2/runbooks/restore-v2-secrets.md) once the
   operator CLI is available. Generate a fresh random database password and
   MinIO secret key; never paste, echo, or commit them. Create the pull secret
   in `robojan` with
   `GHCR_TOKEN="$(gh auth token)" ./scripts/create-ghcr-pull-secret.sh --namespace robojan`.

3. Provision the database and the bucket, and check the logs:

   ```bash
   kubectl apply -f kubeadm/robojan/20-provision-postgres-job.yaml
   kubectl apply -f kubeadm/robojan/30-minio-policy.yaml
   kubectl apply -f kubeadm/robojan/35-provision-minio-job.yaml
   kubectl -n glasslab-v2 wait --for=condition=complete --timeout=300s \
     job/robojan-bob-provision-postgres job/robojan-bob-provision-minio
   kubectl -n glasslab-v2 logs job/robojan-bob-provision-postgres
   kubectl -n glasslab-v2 logs job/robojan-bob-provision-minio
   ```

   Both Jobs pull from GHCR with `glasslab-ghcr-pull`. MinIO's own registries
   no longer serve anonymous pulls, so the mc image is the mirrored release
   described in
   [the MinIO mirror](../../glasslab-v2/minio/mirror/README.md).

4. Bob's image is pinned to a robojan `main` commit that CI built
   (`ghcr.io/ccny-glasslab/robojan-bob:<40-hex sha>`). To upgrade, bump that
   tag in a committed change. Apply from the clean canonical checkout:

   ```bash
   kubectl apply -f kubeadm/robojan/40-bob-deployment.yaml
   kubectl apply -f kubeadm/robojan/50-network-policy.yaml
   kubectl -n robojan rollout status deploy/robojan-bob
   ```

   Bob creates its schema on start-up and refuses a non-UTF8 database.

## Vector search

Bob embeds each passage through the lab's Ollama service on the exo Mac
`192.168.1.17:11434` (`qwen3-embedding:8b`, 4096 dimensions). It runs as the
`com.glasslab.ollama` launch daemon and listens on the lab network without
authentication, like the exo endpoint. `POST /search` takes
`mode: keyword | vector | hybrid`; keyword is the default, so Bob keeps working
when the Mac is down.

The Postgres provisioning Job creates the `vector` extension in `robojan_bob`
and must be re-run before a Bob image that uses it starts. The index has no
HNSW index because pgvector caps `hnsw` at 2000 dimensions.

Reranking uses `bge-reranker-v2-m3` (Q8, 568M) through llama.cpp on the same
Mac at `192.168.1.17:11435` (launch daemon `com.glasslab.rerank`, 811 MB). Bob
re-sorts the top 50 fused candidates; if the service is down, results keep
their fused order and the run records a `partial` error.

## Search portal

The portal is a separate FastAPI service in this namespace. A person signs in
with Google, searches Bob, and opens originals; Bob's bearer token stays in the
portal's Secret and the browser holds only a signed session cookie. Apache
serves it at `https://glasslab.org/robojan/`.

### Bump the pinned images

CI builds `ghcr.io/ccny-glasslab/robojan-bob:<main commit sha>` and
`ghcr.io/ccny-glasslab/robojan-portal:<main commit sha>` on every push to
`main`. Bump both tags in `40-bob-deployment.yaml` and
`60-portal-deployment.yaml` to that SHA in a committed change.

### Create the portal Secret

Add `PORTAL_SECRET_KEY`, `PORTAL_BOB_TOKEN`, `PORTAL_GOOGLE_CLIENT_ID`, and
`PORTAL_GOOGLE_CLIENT_SECRET` to a `robojan-portal` Secret in `robojan` (see
`10-secret.example.yaml`). Generate the session key with at least 32 random
bytes. The Bob token is the token half of a new `token=portal` pair: add that
pair to Bob's `BOB_API_TOKENS` first, restart Bob, and only then roll the
portal out. Never put a token value in this repository.

The Google client is created by the lab operator. Its redirect URI is exactly
`https://glasslab.org/robojan/auth/callback`; the scopes are `openid email
profile`. Until both Google keys exist, the portal serves a 503 that says
sign-in is not configured.

### Set the whitelist

`robojan-portal-config` ships with `PORTAL_ALLOWED_DOMAINS: ccny.cuny.edu` and
two exact addresses: the operator's personal Gmail and a CUNY login address.
CUNY's `login.cuny.edu` identity is Microsoft, not Google, so it will not
authenticate through this Google OAuth client; the personal Gmail is the
working sign-in. Add exact personal Gmail or Google Workspace addresses. Do not
add `gmail.com` as a domain unless every Gmail user should have access. Then:

```bash
kubectl -n robojan apply -f kubeadm/robojan/60-portal-deployment.yaml
kubectl -n robojan rollout restart deploy/robojan-portal
```

### Migrate Bob

Migrations are out of band. Apply the one-shot Job and wait for it before
rolling the Bob Deployment:

```bash
kubectl apply -f kubeadm/robojan/45-bob-migrate-job.yaml
kubectl -n robojan wait --for=condition=complete --timeout=300s job/robojan-bob-migrate
```

Jobs are immutable. Delete the completed Job before a later migration:
`kubectl -n robojan delete job robojan-bob-migrate`.

### Apply and route

```bash
kubectl apply -f kubeadm/robojan/40-bob-deployment.yaml
kubectl -n robojan rollout status deploy/robojan-bob
kubectl apply -f kubeadm/robojan/60-portal-deployment.yaml
kubectl apply -f kubeadm/robojan/70-portal-network-policy.yaml
kubectl -n robojan rollout status deploy/robojan-portal
```

On the gateway, install
`live-config/gateway/etc/apache2/glasslab/robojan-route.conf` and
`robojan-route-http.conf`, then add the matching `Include` lines to
`glasslab-ssl.conf` and `000-default.conf` (after the `/exam/` includes and
above any `ProxyPass /`). Reload Apache only after `apache2ctl -t` passes.

Verify from the gateway:

```bash
curl -fsS -o /dev/null -w '%{http_code}\n' https://glasslab.org/robojan/healthz
curl -sS -o /dev/null -w '%{http_code}\n' https://glasslab.org/robojan/
```

The health check is `200`; the page is `303` to `/robojan/auth/login` once
sign-in is configured, or `503` while it is not. Off-campus clients cannot
reach 443 until CCNY's border permits it, so sign-in is campus-only today (see
`docs/gateway-web.md`).

## Using Bob

Bob has no ingress at M1. From a contributor workstation:

```bash
ssh -L 18090:127.0.0.1:18090 glasslab-provisioner \
  'sudo -n env KUBECONFIG=/home/glasslab/.kube/config \
   kubectl -n robojan port-forward svc/robojan-bob 18090:8080'
curl -fsS http://127.0.0.1:18090/healthz
curl -fsS -H "Authorization: Bearer $BOB_TOKEN" http://127.0.0.1:18090/manifest
```

The API is described in the robojan repository's `bob/README.md` and at
`/docs` on the running service.

## Rotation and removal

- Rotate the database password or MinIO key by updating the Secret(s), then
  deleting and re-applying the matching provisioner Job and restarting Bob.
- Originals are never deleted through Bob's credential. Removing ROBOJAN
  data is an operator decision made with the root MinIO and Postgres
  identities, not a routine step.
