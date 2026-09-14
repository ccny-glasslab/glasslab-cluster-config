#!/usr/bin/env bash
set -euo pipefail

NAMESPACE="${GLASSLAB_V2_NAMESPACE:-glasslab-v2}"
DEPLOYMENT="${GLASSLAB_WORKFLOW_API_DEPLOYMENT:-glasslab-workflow-api}"
LOCAL_PORT="${GLASSLAB_WORKFLOW_API_LOCAL_PORT:-18080}"
SSH_TARGET="${GLASSLAB_WORKFLOW_API_SSH_TARGET:-}"

usage() {
  cat <<'USAGE'
Usage: check-workflow-api-provenance.sh [--target <ssh-host>] [--local]

Print the live workflow-api deployment image and /healthz provenance.

By default the script uses local kubectl when it can reach the configured
namespace, otherwise it falls back to ssh target glasslab-provisioner.
USAGE
}

quote() {
  printf '%q' "$1"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --target)
      SSH_TARGET="$2"
      shift 2
      ;;
    --local)
      SSH_TARGET=""
      shift
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      printf '[check-workflow-api-provenance] unknown argument: %s\n' "$1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

if [[ -z "$SSH_TARGET" ]] && ! kubectl get namespace "$NAMESPACE" >/dev/null 2>&1; then
  SSH_TARGET="glasslab-provisioner"
fi

run_shell() {
  if [[ -n "$SSH_TARGET" ]]; then
    ssh "$SSH_TARGET" "$1"
  else
    bash -lc "$1"
  fi
}

q_namespace="$(quote "$NAMESPACE")"
q_deployment="$(quote "$DEPLOYMENT")"
q_port="$(quote "$LOCAL_PORT")"

image_ref="$(run_shell "kubectl -n $q_namespace get deploy $q_deployment -o jsonpath='{.spec.template.spec.containers[0].image}'")"
pod_name="$(run_shell "kubectl -n $q_namespace get pods -l app.kubernetes.io/name=$q_deployment -o jsonpath='{.items[0].metadata.name}'")"

healthz_json="$(run_shell "LOG=\$(mktemp); kubectl -n $q_namespace port-forward svc/$q_deployment $q_port:8080 >\$LOG 2>&1 & PF=\$!; sleep 2; curl -fsS http://127.0.0.1:$q_port/healthz; RC=\$?; kill \$PF >/dev/null 2>&1 || true; rm -f \$LOG; exit \$RC")"

printf 'deployment_image=%s\n' "$image_ref"
printf 'pod=%s\n' "$pod_name"
printf 'healthz=%s\n' "$(printf '%s' "$healthz_json" | tr -d '\n')"
