# Internal Service Exposure

Glasslab v2 currently uses a conservative internal-only exposure model.

## Current pattern

- Every v2 Service is `ClusterIP`.
- No `Ingress` objects are deployed for `glasslab-v2`.
- Smoke tests use `kubectl port-forward` to reach `workflow-api`.
- OpenClaw validation currently uses internal cluster networking plus occasional `kubectl port-forward`.
- The repo has no committed reverse-proxy or Tailscale-sidecar pattern for v2 services yet.

## Recommended steady-state pattern

- Keep database and message-bus services private to the cluster.
- Keep backend APIs private to the cluster unless there is a reviewed operator need.
- Use port-forwarding for short-lived admin and validation work.
- Use an internal-only ingress or reverse-proxy layer only for human-facing services that need stable URLs.
- Do not add public ingress for v2 services.

## Service-by-service guidance

### Keep ClusterIP-only

- `glasslab-postgres`
- `glasslab-nats`
- `glasslab-workflow-api`
- MinIO API on `glasslab-minio:9000`

These services are platform internals. Operators can reach them through `kubectl exec`, `kubectl port-forward`, or from approved in-cluster clients.

### Port-forward is acceptable

- `workflow-api` health checks and one-off API validation
- OpenClaw validation during bring-up
- MinIO console on `glasslab-minio:9001`
- optional MLflow when that service is enabled in `glasslab-agents`

Port-forwarding is acceptable for admin operations, smoke tests, and short-lived debugging. It should not be treated as the long-term operator UX.

### Candidate internal ingress or gateway targets

- OpenClaw
- MinIO console
- optional MLflow UI through a separate namespace-local route if it is enabled later

If a stable internal URL is needed later, put only these operator-facing services behind an internal ingress class, Tailscale-served reverse proxy, or another authenticated internal gateway.

## What not to expose directly

- Do not expose Postgres or NATS outside the cluster network.
- Do not expose `workflow-api` publicly. If it ever needs a stable operator URL, route it through OpenClaw or an internal admin-only gateway.
- Do not expose MinIO API publicly unless the auth, bucket policy, and TLS posture are reviewed first.

## Repo path

- Future placeholder ingress manifests live under `kubeadm/glasslab-v2/ingress/`.
- The current example file is `kubeadm/glasslab-v2/ingress/10-internal-services.example.yaml`.

## Exceptions

### Exam JupyterHub (`glasslab-exam`)

- Service `glasslab-exam/proxy-public`, `NodePort` 30800, reached through the gateway's
  Apache route `https://glasslab.org/exam/` (campus and private ranges only, `Require ip`).
- Live since 2026-10-20 for the DSEI 10200 exam; separate namespace, not part of the v2
  platform. Every node's 30800 is reachable from the lab LAN (`externalTrafficPolicy: Cluster`);
  the planned gateway-only restriction is not in place yet.
- Remove the Service (or the whole release) after the exam window. Configuration and teardown:
  [`../../kubeadm/glasslab-exam/README.md`](../../kubeadm/glasslab-exam/README.md); design and
  remaining hardening: [`exam-jupyterhub-on-kubernetes-plan.md`](exam-jupyterhub-on-kubernetes-plan.md).
