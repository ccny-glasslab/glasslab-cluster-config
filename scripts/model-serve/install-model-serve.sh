#!/usr/bin/env bash
# Install launchd daemons for a standalone mlx model server + serializing guard.
#
# One Mac per model (exo is retired). The daemons KeepAlive the server and
# guard so a crash or reboot restores serving without manual intervention.
#
# Weights live in a DURABLE Hugging Face cache under the service user's home.
# The previous cache lived under the ephemeral macOS temp dir: it was silently
# cleared, leaving a dangling/0-byte snapshot, and mlx_lm.server then hung
# forever on the first completion instead of failing. This installer refuses
# to point a daemon at an incomplete snapshot.
#
# Usage (run ON the Mac, as a sudo-capable user):
#   sudo ./install-model-serve.sh coder       # .17 -> Qwen3-Coder-Next-4bit
#   sudo ./install-model-serve.sh thinking    # .18 -> Qwen3-Next-80B-A3B-Thinking-4bit
#   sudo ./install-model-serve.sh coder --replace
#   sudo ./install-model-serve.sh coder --offline
#
# Offline mode is OPT-IN. By default the daemons may re-download missing or
# incomplete weights; pass --offline (or set GLASSLAB_MODEL_HF_OFFLINE=1) to
# pin HF_HUB_OFFLINE=1 in the daemons.
#
# With --replace the script stops any non-launchd process already bound to the
# service ports (needed to migrate the currently manual setup). DO NOT pass
# --replace while a rehearsal/run is actively using the model: stopping the
# server mid-turn kills the in-flight generation.
set -euo pipefail

SERVICE_USER="glasslab"
GROUP="staff"
PYTHON="/Users/glasslab/exo/.venv/bin/python"
MLX_BIN="/Users/glasslab/exo/.venv/bin/mlx_lm.server"
GUARD_SRC="/tmp/model_guard.py"
GUARD_DST="/usr/local/libexec/glasslab-model-guard.py"
GUARD_PY="/Applications/Xcode.app/Contents/Developer/Library/Frameworks/Python3.framework/Versions/3.9/Resources/Python.app/Contents/MacOS/Python"
MLX_PORT=52416
GUARD_PORT=52417
PROMPT_CACHE_SIZE=8

# Single source of truth for the durable model cache. HF_HOME MUST live under
# the service user's home so macOS never reaps it; the hub tree is $HF_HOME/hub
# (the standard huggingface_hub layout), which is also where `hf download`
# writes.
HF_HOME="${GLASSLAB_MODEL_HF_HOME:-/Users/$SERVICE_USER/.cache/huggingface}"
HF_HUB="$HF_HOME/hub"

usage() {
  echo "usage: $0 <coder|thinking> [--replace] [--offline]" >&2
}

parse_args() {
  ROLE=""
  REPLACE=0
  OFFLINE=0
  while [ "$#" -gt 0 ]; do
    case "$1" in
      coder|thinking) ROLE="$1" ;;
      --replace) REPLACE=1 ;;
      --offline) OFFLINE=1 ;;
      *) usage; exit 2 ;;
    esac
    shift
  done
  [ -n "$ROLE" ] || { usage; exit 2; }
}

# Echo the first completeness problem in a snapshot, or nothing when it is
# complete. This catches the exact failure mode that hung the server: a
# config.json that is a dangling symlink, a missing shard index, and weight
# shards whose blobs are 0 bytes (incomplete downloads).
snapshot_problem() {
  local snapshot="$1" shard found=0
  if [ ! -f "$snapshot/config.json" ]; then
    echo "config.json is missing or is a dangling symlink"
    return 0
  fi
  if [ ! -f "$snapshot/model.safetensors.index.json" ]; then
    echo "model.safetensors.index.json is missing"
    return 0
  fi
  for shard in "$snapshot"/model-*.safetensors; do
    if [ ! -e "$shard" ] && [ ! -L "$shard" ]; then
      continue
    fi
    found=1
    # -s follows the snapshot symlink into the blob cache: a dangling link or a
    # 0-byte blob fails here.
    if [ ! -s "$shard" ]; then
      echo "weight shard $(basename "$shard") is missing or its blob is empty"
      return 0
    fi
  done
  if [ "$found" -eq 0 ]; then
    echo "no model-*.safetensors weight shards are present"
    return 0
  fi
  return 0
}

snapshot_is_complete() {
  local problem
  problem="$(snapshot_problem "$1")"
  [ -z "$problem" ]
}

require_complete_snapshot() {
  local snapshot="$1" problem hf
  problem="$(snapshot_problem "$snapshot")"
  if [ -z "$problem" ]; then
    return 0
  fi
  hf="/Users/$SERVICE_USER/exo/.venv/bin/hf"
  if [ ! -x "$hf" ]; then
    hf="hf"
  fi
  {
    echo "error: refusing to install $ROLE: Hugging Face snapshot is incomplete"
    echo "  snapshot: $snapshot"
    echo "  problem:  $problem"
    echo "  repair:   HF_HOME=$HF_HOME $hf download $REPO --revision $REVISION"
    echo "            then re-run this installer"
  } >&2
  exit 1
}

