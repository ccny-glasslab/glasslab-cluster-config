#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MANIFEST_DIR="${ROOT_DIR}/kubeadm/glasslab-v2/gpu-runner"
NAMESPACE="glasslab-v2"
SECRET_NAME="glasslab-v2-runner"
SECRET_KEY="GLASSLAB_RUNNER_STORE_POSTGRES_DSN"

usage() {
  cat <<'USAGE'
Usage: deploy-gpu-runner.sh [--apply] [--delete] [--dry-run] [--status]

Deploy GPU runner to Kubernetes cluster.

Set GLASSLAB_GPU_RUNNER_SECRET_FILE to an existing file ending in .local.yaml,
or create the glasslab-v2-runner Secret in namespace glasslab-v2 beforehand.

Options:
  --apply     Apply deployment manifests (default)
  --delete    Delete GPU runner deployment
  --dry-run   Show what would be applied without applying
  --status    Show current deployment status
USAGE
}

ACTION="apply"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply)
      ACTION="apply"
      shift
      ;;
    --delete)
      ACTION="delete"
      shift
      ;;
    --dry-run)
      ACTION="dry-run"
      shift
      ;;
    --status)
      ACTION="status"
      shift
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      printf '[deploy-gpu-runner] unknown argument: %s\n' "$1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

validate_local_secret_file() {
  local secret_file="$1"

  if ! python3 - "$secret_file" "$SECRET_NAME" "$NAMESPACE" "$SECRET_KEY" <<'PY'
import sys
import base64
import binascii
from pathlib import Path
from urllib.parse import urlsplit

import yaml


path = Path(sys.argv[1])
expected_name, expected_namespace, required_key = sys.argv[2:]
try:
    documents = [document for document in yaml.safe_load_all(path.read_text(encoding="utf-8")) if document is not None]
except (OSError, UnicodeError, yaml.YAMLError):
    raise SystemExit(1)

if len(documents) != 1 or not isinstance(documents[0], dict):
    raise SystemExit(1)

secret = documents[0]
metadata = secret.get("metadata")
if (
    secret.get("apiVersion") != "v1"
    or secret.get("kind") != "Secret"
    or not isinstance(metadata, dict)
    or metadata.get("name") != expected_name
    or metadata.get("namespace") != expected_namespace
):
    raise SystemExit(1)

def valid_postgres_dsn(value):
    if not isinstance(value, str) or value != value.strip() or not value:
        return False
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        return False
    normalized = value.lower()
    if normalized.startswith("change-me") or normalized in {
        "redacted",
        "<redacted>",
        "replace-me",
    }:
        return False
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme in {"postgres", "postgresql"}
        and parsed.hostname is not None
        and parsed.username is not None
        and parsed.password is not None
        and parsed.path not in {"", "/"}
        and (port is None or 0 < port < 65536)
    )


string_data = secret.get("stringData")
data = secret.get("data")
plain_value = string_data.get(required_key) if isinstance(string_data, dict) else None
encoded_value = data.get(required_key) if isinstance(data, dict) else None
if plain_value is not None and encoded_value is not None:
    raise SystemExit(1)
if plain_value is None and isinstance(encoded_value, str):
    try:
        plain_value = base64.b64decode(encoded_value, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        raise SystemExit(1)
if valid_postgres_dsn(plain_value):
    raise SystemExit(0)

raise SystemExit(1)
PY
  then
    printf '[deploy-gpu-runner] GPU runner secret file does not match the required Secret contract\n' >&2
    return 1
  fi
}

