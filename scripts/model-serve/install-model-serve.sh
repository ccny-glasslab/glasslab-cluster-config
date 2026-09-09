#!/usr/bin/env bash
# Install launchd daemons for a standalone mlx model server + serializing guard.
#
# One Mac per model (exo is retired). The daemons KeepAlive the server and
# guard so a crash or reboot restores serving without manual intervention.
#
# Usage (run ON the Mac, as a sudo-capable user):
#   sudo ./install-model-serve.sh coder       # .17 -> Qwen3-Coder-Next-4bit
#   sudo ./install-model-serve.sh thinking    # .18 -> Qwen3-Next-80B-A3B-Thinking-4bit
#
# With --replace the script stops any non-launchd process already bound to the
# service ports (needed to migrate the currently manual setup). DO NOT pass
# --replace while a rehearsal/run is actively using the model: stopping the
# server mid-turn kills the in-flight generation.
set -euo pipefail

ROLE="${1:-}"
REPLACE=0
[ "${2:-}" = "--replace" ] && REPLACE=1

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
HF_HOME="/private/tmp/hf-cache"

case "$ROLE" in
  coder)
    LABEL="com.glasslab.mlx-coder"
    MODEL="$HF_HOME/models--mlx-community--Qwen3-Coder-Next-4bit/snapshots/7b9321eabb85ce79625cac3f61ea691e4ea984b5"
    ;;
  thinking)
    LABEL="com.glasslab.mlx-thinking"
    MODEL="$HF_HOME/models--mlx-community--Qwen3-Next-80B-A3B-Thinking-4bit/snapshots/9a2b46347bb170cb2924092175fa21554fe585a9"
    ;;
  *)
    echo "usage: $0 <coder|thinking> [--replace]" >&2
    exit 2
    ;;
esac

LOG_DIR="/Users/$SERVICE_USER/Library/Logs"
PLIST_DIR="/Library/LaunchDaemons"
SERVER_PLIST="$PLIST_DIR/$LABEL.plist"
GUARD_PLIST="$PLIST_DIR/$LABEL.guard.plist"

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
      <key>HF_HUB_OFFLINE</key><string>1</string>
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