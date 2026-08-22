#!/usr/bin/env bash
set -euo pipefail
BASE_URL="${VLLM_BASE_URL:-http://127.0.0.1:8000/v1}"
: "${VLLM_API_KEY:?VLLM_API_KEY must be set}"
MODEL_NAME="${VLLM_MODEL_NAME:-Qwen/Qwen3-4B-Instruct-2507}"

if [[ "$VLLM_API_KEY" == *$'\n'* || "$VLLM_API_KEY" == *$'\r'* ]]; then
  printf 'VLLM_API_KEY must not contain a newline\n' >&2
  exit 1
fi

if ! printf '%s' "$VLLM_API_KEY" | python3 -c '
import sys

normalized = sys.stdin.read().strip().lower()
if not normalized or normalized.startswith("change-me") or normalized in {
    "redacted",
    "<redacted>",
    "replace-me",
}:
    raise SystemExit(1)
'; then
  printf 'VLLM_API_KEY must contain a non-placeholder value\n' >&2
  exit 1
fi

umask 077
CURL_CONFIG="$(mktemp)"
cleanup() {
  rm -f -- "$CURL_CONFIG"
}
trap cleanup EXIT
CURL_API_KEY="${VLLM_API_KEY//\\/\\\\}"
CURL_API_KEY="${CURL_API_KEY//\"/\\\"}"
CURL_API_KEY="${CURL_API_KEY//$'\t'/\\t}"
printf 'header = "Authorization: Bearer %s"\n' "$CURL_API_KEY" > "$CURL_CONFIG"
unset CURL_API_KEY

curl -sS --config "$CURL_CONFIG" "${BASE_URL}/models"
printf '\n'
curl -sS \
  --config "$CURL_CONFIG" \
  -H 'Content-Type: application/json' \
  -d "{\"model\":\"${MODEL_NAME}\",\"messages\":[{\"role\":\"user\",\"content\":\"Return valid JSON only: {\\\"ok\\\": true}\"}],\"temperature\":0.0,\"max_tokens\":64}" \
  "${BASE_URL}/chat/completions"
printf '\n'
