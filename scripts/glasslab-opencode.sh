#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

OPENCODE_BIN="${OPENCODE_BIN:-}"
if [[ -z "$OPENCODE_BIN" ]]; then
  if command -v opencode >/dev/null 2>&1; then
    OPENCODE_BIN="$(command -v opencode)"
  else
    OPENCODE_BIN="${HOME}/.npm-global/bin/opencode"
  fi
fi

API_BASE="${GLASSLAB_EXO_API_BASE:-http://192.168.1.17:52415}"
MODEL="${GLASSLAB_OPENCODE_MODEL:-mlx-community/Qwen3-Coder-Next-4bit}"
SSH_TARGET="${GLASSLAB_EXO_SSH_TARGET:-glasslab-exo17}"
TUNNEL_PORT="${GLASSLAB_EXO_TUNNEL_PORT:-65415}"
TUNNEL_PID=""
CONFIG_PATH=""

if [[ ! -x "$OPENCODE_BIN" ]]; then
  printf '[glasslab-opencode] opencode not executable: %s\n' "$OPENCODE_BIN" >&2
  exit 1
fi

cleanup() {
  if [[ -n "$TUNNEL_PID" ]]; then
    kill "$TUNNEL_PID" 2>/dev/null || true
    wait "$TUNNEL_PID" 2>/dev/null || true
  fi
  if [[ -n "$CONFIG_PATH" ]]; then
    rm -f "$CONFIG_PATH"
  fi
}
trap cleanup EXIT INT TERM

if ! curl -fsS --max-time 5 "${API_BASE}/v1/models" >/dev/null 2>&1; then
  API_BASE="http://127.0.0.1:${TUNNEL_PORT}"
  ssh -N -o BatchMode=yes -o ExitOnForwardFailure=yes \
    -o ServerAliveInterval=30 \
    -L "127.0.0.1:${TUNNEL_PORT}:127.0.0.1:52415" \
    "$SSH_TARGET" </dev/null >/dev/null 2>&1 &
  TUNNEL_PID=$!
  for _ in $(seq 1 50); do
    if curl -fsS --max-time 2 "${API_BASE}/v1/models" >/dev/null 2>&1; then
      break
    fi
    if ! kill -0 "$TUNNEL_PID" 2>/dev/null; then
      printf '[glasslab-opencode] SSH tunnel to %s exited before exo became ready\n' "$SSH_TARGET" >&2
      exit 1
    fi
    sleep 0.2
  done
fi

STATE_JSON="$(curl -fsS --max-time 10 "${API_BASE}/state")" || {
  printf '[glasslab-opencode] exo API unavailable at %s\n' "$API_BASE" >&2
  exit 1
}
if ! jq -e '
  (.topology.nodes | length) >= 2 and
  ([.topology.connections | to_entries[]?.value | to_entries[]?.value[]?
    | select(.sourceRdmaIface? == "rdma_en5" and .sinkRdmaIface? == "rdma_en5")]
    | length) >= 1
' >/dev/null <<<"$STATE_JSON"; then
  printf '[glasslab-opencode] exo does not have a healthy two-node RDMA topology\n' >&2
  exit 1
fi

PROBE_PAYLOAD="$(jq -nc --arg model "$MODEL" '{
  model: $model,
  messages: [{role: "user", content: "ping"}],
  max_tokens: 1,
  temperature: 0,
  stream: false
}')"
if ! curl -fsS --max-time 180 -H 'Content-Type: application/json' \
  --data-binary "$PROBE_PAYLOAD" "${API_BASE}/v1/chat/completions" >/dev/null; then
  printf '[glasslab-opencode] exo model instance is not ready: %s\n' "$MODEL" >&2
  exit 1
fi

CONFIG_PATH="$(mktemp)"
jq -n --arg model "$MODEL" --arg base_url "${API_BASE}/v1" '{
  "$schema": "https://opencode.ai/config.json",
  model: ("exo/" + $model),
  small_model: ("exo/" + $model),
  default_agent: "build",
  share: "disabled",
  autoupdate: false,
  provider: {
    exo: {
      npm: "@ai-sdk/openai-compatible",
      name: "Glasslab Exo",
      options: {baseURL: $base_url},
      models: {($model): {name: $model}}
    }
  }
}' >"$CONFIG_PATH"
chmod 600 "$CONFIG_PATH"
export OPENCODE_CONFIG="$CONFIG_PATH"

cd "$REPO_DIR"
if (($#)); then
  "$OPENCODE_BIN" run -m "exo/$MODEL" "$*"
else
  "$OPENCODE_BIN" -m "exo/$MODEL"
fi