confirm_live_secret() {
  if ! kubectl get secret "$SECRET_NAME" -n "$NAMESPACE" \
    -o "jsonpath={.data.${SECRET_KEY}}" 2>/dev/null | python3 -c '
import base64
import binascii
import sys
from urllib.parse import urlsplit

try:
    value = base64.b64decode(sys.stdin.buffer.read(), validate=True).decode("utf-8")
except (binascii.Error, UnicodeDecodeError):
    raise SystemExit(1)

if value != value.strip() or not value:
    raise SystemExit(1)
if any(ord(character) < 32 or ord(character) == 127 for character in value):
    raise SystemExit(1)
normalized = value.lower()
if normalized.startswith("change-me") or normalized in {
    "redacted",
    "<redacted>",
    "replace-me",
}:
    raise SystemExit(1)
try:
    parsed = urlsplit(value)
    port = parsed.port
except ValueError:
    raise SystemExit(1)
if not (
    parsed.scheme in {"postgres", "postgresql"}
    and parsed.hostname is not None
    and parsed.username is not None
    and parsed.password is not None
    and parsed.path not in {"", "/"}
    and (port is None or 0 < port < 65536)
):
    raise SystemExit(1)
'; then
    printf '[deploy-gpu-runner] required live GPU runner Secret/key is unavailable\n' >&2
    return 1
  fi
}

prepare_secret() {
  local mode="$1"
  local secret_file="${GLASSLAB_GPU_RUNNER_SECRET_FILE:-}"

  if [[ -n "$secret_file" ]]; then
    if [[ "$secret_file" != *.local.yaml ]]; then
      printf '[deploy-gpu-runner] GLASSLAB_GPU_RUNNER_SECRET_FILE must end in .local.yaml\n' >&2
      return 1
    fi
    if [[ ! -f "$secret_file" ]]; then
      printf '[deploy-gpu-runner] GPU runner secret file does not exist: %s\n' "$secret_file" >&2
      return 1
    fi
    validate_local_secret_file "$secret_file"

    if [[ "$mode" == "apply" ]]; then
      printf '[deploy-gpu-runner] applying local GPU runner Secret...\n'
      kubectl apply -f "$secret_file"
      confirm_live_secret
    else
      printf '[deploy-gpu-runner] validating local GPU runner Secret...\n'
      kubectl apply -f "$secret_file" --dry-run=client
    fi
    return
  fi

  if confirm_live_secret; then
    printf '[deploy-gpu-runner] using pre-existing cluster Secret %s/%s\n' "$NAMESPACE" "$SECRET_NAME"
    return
  fi

  printf '[deploy-gpu-runner] no GPU runner Secret is available; set GLASSLAB_GPU_RUNNER_SECRET_FILE or create %s/%s\n' "$NAMESPACE" "$SECRET_NAME" >&2
  return 1
}

apply_manifests() {
  prepare_secret apply
  printf '[deploy-gpu-runner] applying GPU runner manifests...\n'
  kubectl apply -f "${MANIFEST_DIR}/00-all.yaml"
}

delete_manifests() {
  printf '[deploy-gpu-runner] deleting GPU runner deployment...\n'
  kubectl delete -f "${MANIFEST_DIR}/00-all.yaml" --ignore-not-found=true
}

dry_run() {
  prepare_secret dry-run
  printf '[deploy-gpu-runner] dry-run of GPU runner manifests:\n'
  kubectl apply -f "${MANIFEST_DIR}/00-all.yaml" --dry-run=client
}

show_status() {
  printf '[deploy-gpu-runner] GPU runner deployment status:\n'
  kubectl get pods -n glasslab-v2 -l app.kubernetes.io/name=glasslab-gpu-runner -o wide 2>/dev/null || printf 'No pods found\n'
  printf '\nServices:\n'
  kubectl get svc -n glasslab-v2 glasslab-gpu-runner 2>/dev/null || printf 'No service found\n'
  printf '\nPVCs:\n'
  kubectl get pvc -n glasslab-v2 runner-model-cache 2>/dev/null || printf 'No PVC found\n'
}

case "$ACTION" in
  apply)
    apply_manifests
    ;;
  delete)
    delete_manifests
    ;;
  dry-run)
    dry_run
    ;;
  status)
    show_status
    ;;
esac

printf '[deploy-gpu-runner] done\n'
