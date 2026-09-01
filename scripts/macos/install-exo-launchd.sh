#!/bin/bash
set -euo pipefail

ROLE="${1:-}"
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)

if [[ "$ROLE" != "17" && "$ROLE" != "18" ]]; then
  echo "Usage: sudo $0 17|18" >&2
  exit 2
fi

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run this installer with sudo." >&2
  exit 2
fi

if [[ ! -x /Users/glasslab/exo/.venv/bin/exo ]]; then
  echo "Missing /Users/glasslab/exo/.venv/bin/exo" >&2
  exit 3
fi

install -d -o glasslab -g staff /Users/glasslab/Library/Logs
install -d -o root -g wheel /usr/local/libexec
install -o root -g wheel -m 0755 \
  "${SCRIPT_DIR}/glasslab-exo-run.sh" \
  /usr/local/libexec/glasslab-exo-run
install -o root -g wheel -m 0755 \
  "${SCRIPT_DIR}/glasslab-exo-reconcile.sh" \
  /usr/local/libexec/glasslab-exo-reconcile
install -o root -g wheel -m 0755 \
  "${SCRIPT_DIR}/glasslab-exo-rdma-guard.sh" \
  /usr/local/libexec/glasslab-exo-rdma-guard
install -o root -g wheel -m 0644 \
  "${SCRIPT_DIR}/com.glasslab.exo-${ROLE}.plist" \
  /Library/LaunchDaemons/com.glasslab.exo.plist
install -o root -g wheel -m 0644 \
  "${SCRIPT_DIR}/com.glasslab.exo-rdma-guard.plist" \
  /Library/LaunchDaemons/com.glasslab.exo-rdma-guard.plist

plutil -lint /Library/LaunchDaemons/com.glasslab.exo.plist
plutil -lint /Library/LaunchDaemons/com.glasslab.exo-rdma-guard.plist

launchctl bootout system/com.glasslab.exo 2>/dev/null || true
launchctl bootout system/com.glasslab.exo-reconcile 2>/dev/null || true
launchctl bootout system/com.glasslab.exo-rdma-guard 2>/dev/null || true
sleep 2
pkill -u glasslab -f '/Users/glasslab/exo/.venv/bin/python3' 2>/dev/null || true
pkill -u glasslab -f 'caffeinate.*exo' 2>/dev/null || true

if [[ "$ROLE" == "17" ]]; then
  install -o root -g wheel -m 0644 \
    "${SCRIPT_DIR}/com.glasslab.exo-reconcile.plist" \
    /Library/LaunchDaemons/com.glasslab.exo-reconcile.plist
  plutil -lint /Library/LaunchDaemons/com.glasslab.exo-reconcile.plist
else
  rm -f /Library/LaunchDaemons/com.glasslab.exo-reconcile.plist
fi

launchctl bootstrap system /Library/LaunchDaemons/com.glasslab.exo.plist
launchctl enable system/com.glasslab.exo
launchctl kickstart -k system/com.glasslab.exo

launchctl bootstrap system /Library/LaunchDaemons/com.glasslab.exo-rdma-guard.plist
launchctl enable system/com.glasslab.exo-rdma-guard
launchctl kickstart -k system/com.glasslab.exo-rdma-guard

if [[ "$ROLE" == "17" ]]; then
  launchctl bootstrap system /Library/LaunchDaemons/com.glasslab.exo-reconcile.plist
  launchctl enable system/com.glasslab.exo-reconcile
  launchctl kickstart -k system/com.glasslab.exo-reconcile
fi

launchctl print system/com.glasslab.exo | grep -E 'state =|pid =|last exit code ='
