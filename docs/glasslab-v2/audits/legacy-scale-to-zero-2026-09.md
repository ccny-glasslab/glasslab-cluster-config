# Legacy Scale-to-Zero Audit (2026-09)

Issue #353 (hyperplan T8/E5). Audits each listed service for live references
and records a disposition. Live state queried from the provisioner cluster on
2026-09-04. Repo references checked with `grep -rl` over `kubeadm/ services/
scripts/ docs/`.

## Dispositions

| Service | Manifest | Disposition | Evidence |
| --- | --- | --- | --- |
| assessment-agent | `kubeadm/glasslab-v2/assessment-agent/` | SCALE-TO-0 | Feature flag `GLASSLAB_WORKFLOW_API_ASSESSMENT_AGENT_ENABLED=false` in live configmap; not part of current orchestrator path. |
| intake-agent | `kubeadm/glasslab-v2/intake-agent/` | SCALE-TO-0 | Flag `..._INTAKE_AGENT_ENABLED=false` in live configmap; not part of current orchestrator path. |
| interpretation-agent | `kubeadm/glasslab-v2/interpretation-agent/` | SCALE-TO-0 | Flag `..._INTERPRETATION_AGENT_ENABLED=true` in live configmap (only live-referenced agent); declared not-current in `system-map-2026-07.md`; scaling to 0 makes the workflow-api interpretation endpoint fail closed. |
| design-agent | `kubeadm/glasslab-v2/design-agent/` | SCALE-TO-0 | Flag `..._DESIGN_AGENT_ENABLED=false` in live configmap; not part of current orchestrator path. |
| research-command-router | `kubeadm/glasslab-v2/research-command-router/` | SCALE-TO-0 | Already 0/0 in live cluster; only repo refs are its own manifests, a shared token secret in `workflow-api/10-secret.example`, and the rollout script. |
| schedule-worker | `kubeadm/glasslab-v2/schedule-worker/` | SCALE-TO-0 | Already 0/0 in live cluster; workflow-api references only a shared caller token secret, not the service. |
| agent-api (legacy Titanic v1) | `kubeadm/agent-stack/21-agent-api-deployment.yaml` | SCALE-TO-0 | No current service (orchestrator/workflow-api/workspace-runner) references it; only its own deploy script and legacy docs. |
| runner (legacy) | `services/runner/` | KEEP (no manifest) | No kubeadm Deployment exists; code only. |
| ranker (legacy) | `services/ranker/` | KEEP (no manifest) | No kubeadm Deployment exists; code only. |
| MinIO | `kubeadm/glasslab-v2/minio/` | KEEP | Live (1/1); referenced by orchestrator `s3://` URIs and workflow-api endpoint/secret refs. |
| NATS | `kubeadm/glasslab-v2/nats/` | KEEP | Live (1/1); listed as current in `system-map-2026-07.md`. |
| RabbitMQ | `kubeadm/glasslab-v2/rabbitmq/` | KEEP | Not deployed live; intended task-fabric Celery broker per `docs/superpowers/specs/2026-08-23-postgres-celery-task-fabric-design.md`. |
| MLflow | `kubeadm/agent-stack/30-mlflow-optional.yaml` | KEEP | Not deployed live; optional manifest referenced only by legacy agent-api/runner. |

## Changes

`replicas: 0` set on the seven SCALE-TO-0 Deployments above. No deletion, no
secret or live-state changes.

## Caveats

- interpretation-agent is the only bounded agent still enabled in the live
  workflow-api configmap; scaling it to 0 makes `POST /transitions/create-interpretation`
  fail closed (503). Follow-up: disable the flag or remove the transition path.
- NATS is live but has no code consumers in current services; candidate for a
  later removal pass, out of scope here.