#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NAMESPACE="${GLASSLAB_V2_NAMESPACE:-glasslab-v2}"
KUBECTL="${KUBECTL:-kubectl}"
SERVICE="all"
IMAGE_TAG=""
SYNC=false
SKIP_SMOKE=false
SKIP_IMAGE_PRUNE=false

usage() {
  cat <<'USAGE'
Usage: rollout-research-services.sh [options]

Roll out the authenticated workflow-api bundle and research-orchestrator images.
Images are selected by immutable Git commit tag; this script does not build or push.

Options:
  --service <name>  all, workflow-api, research-orchestrator, or rabbitmq.
                    workflow-api includes all three authenticated callers.
                    rabbitmq rolls out only the task-fabric broker. Default: all
  --tag <tag>       GHCR image tag. Default: full SHA of the checked-out commit
  --sync            Fast-forward the canonical checkout to origin/main first
  --skip-smoke      Skip post-rollout service health checks
  --skip-image-prune  Do not apply the local control-service tag retention policy
  -h, --help        Show this help
USAGE
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    printf '[rollout-research-services] missing command: %s\n' "$1" >&2
    exit 1
  }
}

apply_manifest() {
  local path="$1"
  printf '[rollout-research-services] applying %s\n' "$path"
  "$KUBECTL" apply -f "$path"
}

require_object() {
  local kind="$1"
  local name="$2"
  if ! "$KUBECTL" -n "$NAMESPACE" get "$kind" "$name" >/dev/null 2>&1; then
    printf '[rollout-research-services] required %s/%s is missing in %s\n' \
      "$kind" "$name" "$NAMESPACE" >&2
    exit 1
  fi
}

rollout_workflow_api() {
  local image="ghcr.io/ccny-glasslab/glasslab-workflow-api:${IMAGE_TAG}"

  require_object persistentvolumeclaim glasslab-shared-datasets
  require_object persistentvolumeclaim glasslab-shared-artifacts
  "$ROOT_DIR/scripts/seed-registry.sh"
  apply_manifest "$ROOT_DIR/kubeadm/glasslab-v2/config/10-workflow-api-configmap.yaml"
  apply_manifest "$ROOT_DIR/kubeadm/glasslab-v2/workflow-api/10-rbac.yaml"
  apply_manifest "$ROOT_DIR/kubeadm/glasslab-v2/workflow-api/30-service.yaml"
  apply_manifest "$ROOT_DIR/kubeadm/glasslab-v2/workflow-api/40-workspace-network-policy.yaml"
  printf '[rollout-research-services] deploying workflow-api image %s\n' "$image"
  "$KUBECTL" set image \
    -f "$ROOT_DIR/kubeadm/glasslab-v2/workflow-api/20-deployment.yaml" \
    "workflow-api=$image" --local -o yaml |
    "$KUBECTL" apply -f -
  "$KUBECTL" -n "$NAMESPACE" rollout status \
    deployment/glasslab-workflow-api --timeout=300s
  apply_manifest "$ROOT_DIR/kubeadm/glasslab-v2/workflow-api/50-ingress-network-policy.yaml"
}

rollout_command_router() {
  local image="ghcr.io/ccny-glasslab/glasslab-research-command-router:${IMAGE_TAG}"
  "$KUBECTL" set image \
    -f "$ROOT_DIR/kubeadm/glasslab-v2/research-command-router/10-deployment.yaml" \
    "research-command-router=$image" --local -o yaml |
    "$KUBECTL" apply -f -
  "$KUBECTL" -n "$NAMESPACE" rollout status \
    deployment/glasslab-research-command-router --timeout=300s
}

rollout_schedule_worker() {
  local image="ghcr.io/ccny-glasslab/glasslab-schedule-worker:${IMAGE_TAG}"
  "$KUBECTL" set image \
    -f "$ROOT_DIR/kubeadm/glasslab-v2/schedule-worker/10-deployment.yaml" \
    "schedule-worker=$image" --local -o yaml |
    "$KUBECTL" apply -f -
  "$KUBECTL" -n "$NAMESPACE" rollout status \
    deployment/glasslab-schedule-worker --timeout=300s
}

require_workflow_caller_secrets() {
  require_object secret glasslab-workflow-api-research-command-router
  require_object secret glasslab-workflow-api-schedule-worker
  require_object secret glasslab-workflow-api-research-orchestrator
}

rollout_authenticated_workflow_bundle() {
  require_workflow_caller_secrets
  # New callers remain compatible with the old unauthenticated API. Roll them
  # first so the server is never switched to fail-closed auth ahead of clients.
  rollout_command_router
  rollout_schedule_worker
  rollout_research_orchestrator
  rollout_workflow_api
}

