#!/usr/bin/env bash
# Measure the context/overhead budget of a standalone mlx_lm Mac server.
#
# Answers "how much context can we afford?" for the agent turns:
#   total RAM - model weights - baseline = KV-cache budget for context tokens.
#
# Read-only by default. With --probe it makes TWO tiny 1-token completions
# (short + long prompt) and measures the server RSS delta to derive the
# empirical bytes/token KV cost. The probe is cheap but touches the server.
#
# Usage:
#   scripts/measure-model-context-budget.sh 192.168.1.17 [--probe]
#   scripts/measure-model-context-budget.sh 192.168.1.18 [--probe]
set -euo pipefail

HOST="${1:-192.168.1.17}"
PORT="${EXO_PORT:-52417}"
PROBE=0
[ "${2:-}" = "--probe" ] && PROBE=1

echo "== $HOST =="
# --- total RAM (bytes) ---
mem="$(ssh -o BatchMode=yes -o ConnectTimeout=8 "glasslab-${HOST##*.}" 'sysctl -n hw.memsize' 2>/dev/null || true)"
if [ -n "$mem" ]; then
  echo "total RAM:        $(python3 -c "print(f'{$mem/1024/1024/1024:.0f} GB')")"
else
  echo "total RAM:        unreachable"
fi

# --- memory pressure / free ---
echo "memory pressure:  $(ssh -o BatchMode=yes -o ConnectTimeout=8 "glasslab-${HOST##*.}" 'memory_pressure -Q 2>/dev/null | tail -2 | tr "\n" " "' 2>/dev/null || echo unreachable)"

# --- model server process RSS ---
ssh_out="$(ssh -o BatchMode=yes -o ConnectTimeout=8 "glasslab-${HOST##*.}" "ps -axo pid,rss,command | grep -iE 'mlx_lm|lm.server' | grep -v grep | head -2" 2>/dev/null || true)"
if [ -n "$ssh_out" ]; then
  while read -r pid rss cmd; do
    [ -n "$pid" ] || continue
    echo "server pid=$pid  rss=$(python3 -c "print(f'{$rss/1024/1024:.1f} GB')")  cmd=$cmd"
    SERVER_PID="$pid"
  done <<<"$ssh_out"
fi

# --- model context limit from the loaded config.json ---
ctx="$(ssh -o BatchMode=yes -o ConnectTimeout=8 "glasslab-${HOST##*.}" \
  'f=$(find /private/tmp/hf-cache -maxdepth 6 -name config.json 2>/dev/null | grep -v snapshots | head -1); python3 -c "import json;c=json.load(open(\"$f\"));print(c.get(\"max_position_embeddings\", c.get(\"max_model_len\", \"?\")))" 2>/dev/null' 2>/dev/null || true)"
[ -n "$ctx" ] && echo "model context limit: ${ctx} tokens"

# --- empirical KV cost (optional) ---
if [ "$PROBE" = "1" ] && [ -n "${SERVER_PID:-}" ]; then
  # Methodology: mmap'd weights make a single RSS delta meaningless (pages
  # fault in/out during sampling). Warm the model with one request, then
  # measure the RSS delta between TWO equal-length long requests: the weights
  # are resident by then, so the residual delta approximates KV growth.
  # Methodology: mmap'd weights make a single RSS delta meaningless (pages
  # fault in/out during sampling). Warm the model with one request, then
  # measure the RSS delta between TWO equal-length long requests: the weights
  # are resident by then, so the residual delta approximates KV growth.
  short_body='{"messages":[{"role":"user","content":"hi"}],"max_tokens":1}'
  long_prompt="$(python3 -c "print('word ' * 8000)")"
  long_body="$(python3 -c "import json,sys; print(json.dumps({'messages':[{'role':'user','content':sys.argv[1]}],'max_tokens':1}))" "$long_prompt")"
  curl -fsS -m 300 -H 'Content-Type: application/json' -d "$short_body" \
    "http://${HOST}:${PORT}/v1/chat/completions" >/dev/null 2>&1 || true
  rss_before="$(ssh -o BatchMode=yes -o ConnectTimeout=8 "glasslab-${HOST##*.}" "ps -o rss= -p ${SERVER_PID}" 2>/dev/null | tr -d ' ')"
  t0=$(date +%s.%N)
  curl -fsS -m 300 -H 'Content-Type: application/json' -d "$long_body" \
    "http://${HOST}:${PORT}/v1/chat/completions" >/dev/null 2>&1 || true
  t1=$(date +%s.%N)
  curl -fsS -m 300 -H 'Content-Type: application/json' -d "$long_body" \
    "http://${HOST}:${PORT}/v1/chat/completions" >/dev/null 2>&1 || true
  t2=$(date +%s.%N)
  rss_after="$(ssh -o BatchMode=yes -o ConnectTimeout=8 "glasslab-${HOST##*.}" "ps -o rss= -p ${SERVER_PID}" 2>/dev/null | tr -d ' ')"
  python3 - "$rss_before" "$rss_after" "$t0" "$t1" "$t2" <<'PY'
import sys
rb, ra = int(sys.argv[1]), int(sys.argv[2])
rss_delta_gb = (ra - rb) / 1024 / 1024
print(f"probe: warm-long-latency={float(sys.argv[4])-float(sys.argv[3]):.1f}s "
      f"2nd-long-latency={float(sys.argv[5])-float(sys.argv[4]):.1f}s "
      f"rss_delta={rss_delta_gb:.2f} GB (8k tokens, weights warm) "
      f"=> ~{rss_delta_gb*1024/8:.1f} MB per 1k tokens of context")
PY
fi
echo