write_plist() {
  local label="$1" out="$2"; shift 2
  cat > "$out" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
  <dict>
    <key>Label</key><string>$label</string>
    <key>ProgramArguments</key>
    <array>
$*
    </array>
    <key>EnvironmentVariables</key>
    <dict>
      <key>HF_HOME</key><string>$HF_HOME</string>
      <key>HF_HUB_OFFLINE</key><string>$OFFLINE</string>
      <key>HOME</key><string>/Users/$SERVICE_USER</string>
    </dict>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>ThrottleInterval</key><integer>10</integer>
    <key>UserName</key><string>$SERVICE_USER</string>
    <key>GroupName</key><string>$GROUP</string>
    <key>ProcessType</key><string>Background</string>
    <key>StandardOutPath</key><string>$LOG_DIR/$label.log</string>
    <key>StandardErrorPath</key><string>$LOG_DIR/$label.error.log</string>
  </dict>
</plist>
PLIST
  chown root:wheel "$out"
  chmod 644 "$out"
}

main() {
  parse_args "$@"
  OFFLINE="${GLASSLAB_MODEL_HF_OFFLINE:-$OFFLINE}"

  case "$ROLE" in
    coder)
      LABEL="com.glasslab.mlx-coder"
      REPO="mlx-community/Qwen3-Coder-Next-4bit"
      REVISION="7b9321eabb85ce79625cac3f61ea691e4ea984b5"
      ;;
    thinking)
      LABEL="com.glasslab.mlx-thinking"
      REPO="mlx-community/Qwen3-Next-80B-A3B-Thinking-4bit"
      REVISION="9a2b46347bb170cb2924092175fa21554fe585a9"
      ;;
  esac

  # huggingface_hub stores a repo as models--<org>--<name>; the revision is the
  # snapshot directory name.
  SNAPSHOT="$HF_HUB/models--${REPO//\//--}/snapshots/$REVISION"
  MODEL="$SNAPSHOT"

  LOG_DIR="/Users/$SERVICE_USER/Library/Logs"
  PLIST_DIR="/Library/LaunchDaemons"
  SERVER_PLIST="$PLIST_DIR/$LABEL.plist"
  GUARD_PLIST="$PLIST_DIR/$LABEL.guard.plist"

  # Never start a daemon against weights that were never fully downloaded; an
  # incomplete snapshot hangs mlx_lm.server on the first completion.
  require_complete_snapshot "$SNAPSHOT"

  # Refuse to clobber a live manual server unless --replace is explicit.
  if [ "$REPLACE" = "0" ]; then
    if pgrep -f "mlx_lm.server.*--port $MLX_PORT" >/dev/null 2>&1 ||
       pgrep -f "model_guard.py.*--port $GUARD_PORT" >/dev/null 2>&1; then
      echo "manual server/guard already listening on $MLX_PORT/$GUARD_PORT;" >&2
      echo "re-run with --replace to migrate (only while no run is active)." >&2
      exit 1
    fi
  fi

  mkdir -p "$LOG_DIR"

  # The guard script currently lives in /tmp (wiped on reboot); install a copy
  # at a stable path so the daemon survives restarts.
  cp "$GUARD_SRC" "$GUARD_DST"
  chmod 755 "$GUARD_DST"

  mlx_args="    <string>$PYTHON</string>
    <string>$MLX_BIN</string>
    <string>--model</string><string>$MODEL</string>
    <string>--port</string><string>$MLX_PORT</string>
    <string>--host</string><string>0.0.0.0</string>
    <string>--decode-concurrency</string><string>1</string>
    <string>--prompt-concurrency</string><string>1</string>
    <string>--prompt-cache-size</string><string>$PROMPT_CACHE_SIZE</string>"
  guard_args="    <string>$GUARD_PY</string>
    <string>$GUARD_DST</string>
    <string>--port</string><string>$GUARD_PORT</string>
    <string>--upstream</string><string>http://127.0.0.1:$MLX_PORT/v1</string>"

  write_plist "$LABEL" "$SERVER_PLIST" "$mlx_args"
  write_plist "$LABEL.guard" "$GUARD_PLIST" "$guard_args"

  # Migrate from the manual setup: stop non-launchd processes on the ports.
  if [ "$REPLACE" = "1" ]; then
    launchctl bootout system/"$LABEL" 2>/dev/null || true
    launchctl bootout system/"$LABEL.guard" 2>/dev/null || true
    pkill -f "mlx_lm.server.*--port $MLX_PORT" 2>/dev/null || true
    pkill -f "model_guard.py.*--port $GUARD_PORT" 2>/dev/null || true
    sleep 2
  fi

  launchctl bootstrap system "$SERVER_PLIST"
  launchctl bootstrap system "$GUARD_PLIST"

  # Wait for the model to load, then verify end to end through the guard.
  echo "waiting for $ROLE server to load (model weights) ..."
  for _ in $(seq 1 60); do
    if curl -fsS -m 5 "http://127.0.0.1:$GUARD_PORT/v1/models" >/dev/null 2>&1; then
      echo "OK: $LABEL serving on $GUARD_PORT"
      exit 0
    fi
    sleep 5
  done
  echo "server did not become healthy; check $LOG_DIR/$LABEL.error.log" >&2
  exit 1
}

# Allow `source`-ing for unit tests without executing the installer.
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  main "$@"
fi