rollout_research_orchestrator() {
  local image="ghcr.io/ccny-glasslab/glasslab-research-orchestrator:${IMAGE_TAG}"

  require_object persistentvolumeclaim glasslab-shared-artifacts
  apply_manifest "$ROOT_DIR/kubeadm/glasslab-v2/research-orchestrator/00-service-account.yaml"
  apply_manifest "$ROOT_DIR/kubeadm/glasslab-v2/research-orchestrator/10-configmap.yaml"
  apply_manifest "$ROOT_DIR/kubeadm/glasslab-v2/research-orchestrator/30-service.yaml"

  printf '[rollout-research-services] deploying research-orchestrator image %s\n' "$image"
  "$KUBECTL" set image \
    -f "$ROOT_DIR/kubeadm/glasslab-v2/research-orchestrator/20-deployment.yaml" \
    "orchestrator=$image" --local -o yaml |
    "$KUBECTL" apply -f -
  "$KUBECTL" -n "$NAMESPACE" rollout status \
    deployment/glasslab-research-orchestrator --timeout=300s
}

rollout_rabbitmq() {
  # The broker is delivery infrastructure only; PostgreSQL stays authoritative
  # (ADR 0004). Credentials come from the SOPS-managed secret; the PVC is
  # provisioned out-of-band like the other static local-PV services.
  require_object secret glasslab-v2-rabbitmq
  require_object persistentvolumeclaim glasslab-rabbitmq-data
  apply_manifest "$ROOT_DIR/kubeadm/glasslab-v2/rabbitmq/20-configmap.yaml"
  apply_manifest "$ROOT_DIR/kubeadm/glasslab-v2/rabbitmq/30-topology.yaml"
  apply_manifest "$ROOT_DIR/kubeadm/glasslab-v2/rabbitmq/40-service.yaml"
  apply_manifest "$ROOT_DIR/kubeadm/glasslab-v2/rabbitmq/50-network-policy.yaml"
  apply_manifest "$ROOT_DIR/kubeadm/glasslab-v2/rabbitmq/60-statefulset.yaml"
  "$KUBECTL" -n "$NAMESPACE" rollout status \
    statefulset/glasslab-rabbitmq --timeout=300s
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --service)
      SERVICE="${2:-}"
      shift 2
      ;;
    --tag)
      IMAGE_TAG="${2:-}"
      shift 2
      ;;
    --sync)
      SYNC=true
      shift
      ;;
    --skip-smoke)
      SKIP_SMOKE=true
      shift
      ;;
    --skip-image-prune)
      SKIP_IMAGE_PRUNE=true
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      printf '[rollout-research-services] unknown argument: %s\n' "$1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

case "$SERVICE" in
  all|workflow-api|research-orchestrator|rabbitmq) ;;
  *)
    printf '[rollout-research-services] invalid service: %s\n' "$SERVICE" >&2
    exit 1
    ;;
esac

need_cmd git
need_cmd "$KUBECTL"

cd "$ROOT_DIR"

if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
  printf '[rollout-research-services] refusing to deploy from a dirty checkout\n' >&2
  git status --short >&2
  exit 1
fi

if [[ "$SYNC" == true ]]; then
  printf '[rollout-research-services] fast-forwarding to origin/main\n'
  git fetch origin main
  git checkout main
  git merge --ff-only origin/main
fi

if [[ -z "$IMAGE_TAG" ]]; then
  IMAGE_TAG="$(git rev-parse HEAD)"
fi

if [[ ! "$IMAGE_TAG" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]]; then
  printf '[rollout-research-services] invalid image tag: %s\n' "$IMAGE_TAG" >&2
  exit 1
fi

require_object secret glasslab-ghcr-pull

case "$SERVICE" in
  all)
    rollout_authenticated_workflow_bundle
    ;;
  workflow-api)
    rollout_authenticated_workflow_bundle
    ;;
  research-orchestrator)
    require_object secret glasslab-workflow-api-research-orchestrator
    rollout_research_orchestrator
    ;;
  rabbitmq)
    rollout_rabbitmq
    ;;
esac

if [[ "$SERVICE" == "rabbitmq" ]]; then
  "$KUBECTL" -n "$NAMESPACE" get statefulset glasslab-rabbitmq \
    -o custom-columns=NAME:.metadata.name,IMAGE:.spec.template.spec.containers[0].image,READY:.status.readyReplicas
  printf '[rollout-research-services] done\n'
  exit 0
fi

printf '[rollout-research-services] deployed images\n'
"$KUBECTL" -n "$NAMESPACE" get deployment \
  glasslab-workflow-api glasslab-research-orchestrator \
  -o custom-columns=NAME:.metadata.name,IMAGE:.spec.template.spec.containers[0].image,READY:.status.readyReplicas

if [[ "$SKIP_SMOKE" != true ]]; then
  "$ROOT_DIR/scripts/smoke-test-v2.sh"
  "$KUBECTL" -n "$NAMESPACE" exec \
    deployment/glasslab-research-orchestrator -c orchestrator -- \
    python -c 'import json, urllib.request; print(json.load(urllib.request.urlopen("http://127.0.0.1:8080/ready")))'
fi

if [[ "$SKIP_IMAGE_PRUNE" != true ]]; then
  "$ROOT_DIR/scripts/prune-control-service-images.sh" --apply \
    --retain-tag "$IMAGE_TAG"
fi

printf '[rollout-research-services] done\n'
