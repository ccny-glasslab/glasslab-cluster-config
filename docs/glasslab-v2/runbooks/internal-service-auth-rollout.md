# Internal Service Auth Rollout (Issue #602)

This runbook covers the shared-token authentication boundary between
`workflow-api`, the four bounded stage agents, and `schedule-worker`, plus the
least-privilege manifest changes that ship with it.

## Components

- **Shared token Secret**: `glasslab-agent-internal-token` (key `token`) in
  `glasslab-v2`. One token for this internal boundary; rotating it rotates all
  five consumers together.
- **Header**: `X-Glasslab-Internal-Token` (constant-time compared).
- **Callers**: `workflow-api` (presents the token on stage-agent calls via
  `GLASSLAB_WORKFLOW_API_AGENT_INTERNAL_TOKEN`) and the
  `glasslab-schedule-worker-run-once` CronJob (presents it to
  `POST /run-once` via `GLASSLAB_AGENT_INTERNAL_TOKEN`).
- **Servers**: intake, interpretation, assessment, design agents and
  `schedule-worker`. Each fails closed: **503** when its token is
  unconfigured, **401** when the header is missing or wrong. `/healthz` stays
  anonymous for kubelet probes and the public smoke test.

## Rollout order (callers first, servers second)

1. **Create the Secret** before any enforcing image rolls out:

   ```bash
   # SOPS-managed live Secret; the tracked example is documentation only:
   # kubeadm/glasslab-v2/secrets/10-agent-internal-token.example.yaml
   kubectl -n glasslab-v2 get secret glasslab-agent-internal-token
   ```

2. **Roll out workflow-api** (caller). It starts sending the header when
   `GLASSLAB_WORKFLOW_API_AGENT_INTERNAL_TOKEN` is mounted. The not-yet-updated
   servers ignore the extra header, so nothing breaks.

   ```bash
   ./scripts/rollout-research-services.sh --service workflow-api
   ```

3. **Roll out the four stage agents** (servers). They now enforce the token.
   Their deployments mount `GLASSLAB_AGENT_INTERNAL_TOKEN` from the shared
   Secret. Verify `/healthz` and one workflow-api stage call per agent.

4. **Roll out schedule-worker, then apply its CronJob.** The CronJob
   (`kubeadm/glasslab-v2/schedule-worker/30-cronjob.yaml`) is the only caller
   of `POST /run-once` and presents the token. Trigger it once by hand and
   confirm success:

   ```bash
   kubectl -n glasslab-v2 create job --from=cronjob/glasslab-schedule-worker-run-once run-once-manual
   kubectl -n glasslab-v2 logs job/run-once-manual
   ```

5. **Apply the ingress NetworkPolicies** (agent `50-ingress-network-policy.yaml`
   files and `schedule-worker/50-ingress-network-policy.yaml`). They are
   defense-in-depth behind the token gate: only the `workflow-api` pod label
   may reach the agents (ports 8090-8093) and only the CronJob pod label may
   reach `schedule-worker` (port 8094). Deploying them after step 3 keeps a
   mislabelled emergency caller from being cut off before token auth is live.

6. **Apply the workflow-api privilege changes.** The deployment pins
   `automountServiceAccountToken: true` (job submission calls
   `load_incluster_config()`) and the RBAC Role drops the unused `jobs`
   `list`/`watch` verbs. Both roll out with the workflow-api image.

## Rollback

- **Preferred: revert servers first, caller second.** Redeploy the previous
  (pre-auth) agent and schedule-worker images; they ignore the header. Then
  roll workflow-api back if its caller change is the problem.
- **Do not roll back by unsetting the token.** An unset server token is a
  **503 fail-closed**, not an open door. A partial state (workflow-api rolled
  back first while agents still enforce) makes stage calls fail their token
  check and degrades to the deterministic fallback path, so it is not a silent
  no-op: restore the enforcing side or revert it, never leave it half applied.
- **Token rotation**: update the Secret, then restart all five consumers
  (`kubectl rollout restart deployment/...`); environment variables are read
  at process start, and each CronJob run reads the Secret fresh.
- **NetworkPolicy rollback**: delete the specific
  `glasslab-<service>-ingress` policy. **RBAC rollback**: re-apply the previous
  Role only if a code path that lists/watches jobs is reintroduced; the
  current code paths use `get`, `create`, and `delete` only.

## Verification checklist

```bash
kubectl -n glasslab-v2 get netpol \
  glasslab-intake-agent-ingress glasslab-interpretation-agent-ingress \
  glasslab-assessment-agent-ingress glasslab-design-agent-ingress \
  glasslab-schedule-worker-ingress
kubectl -n glasslab-v2 get cronjob glasslab-schedule-worker-run-once
# Unauthenticated request from an arbitrary pod must be denied:
#   curl -s -o /dev/null -w '%{http_code}' http://glasslab-intake-agent:8090/healthz  # 200 (probe path)
#   curl -s -o /dev/null -w '%{http_code}' -X POST http://glasslab-intake-agent:8090/normalize-intake  # 401
```
