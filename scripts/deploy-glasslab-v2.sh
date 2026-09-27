#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MANIFEST_ROOT="$ROOT_DIR/kubeadm/glasslab-v2"
LOCAL_SECRETS_DIR="${GLASSLAB_V2_LOCAL_SECRETS_DIR:-$MANIFEST_ROOT/secrets}"
KUBECTL="${KUBECTL:-kubectl}"
NAMESPACE="${GLASSLAB_V2_NAMESPACE:-glasslab-v2}"
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

Environment:
  GLASSLAB_V2_LOCAL_SECRETS_DIR
      Directory scanned for the *.local.yaml permission preflight (default:
      kubeadm/glasslab-v2/secrets)
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

printf '[deploy-glasslab-v2] validating workflow registry definitions\n'
"$ROOT_DIR/scripts/seed-registry.sh"

apply_yaml_dir "$MANIFEST_ROOT/namespaces"
apply_yaml_dir "$MANIFEST_ROOT/priorityclasses"
apply_yaml_dir "$MANIFEST_ROOT/secrets"
apply_yaml_dir "$MANIFEST_ROOT/config"
apply_yaml_dir "$MANIFEST_ROOT/postgres"
apply_yaml_dir "$MANIFEST_ROOT/nats"
apply_yaml_dir "$MANIFEST_ROOT/minio"
apply_yaml_dir "$MANIFEST_ROOT/intake-agent"
apply_yaml_dir "$MANIFEST_ROOT/interpretation-agent"
apply_yaml_dir "$MANIFEST_ROOT/assessment-agent"
apply_yaml_dir "$MANIFEST_ROOT/design-agent"
apply_yaml_dir "$MANIFEST_ROOT/schedule-worker"
apply_yaml_dir "$MANIFEST_ROOT/workflow-api"
apply_yaml_dir "$MANIFEST_ROOT/research-orchestrator"
