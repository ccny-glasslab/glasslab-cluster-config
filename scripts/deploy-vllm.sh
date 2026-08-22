#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NAMESPACE="glasslab-agents"
SECRET_NAME="glasslab-agent-secrets"
SECRETS_FILE="${GLASSLAB_VLLM_SECRET_FILE:-$ROOT/kubeadm/agent-stack/12-agent-secrets.yaml}"
read -r -a SECRET_KEYS <<< "${GLASSLAB_AGENT_SECRET_REQUIRED_KEYS:-VLLM_API_KEY}"

for secret_key in "${SECRET_KEYS[@]}"; do
  if [[ ! "$secret_key" =~ ^[A-Za-z0-9._-]+$ ]]; then
    printf '[deploy-vllm] invalid required Secret key name\n' >&2
    exit 1
  fi
done

validate_secret_file() {
  python3 - "$SECRETS_FILE" "$SECRET_NAME" "$NAMESPACE" "${SECRET_KEYS[@]}" <<'PY'
import base64
import binascii
import sys
from pathlib import Path

import yaml


path = Path(sys.argv[1])
expected_name, expected_namespace = sys.argv[2:4]
required_keys = sys.argv[4:]
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

string_data = secret.get("stringData")
data = secret.get("data")
for required_key in required_keys:
    value = string_data.get(required_key) if isinstance(string_data, dict) else None
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
  local encoded_key secret_key
  for secret_key in "${SECRET_KEYS[@]}"; do
    if ! encoded_key="$(
      kubectl get secret "$SECRET_NAME" -n "$NAMESPACE" \
        -o "jsonpath={.data.${secret_key}}" 2>/dev/null
    )"; then
      return 1
    fi

    if ! printf '%s' "$encoded_key" | python3 -c '
import base64
import binascii
import sys

try:
    value = base64.b64decode(sys.stdin.buffer.read(), validate=True).decode("utf-8")
except (binascii.Error, UnicodeDecodeError):
    raise SystemExit(1)

normalized = value.strip().lower()
if not normalized or normalized.startswith("change-me") or normalized in {
    "redacted",
    "<redacted>",
    "replace-me",
}:
    raise SystemExit(1)
'; then
      return 1
    fi
  done
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
