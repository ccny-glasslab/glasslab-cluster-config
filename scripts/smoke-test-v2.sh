#!/usr/bin/env bash
set -euo pipefail

KUBECTL="${KUBECTL:-kubectl}"
CURL="${CURL:-curl}"
NAMESPACE="${GLASSLAB_V2_NAMESPACE:-glasslab-v2}"
HEALTH_PORT="${GLASSLAB_V2_HEALTH_PORT:-18081}"
INCLUDE_BOUNDED_AGENTS=false
EXPECTED_SERVICES=(glasslab-workflow-api glasslab-postgres glasslab-nats glasslab-minio)
PORT_FORWARD_PID=""
PORT_FORWARD_LOG=""

usage() {
  cat <<'USAGE'
Usage: smoke-test-v2.sh [--include-bounded-agents]

Validate the core Glasslab v2 services by default.
Bounded-agent checks are optional until those Deployments are intentionally applied.
USAGE
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    printf '[smoke-test-v2] missing command: %s\n' "$1" >&2
    exit 1
  }
}

cleanup() {
  if [[ -n "$PORT_FORWARD_PID" ]]; then
    kill "$PORT_FORWARD_PID" >/dev/null 2>&1 || true
    wait "$PORT_FORWARD_PID" 2>/dev/null || true
  fi
  if [[ -n "$PORT_FORWARD_LOG" && -f "$PORT_FORWARD_LOG" ]]; then
    rm -f "$PORT_FORWARD_LOG"
  fi
}
trap cleanup EXIT

while [[ $# -gt 0 ]]; do
  case "$1" in
    --include-bounded-agents)
      INCLUDE_BOUNDED_AGENTS=true
      EXPECTED_SERVICES+=(
        glasslab-intake-agent
        glasslab-interpretation-agent
        glasslab-assessment-agent
        glasslab-design-agent
        glasslab-schedule-worker
      )
      shift
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      printf '[smoke-test-v2] unknown argument: %s\n' "$1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

need_cmd "$KUBECTL"
need_cmd "$CURL"

printf '[smoke-test-v2] checking namespace %s\n' "$NAMESPACE"
"$KUBECTL" get namespace "$NAMESPACE" >/dev/null

printf '[smoke-test-v2] checking rollout status for core services\n'
"$KUBECTL" -n "$NAMESPACE" rollout status deployment/glasslab-nats --timeout=120s
"$KUBECTL" -n "$NAMESPACE" rollout status deployment/glasslab-minio --timeout=120s
"$KUBECTL" -n "$NAMESPACE" rollout status deployment/glasslab-workflow-api --timeout=120s
"$KUBECTL" -n "$NAMESPACE" rollout status statefulset/glasslab-postgres --timeout=120s

if [[ "$INCLUDE_BOUNDED_AGENTS" == true ]]; then
  "$KUBECTL" -n "$NAMESPACE" rollout status deployment/glasslab-intake-agent --timeout=120s
  "$KUBECTL" -n "$NAMESPACE" rollout status deployment/glasslab-interpretation-agent --timeout=120s
  "$KUBECTL" -n "$NAMESPACE" rollout status deployment/glasslab-assessment-agent --timeout=120s
  "$KUBECTL" -n "$NAMESPACE" rollout status deployment/glasslab-design-agent --timeout=120s
  "$KUBECTL" -n "$NAMESPACE" rollout status deployment/glasslab-schedule-worker --timeout=120s
fi

printf '[smoke-test-v2] checking service inventory\n'
"$KUBECTL" -n "$NAMESPACE" get deploy,statefulset,svc

for service in "${EXPECTED_SERVICES[@]}"; do
  "$KUBECTL" -n "$NAMESPACE" get service "$service" >/dev/null
  printf '[smoke-test-v2] dns %s.%s.svc.cluster.local\n' "$service" "$NAMESPACE"
done

PORT_FORWARD_LOG="$(mktemp)"
"$KUBECTL" -n "$NAMESPACE" port-forward svc/glasslab-workflow-api "${HEALTH_PORT}:8080" >"$PORT_FORWARD_LOG" 2>&1 &
PORT_FORWARD_PID="$!"

sleep 2
for _ in $(seq 1 20); do
  if "$CURL" -fsS "http://127.0.0.1:${HEALTH_PORT}/healthz" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

printf '[smoke-test-v2] workflow-api health response\n'
"$CURL" -fsS "http://127.0.0.1:${HEALTH_PORT}/healthz"
printf '\n'

if [[ "$INCLUDE_BOUNDED_AGENTS" == true ]]; then
  printf '[smoke-test-v2] bounded-agent rollout checks were included.\n'
else
  printf '[smoke-test-v2] bounded-agent checks skipped by default.\n'
fi
