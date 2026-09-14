#!/usr/bin/env bash
# Verify the live exo model-serving split and detect server reloads.
#
# The split-model layout (verified 2026-09-09):
#   .17:52417 -> mlx-community/Qwen3-Coder-Next-4bit            (structured/Beaker)
#   .18:52417 -> mlx-community/Qwen3-Next-80B-A3B-Thinking-4bit (reasoning/Honeydew)
#
# Each mlx_lm server reports its loaded model with a `created` epoch in
# /v1/models; a fresh `created` means the server (re)loaded weights, which is
# expensive and worth surfacing. Read-only: never starts a generation.
#
# Usage:
#   scripts/check-exo-model-split.sh
#   EXO_HOSTS="192.168.1.17 192.168.1.18" EXO_PORT=52417 scripts/check-exo-model-split.sh
set -euo pipefail

HOSTS="${EXO_HOSTS:-192.168.1.17 192.168.1.18}"
PORT="${EXO_PORT:-52417}"
TIMEOUT="${EXO_TIMEOUT:-8}"

# Expected model per host: host -> substring of the model id.
declare -A EXPECT
EXPECT[192.168.1.17]="Qwen3-Coder-Next-4bit"
EXPECT[192.168.1.18]="Qwen3-Next-80B-A3B-Thinking-4bit"

rc=0
for host in $HOSTS; do
  url="http://${host}:${PORT}/v1/models"
  start=$(date +%s.%N)
  body="$(curl -fsS -m "$TIMEOUT" "$url" 2>/dev/null || true)"
  end=$(date +%s.%N)
  latency="$(python3 -c "print(f'{$end-$start:.2f}')" 2>/dev/null || echo '?')"

  if [ -z "$body" ]; then
    echo "UNREACHABLE  ${host}:${PORT}  (latency ${latency}s)"
    rc=1
    continue
  fi

  parsed="$(printf '%s' "$body" | python3 -c '
import json, sys, datetime
try:
    data = json.load(sys.stdin)
except Exception:
    print("invalid-json"); raise SystemExit
models = [m["id"] for m in data.get("data", [])]
created = None
for m in data.get("data", []):
    if not m["id"].startswith("/private"):  # the canonical id, not the cache path
        created = m.get("created")
        break
if created:
    when = datetime.datetime.fromtimestamp(created, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
else:
    when = "?"
print(" | ".join(models))
print(when)
')"
  model_list="$(printf '%s\n' "$parsed" | sed -n 1p)"
  created_at="$(printf '%s\n' "$parsed" | sed -n 2p)"
  expected="${EXPECT[$host]:-}"

  ok="OK"
  if [ -n "$expected" ] && ! printf '%s' "$model_list" | grep -q "$expected"; then
    ok="MISMATCH (expected $expected)"
    rc=1
  fi
  echo "$ok  ${host}:${PORT}  model=[$model_list]  created=$created_at  latency=${latency}s"
done

exit "$rc"