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
WAIT_FOR_IMAGE=true
IMAGE_PROBE="${IMAGE_PROBE:-crane}"
IMAGE_POLL_ATTEMPTS="${IMAGE_POLL_ATTEMPTS:-30}"
IMAGE_POLL_INTERVAL="${IMAGE_POLL_INTERVAL:-10}"
ROLLOUT_STARTED=false
PRIOR_ORCHESTRATOR_IMAGE=""
PRIOR_WORKFLOW_API_IMAGE=""

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
  --wait-for-image  Poll GHCR until the image tag exists before rolling out.
                    Default: enabled
  --no-wait-for-image  Skip the GHCR existence preflight (escape hatch)
  --skip-smoke      Skip post-rollout service health checks
  --skip-image-prune  Do not apply the local control-service tag retention policy
  -h, --help        Show this help

Environment:
  IMAGE_PROBE          Command used to probe GHCR manifests (default: crane)
  IMAGE_POLL_ATTEMPTS  Max preflight probe attempts (default: 30)
  IMAGE_POLL_INTERVAL  Seconds between probe attempts (default: 10)
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

probe_image_exists() {
  local image="$1"
  "$IMAGE_PROBE" manifest "$image" >/dev/null 2>&1
}

wait_for_image() {
  local image="$1"
  local attempt
  for ((attempt = 1; attempt <= IMAGE_POLL_ATTEMPTS; attempt++)); do
    if probe_image_exists "$image"; then
      printf '[rollout-research-services] image %s found on GHCR\n' "$image"
      return 0
    fi
    if (( attempt < IMAGE_POLL_ATTEMPTS )); then
      printf '[rollout-research-services] image %s not found yet (attempt %d/%d); CI may still be building\n' \
        "$image" "$attempt" "$IMAGE_POLL_ATTEMPTS" >&2
      sleep "$IMAGE_POLL_INTERVAL"
    fi
  done
  printf '[rollout-research-services] ERROR: image %s not found on GHCR after %d attempts\n' \
    "$image" "$IMAGE_POLL_ATTEMPTS" >&2
  printf '[rollout-research-services] CI is likely still building this tag; re-run once the build finishes.\n' >&2
  return 1
}

service_images() {
  case "$SERVICE" in
    all|workflow-api)
      printf '%s\n' \
        "ghcr.io/ccny-glasslab/glasslab-research-orchestrator:${IMAGE_TAG}" \
        "ghcr.io/ccny-glasslab/glasslab-workflow-api:${IMAGE_TAG}"
      ;;
    research-orchestrator)
      printf '%s\n' "ghcr.io/ccny-glasslab/glasslab-research-orchestrator:${IMAGE_TAG}"
      ;;
  esac
}

preflight_images() {
  local image
  local -a images
  mapfile -t images < <(service_images)
  for image in "${images[@]}"; do
    wait_for_image "$image"
  done
}

capture_prior_images() {
  PRIOR_ORCHESTRATOR_IMAGE="$("$KUBECTL" -n "$NAMESPACE" get deployment glasslab-research-orchestrator \
    -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null || true)"
  PRIOR_WORKFLOW_API_IMAGE="$("$KUBECTL" -n "$NAMESPACE" get deployment glasslab-workflow-api \
    -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null || true)"
}

print_rollback_guidance() {
  printf '[rollout-research-services] ROLLBACK GUIDANCE: rollout failed partway through; the bundle may be mixed-version.\n' >&2
  printf '[rollout-research-services] Roll back each already-updated component to its previous image:\n' >&2
  if [[ -n "$PRIOR_ORCHESTRATOR_IMAGE" ]]; then
    printf '[rollout-research-services]   kubectl -n %s set image deployment/glasslab-research-orchestrator orchestrator=%s\n' \
      "$NAMESPACE" "$PRIOR_ORCHESTRATOR_IMAGE" >&2
  fi
  if [[ -n "$PRIOR_WORKFLOW_API_IMAGE" ]]; then
    printf '[rollout-research-services]   kubectl -n %s set image deployment/glasslab-workflow-api workflow-api=%s\n' \
      "$NAMESPACE" "$PRIOR_WORKFLOW_API_IMAGE" >&2
  fi
}

rollback_guidance_on_error() {
  local status=$?
  if [[ "$ROLLOUT_STARTED" == true ]]; then
    print_rollback_guidance
  fi
  exit "$status"
}

trap rollback_guidance_on_error EXIT

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
  require_object secret glasslab-workflow-api-schedule-worker
  require_object secret glasslab-workflow-api-research-orchestrator
}

rollout_authenticated_workflow_bundle() {
  require_workflow_caller_secrets
  # New callers remain compatible with the old unauthenticated API. Roll them
  # first so the server is never switched to fail-closed auth ahead of clients.
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
  # ConfigMap changes do not roll a StatefulSet, and rabbitmq.conf /
  # enabled_plugins are subPath mounts that never receive live updates. The
  # init renderer and postStart verifier only run in a fresh pod, so every
  # intentional rollout forces a new pod before waiting for status.
  "$KUBECTL" -n "$NAMESPACE" rollout restart \
    statefulset/glasslab-rabbitmq
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
    --wait-for-image)
      WAIT_FOR_IMAGE=true
      shift
      ;;
    --no-wait-for-image)
      WAIT_FOR_IMAGE=false
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

if [[ "$WAIT_FOR_IMAGE" == true && "$SERVICE" != "rabbitmq" ]]; then
  need_cmd "$IMAGE_PROBE"
  preflight_images
fi

if [[ "$SERVICE" != "rabbitmq" ]]; then
  capture_prior_images
  ROLLOUT_STARTED=true
fi

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
