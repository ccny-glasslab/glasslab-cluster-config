#!/usr/bin/env bash
set -euo pipefail

# Run contrastive learning experiment on GPU runner

RUNNER_ENDPOINT="${RUNNER_ENDPOINT:-http://glasslab-gpu-runner.glasslab-v2.svc.cluster.local:52415}"
EXPERIMENT_ID="${EXPERIMENT_ID:-contrastive-cifar100-$(date +%s)}"
CONFIG_PATH="${CONFIG_PATH:-configs/search_spaces/cifar100_contrastive_v0.yaml}"

usage() {
  cat <<'USAGE'
Usage: run-contrastive-experiment.sh [--runner-endpoint <endpoint>] [--experiment-id <id>] [--config <path>]

Run contrastive learning experiment on GPU runner.

Options:
  --runner-endpoint  GPU runner endpoint (default: http://glasslab-gpu-runner.glasslab-v2.svc.cluster.local:52415)
  --experiment-id    Experiment ID (default: contrastive-cifar100-<timestamp>)
  --config           Path to config file (default: configs/search_spaces/cifar100_contrastive_v0.yaml)
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --runner-endpoint)
      RUNNER_ENDPOINT="$2"
      shift 2
      ;;
    --experiment-id)
      EXPERIMENT_ID="$2"
      shift 2
      ;;
    --config)
      CONFIG_PATH="$2"
      shift 2
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      printf '[run-contrastive-experiment] unknown argument: %s\n' "$1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

# Submit experiment
printf '[run-contrastive-experiment] submitting experiment: %s\n' "${EXPERIMENT_ID}"

if [[ ! "${EXPERIMENT_ID}" =~ ^[A-Za-z0-9._-]+$ ]]; then
  printf '[run-contrastive-experiment] invalid experiment id: %s\n' "${EXPERIMENT_ID}" >&2
  exit 1
fi

payload="$(jq -n \
  --arg experiment_id "${EXPERIMENT_ID}" \
  --arg config_path "${CONFIG_PATH}" \
  '{experiment_id: $experiment_id, config_path: $config_path, runner_image: "glasslab/runner:gpu-v1"}')"

curl -fsS -X POST "${RUNNER_ENDPOINT}/runs" \
  -H 'Content-Type: application/json' \
  -d "$payload" | tee "/tmp/${EXPERIMENT_ID}_response.json"

printf '\n[run-contrastive-experiment] experiment submitted. Checking status...\n'

# Monitor
RUN_STATUS="pending"
while [[ "${RUN_STATUS}" == "pending" || "${RUN_STATUS}" == "running" ]]; do
  STATUS_RESPONSE="$(curl -fsS "${RUNNER_ENDPOINT}/runs/${EXPERIMENT_ID}" 2>/dev/null || echo '{"status": "unknown"}')"
  RUN_STATUS="$(printf '%s' "${STATUS_RESPONSE}" | jq -r '.status // "unknown"' 2>/dev/null || echo "unknown")"

  printf '[run-contrastive-experiment] status: %s\n' "${RUN_STATUS}"

  case "${RUN_STATUS}" in
    completed)
      printf '[run-contrastive-experiment] experiment completed. Metrics:\n'
      curl -fsS "${RUNNER_ENDPOINT}/runs/${EXPERIMENT_ID}/metrics" | jq .
      break
      ;;
    failed)
      printf '[run-contrastive-experiment] experiment failed!\n'
      curl -fsS "${RUNNER_ENDPOINT}/runs/${EXPERIMENT_ID}/error" | jq .
      break
      ;;
    unknown)
      printf '[run-contrastive-experiment] error: could not determine run status\n' >&2
      exit 1
      ;;
  esac

  sleep 10
done

printf '[run-contrastive-experiment] done\n'
