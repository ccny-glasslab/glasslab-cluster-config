#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NAMESPACE="glasslab-agents"
SECRET_NAME="glasslab-agent-secrets"
SECRET_KEY="VLLM_API_KEY"
SECRETS_FILE="${GLASSLAB_VLLM_SECRET_FILE:-$ROOT/kubeadm/agent-stack/12-agent-secrets.yaml}"

validate_secret_file() {
  python3 - "$SECRETS_FILE" "$SECRET_NAME" "$NAMESPACE" "$SECRET_KEY" <<'PY'
import base64
import binascii
import sys
from pathlib import Path

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

value = None
string_data = secret.get("stringData")
if isinstance(string_data, dict):
    value = string_data.get(required_key)
data = secret.get("data")
if value is None and isinstance(data, dict) and isinstance(data.get(required_key), str):
    try:
        value = base64.b64decode(data[required_key], validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        raise SystemExit(1)

if not isinstance(value, str) or not value.strip():
    raise SystemExit(1)

normalized = value.strip().lower()
if normalized.startswith("change-me") or normalized in {"redacted", "<redacted>", "replace-me"}:
    raise SystemExit(1)
PY
}

confirm_live_secret() {
  kubectl get secret "$SECRET_NAME" -n "$NAMESPACE" \
    -o "jsonpath={.data.${SECRET_KEY}}" 2>/dev/null | grep -q '[^[:space:]]'
}

APPLY_LOCAL_SECRET=false
if [[ -f "$SECRETS_FILE" ]]; then
  if ! validate_secret_file; then
    printf '[deploy-vllm] Secret file does not match the required live Secret contract\n' >&2
    exit 1
  fi
  APPLY_LOCAL_SECRET=true
elif confirm_live_secret; then
  printf '[deploy-vllm] using pre-existing cluster Secret %s/%s\n' "$NAMESPACE" "$SECRET_NAME"
else
  printf '[deploy-vllm] no live Secret is available; set GLASSLAB_VLLM_SECRET_FILE or create %s/%s\n' "$NAMESPACE" "$SECRET_NAME" >&2
  exit 1
fi

kubectl apply -f "$ROOT/kubeadm/agent-stack/00-namespace.yaml"
if [[ "$APPLY_LOCAL_SECRET" == true ]]; then
  kubectl apply -f "$SECRETS_FILE"
  if ! confirm_live_secret; then
    printf '[deploy-vllm] required live Secret/key is unavailable after apply\n' >&2
    exit 1
  fi
fi
kubectl apply -f "$ROOT/kubeadm/agent-stack/02-persistent-volume-claims.yaml"
kubectl apply -f "$ROOT/kubeadm/agent-stack/10-vllm-config.yaml"
kubectl apply -f "$ROOT/kubeadm/agent-stack/11-vllm-deployment.yaml"
