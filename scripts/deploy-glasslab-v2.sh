#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MANIFEST_ROOT="$ROOT_DIR/kubeadm/glasslab-v2"
LOCAL_SECRETS_DIR="${GLASSLAB_V2_LOCAL_SECRETS_DIR:-$MANIFEST_ROOT/secrets}"
KUBECTL="${KUBECTL:-kubectl}"
NAMESPACE="${GLASSLAB_V2_NAMESPACE:-glasslab-v2}"
# Service images are resolved from the checked-out commit, not the placeholder
# tag baked into the manifest, so a plain deploy cannot ship stale code.
IMAGE_TAG="${GLASSLAB_V2_IMAGE_TAG:-}"
usage() {
  cat <<'USAGE'
Usage: deploy-glasslab-v2.sh

Deploy the core Glasslab v2 services by default:
- namespace
- local secrets if present
- config
- Postgres
- NATS
- MinIO
- bounded stage-agent services
- workflow-api
- research-orchestrator
Example manifests ending in .example.yaml are never applied.

Service image tags are overridden with the checked-out commit SHA (same
semantics as rollout-research-services.sh), so the pending-ci-build markers in
the manifests never reach the cluster unchanged.

Environment:
  GLASSLAB_V2_LOCAL_SECRETS_DIR
      Directory scanned for the *.local.yaml permission preflight (default:
      kubeadm/glasslab-v2/secrets)
  GLASSLAB_V2_IMAGE_TAG
      Image tag to deploy instead of the checked-out commit SHA (default:
      git rev-parse HEAD)
USAGE
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    printf '[deploy-glasslab-v2] missing command: %s\n' "$1" >&2
    exit 1
  }
}

require_k8s_object() {
  local namespace="$1"
  local kind="$2"
  local name="$3"
  if ! "$KUBECTL" -n "$namespace" get "$kind" "$name" >/dev/null 2>&1; then
    printf '[deploy-glasslab-v2] required %s/%s not found in namespace %s\n' "$kind" "$name" "$namespace" >&2
    exit 1
  fi
}

apply_yaml_dir() {
  local dir="$1"
  if ! find "$dir" -maxdepth 1 -type f -name '*.yaml' ! -name '*.example.yaml' | grep -q .; then
    printf '[deploy-glasslab-v2] skipping %s (no deployable YAML manifests yet)\n' "$dir"
    return
  fi

  while IFS= read -r file; do
    printf '[deploy-glasslab-v2] applying %s\n' "$file"
    "$KUBECTL" apply -f "$file"
  done < <(find "$dir" -maxdepth 1 -type f -name '*.yaml' ! -name '*.example.yaml' | sort)
}

# Apply a service directory, but replace the deployment's image tag with the
# resolved commit SHA before it reaches kubectl. The plain `apply -f` path
# would otherwise ship the manifest's placeholder/pinned tag.
apply_service_dir() {
  local dir="$1"
  local deployment="$2"
  local image_repo="$3"
  shift 3
  # One or more container names in that deployment which share this image.
  local containers=("$@")
  local file
  local container
  local set_args=()
  if ! find "$dir" -maxdepth 1 -type f -name '*.yaml' ! -name '*.example.yaml' | grep -q .; then
    printf '[deploy-glasslab-v2] skipping %s (no deployable YAML manifests yet)\n' "$dir"
    return
  fi

  while IFS= read -r file; do
    if [[ "$file" == "$dir/$deployment" ]]; then
      printf '[deploy-glasslab-v2] applying %s (image %s:%s)\n' "$file" "$image_repo" "$IMAGE_TAG"
      set_args=()
      for container in "${containers[@]}"; do
        set_args+=("${container}=${image_repo}:${IMAGE_TAG}")
      done
      "$KUBECTL" set image -f "$file" "${set_args[@]}" --local -o yaml |
        "$KUBECTL" apply -f -
    else
      printf '[deploy-glasslab-v2] applying %s\n' "$file"
      "$KUBECTL" apply -f "$file"
    fi
  done < <(find "$dir" -maxdepth 1 -type f -name '*.yaml' ! -name '*.example.yaml' | sort)
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --help|-h)
      usage
      exit 0
      ;;
    *)
      printf '[deploy-glasslab-v2] unknown argument: %s\n' "$1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

need_cmd "$KUBECTL"

# Live *.local.yaml manifests are applied below and must never be readable or
# writable by the checkout's group (which includes every contributor account).
# Refuse before any cluster mutation when one is not owner-only.
printf '[deploy-glasslab-v2] checking local Secret manifest permissions\n'
"$ROOT_DIR/scripts/check-secret-permissions.sh" "$LOCAL_SECRETS_DIR"

# The manifests carry placeholder/pinned image tags; deploy must use the
# checked-out commit so a plain deploy cannot ship stale code.
if [[ -z "$IMAGE_TAG" ]]; then
  need_cmd git
  if ! IMAGE_TAG="$(git -C "$ROOT_DIR" rev-parse --verify HEAD 2>/dev/null)" || [[ -z "$IMAGE_TAG" ]]; then
    printf '[deploy-glasslab-v2] ERROR: cannot resolve the checked-out commit SHA (git rev-parse HEAD); refusing to deploy service images that cannot be pinned\n' >&2
    exit 1
  fi
fi
if [[ ! "$IMAGE_TAG" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]]; then
  printf '[deploy-glasslab-v2] invalid image tag: %s\n' "$IMAGE_TAG" >&2
  exit 1
fi

printf '[deploy-glasslab-v2] validating workflow registry definitions\n'
"$ROOT_DIR/scripts/seed-registry.sh"

apply_yaml_dir "$MANIFEST_ROOT/namespaces"
apply_yaml_dir "$MANIFEST_ROOT/priorityclasses"
apply_yaml_dir "$MANIFEST_ROOT/secrets"
apply_yaml_dir "$MANIFEST_ROOT/config"
apply_yaml_dir "$MANIFEST_ROOT/postgres"
apply_yaml_dir "$MANIFEST_ROOT/nats"
apply_yaml_dir "$MANIFEST_ROOT/minio"
apply_service_dir "$MANIFEST_ROOT/intake-agent" \
  10-deployment.yaml ghcr.io/ccny-glasslab/glasslab-intake-agent intake-agent
apply_service_dir "$MANIFEST_ROOT/interpretation-agent" \
  10-deployment.yaml ghcr.io/ccny-glasslab/glasslab-interpretation-agent interpretation-agent
apply_service_dir "$MANIFEST_ROOT/assessment-agent" \
  10-deployment.yaml ghcr.io/ccny-glasslab/glasslab-assessment-agent assessment-agent
apply_service_dir "$MANIFEST_ROOT/design-agent" \
  10-deployment.yaml ghcr.io/ccny-glasslab/glasslab-design-agent design-agent
apply_service_dir "$MANIFEST_ROOT/schedule-worker" \
  10-deployment.yaml ghcr.io/ccny-glasslab/glasslab-schedule-worker schedule-worker
apply_service_dir "$MANIFEST_ROOT/workflow-api" \
  20-deployment.yaml ghcr.io/ccny-glasslab/glasslab-workflow-api workflow-api
apply_service_dir "$MANIFEST_ROOT/research-orchestrator" \
  20-deployment.yaml ghcr.io/ccny-glasslab/glasslab-research-orchestrator orchestrator opencode
