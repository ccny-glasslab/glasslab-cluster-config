# Schedule Worker

ClusterIP deployment manifests for the bounded `schedule-worker` service live here.

Current intended role:

- call the bounded `workflow-api` due-digest execution path
- remain internal-only in `glasslab-v2`
- stay limited to digest execution first
- avoid arbitrary job scheduling or free-form agent control

Callers and authentication (issue #602):

- `POST /run-once` requires the `X-Glasslab-Internal-Token` shared token
  (`GLASSLAB_AGENT_INTERNAL_TOKEN`, Secret `glasslab-agent-internal-token`);
  the service fails closed with 401/503 without it.
- `30-cronjob.yaml` is the only caller of `/run-once` and the only pod peer
  admitted by `50-ingress-network-policy.yaml` on port 8094.
- `deploy-glasslab-v2.sh` applies this directory in sorted order, so the
  CronJob and NetworkPolicy ship with the Deployment.
