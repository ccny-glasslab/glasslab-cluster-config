# Deploy Bounded Agents

This runbook covers the first bounded stage-agent rollout for Glasslab v2.

Current agent set:

- intake-agent
- interpretation-agent
- assessment-agent
- design-agent
- schedule-worker

## Purpose

Deploy the internal-only bounded services behind `workflow-api` without enabling
them all at once.

## 1. Build And Push The Agent Images

From the canonical repo on `.44`:

```bash
cd /home/glasslab/cluster-config
GHCR_TOKEN="$(gh auth token)" ./scripts/push-bounded-agent-images.sh
GHCR_TOKEN="$(gh auth token)" ./scripts/create-ghcr-pull-secret.sh
```

## 2. Apply The Manifests

Apply the bounded-agent manifests and the updated `workflow-api` ConfigMap:

```bash
./scripts/deploy-glasslab-v2.sh
```

The core deploy script now includes the bounded-agent manifest directories.

## 3. Verify Service Rollout

```bash
kubectl -n glasslab-v2 rollout status deployment/glasslab-intake-agent --timeout=120s
kubectl -n glasslab-v2 rollout status deployment/glasslab-interpretation-agent --timeout=120s
kubectl -n glasslab-v2 rollout status deployment/glasslab-assessment-agent --timeout=120s
kubectl -n glasslab-v2 rollout status deployment/glasslab-design-agent --timeout=120s
kubectl -n glasslab-v2 rollout status deployment/glasslab-schedule-worker --timeout=120s
kubectl -n glasslab-v2 get deploy,svc | egrep 'agent|workflow-api'
```

## 4. Keep Feature Flags Off By Default

The `workflow-api` ConfigMap keeps all four agent integrations disabled by
default:

- `GLASSLAB_WORKFLOW_API_INTAKE_AGENT_ENABLED=false`
- `GLASSLAB_WORKFLOW_API_INTERPRETATION_AGENT_ENABLED=false`
- `GLASSLAB_WORKFLOW_API_ASSESSMENT_AGENT_ENABLED=false`
- `GLASSLAB_WORKFLOW_API_DESIGN_AGENT_ENABLED=false`

That means deployment alone is safe.

## 5. Internal Service Authentication

The four bounded agents and `schedule-worker` require the shared internal
token (`glasslab-agent-internal-token`, header `X-Glasslab-Internal-Token`)
on every non-health route; they fail closed with 401/503 without it. Provision
the Secret and roll out `workflow-api` (the caller) before the enforcing agent
images. See [`internal-service-auth-rollout.md`](internal-service-auth-rollout.md)
for the full callers-first order and rollback procedure.

## 5. Enable One Agent At A Time

Turn on only one integration flag at a time and restart `workflow-api`.

Suggested order:

1. interpretation-agent
2. intake-agent
3. assessment-agent
4. design-agent
5. schedule-worker after digest schedules are enabled intentionally

## 6. Verify Behavior Through Workflow-API

After enabling one stage:

```bash
./scripts/smoke-test-v2.sh --include-bounded-agents
kubectl -n glasslab-v2 logs deploy/glasslab-workflow-api --tail=200
```

Then exercise the relevant endpoint only:

- intake:
  - `POST /intakes`
- interpretation:
  - `POST /interpretations/from-latest-intake`
- assessment:
  - `POST /replicability-assessments/from-latest-interpretation`
- design:
  - `POST /design-drafts/from-latest-intake`
  - `POST /design-drafts/from-latest-assessment`

## 7. Fail Closed If Anything Looks Wrong

If a stage misbehaves:

- set its feature flag back to `false`
- reapply the ConfigMap
- restart `workflow-api`

The deterministic fallback paths remain available, so the system does not need
to stay broken while the bounded service is debugged.
