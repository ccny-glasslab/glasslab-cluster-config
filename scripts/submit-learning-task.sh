#!/usr/bin/env bash
set -euo pipefail

KUBECTL="${KUBECTL:-kubectl}"
CURL="${CURL:-curl}"
NAMESPACE="${GLASSLAB_V2_NAMESPACE:-glasslab-v2}"
LOCAL_PORT="${GLASSLAB_WORKFLOW_API_PORT:-18081}"
BASE_URL="${GLASSLAB_WORKFLOW_API_BASE_URL:-}"
PORT_FORWARD_PID=""
PORT_FORWARD_LOG=""

usage() {
  cat <<'USAGE'
Usage:
  submit-learning-task.sh "objective"

Submit one bounded metric-search learning task through workflow-api.

Environment overrides:
  METRIC_SEARCH_CONFIG           configs/search_spaces/art_metric_baseline.yaml
  METRIC_SEARCH_TRAIN_URI        s3://datasets/artbench/train.parquet
  METRIC_SEARCH_VAL_URI          empty by default
  METRIC_SEARCH_TEST_URI         empty by default
  METRIC_SEARCH_MAX_EPOCHS       1
  METRIC_SEARCH_MAX_MINUTES      30
  METRIC_SEARCH_PRIMARY_METRIC   composite_score
  METRIC_SEARCH_SUBMITTED_BY     submit-learning-task
  GLASSLAB_WORKFLOW_API_BASE_URL use direct API URL instead of kubectl port-forward
USAGE
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    printf '[submit-learning-task] missing command: %s\n' "$1" >&2
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

start_port_forward() {
  PORT_FORWARD_LOG="$(mktemp)"
  "$KUBECTL" -n "$NAMESPACE" port-forward svc/glasslab-workflow-api "${LOCAL_PORT}:8080" >"$PORT_FORWARD_LOG" 2>&1 &
  PORT_FORWARD_PID="$!"
  for _ in $(seq 1 30); do
    if "$CURL" -fsS "http://127.0.0.1:${LOCAL_PORT}/healthz" >/dev/null 2>&1; then
      BASE_URL="http://127.0.0.1:${LOCAL_PORT}"
      return 0
    fi
    sleep 1
  done
  printf '[submit-learning-task] workflow-api port-forward did not become ready\n' >&2
  cat "$PORT_FORWARD_LOG" >&2 || true
  exit 1
}

pretty_print() {
  python3 -c 'import json,sys; print(json.dumps(json.loads(sys.stdin.read()), indent=2))'
}

build_payload() {
  python3 - "$@" <<'PY'
import json
import os
import shlex
import sys

objective = sys.argv[1]
config_path = os.environ.get('METRIC_SEARCH_CONFIG', 'configs/search_spaces/art_metric_baseline.yaml')
train_uri = os.environ.get('METRIC_SEARCH_TRAIN_URI', 's3://datasets/artbench/train.parquet')
val_uri = os.environ.get('METRIC_SEARCH_VAL_URI', '')
test_uri = os.environ.get('METRIC_SEARCH_TEST_URI', '')
max_epochs = int(os.environ.get('METRIC_SEARCH_MAX_EPOCHS', '1'))
max_minutes = int(os.environ.get('METRIC_SEARCH_MAX_MINUTES', '30'))
primary_metric = os.environ.get('METRIC_SEARCH_PRIMARY_METRIC', 'composite_score')
submitted_by = os.environ.get('METRIC_SEARCH_SUBMITTED_BY', 'submit-learning-task')

dataset_bindings = {}
if train_uri:
    dataset_bindings['train_uri'] = train_uri
if val_uri:
    dataset_bindings['val_uri'] = val_uri
if test_uri:
    dataset_bindings['test_uri'] = test_uri

payload = {
    'objective': objective,
    'experiment_type': 'gpu-training-job',
    'workload_id': 'metric-search-v0',
    'entrypoint': [
        'sh',
        '-lc',
        (
            'python3 scripts/run_experiment.py '
            f'--config {shlex.quote(config_path)} '
            '--output-dir /mnt/artifacts/$GLASSLAB_RUNNER_EXPERIMENT_ID'
        ),
    ],
    'config_payload': {
        'workflow_family': 'metric-search',
        'config_path': config_path,
    },
    'dataset_bindings': dataset_bindings,
    'budget': {
        'max_epochs': max_epochs,
        'max_wallclock_minutes': max_minutes,
    },
    'metric_contract': {
        'primary_metric': primary_metric,
        'higher_is_better': True,
    },
    'submitted_by': submitted_by,
}

print(json.dumps(payload))
PY
}

main() {
  need_cmd "$CURL"
  need_cmd python3

  local objective="${1:-}"
  if [[ -z "$objective" || "$objective" == "-h" || "$objective" == "--help" ]]; then
    usage
    [[ -n "$objective" ]] && exit 0
    exit 2
  fi

  if [[ -z "$BASE_URL" ]]; then
    need_cmd "$KUBECTL"
    start_port_forward
  fi

  local payload
  payload="$(build_payload "$objective")"

  "$CURL" -fsS -X POST "${BASE_URL}/experiments/runs" \
    -H 'content-type: application/json' \
    --data "$payload" \
    | pretty_print
}

main "$@"
