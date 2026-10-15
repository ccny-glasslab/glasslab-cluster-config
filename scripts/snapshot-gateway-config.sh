#!/usr/bin/env bash
# Copy the public gateway's live web-serving config into live-config/gateway/.
# Runs from a contributor workstation: the gateway has no repo checkout, so
# files are read over SSH with `sudo -n` and nothing on the gateway changes.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SNAPROOT="$ROOT/live-config/gateway"
GATEWAY_SSH="${GATEWAY_SSH:-glasslab-gateway}"

copy_file() {
  local rel="$1"
  local tmp
  tmp="$(mktemp)"
  if ! ssh -o BatchMode=yes "$GATEWAY_SSH" "sudo -n cat /$rel" >"$tmp" 2>/dev/null; then
    rm -f "$tmp"
    printf 'Failed to read /%s from %s\n' "$rel" "$GATEWAY_SSH" >&2
    exit 1
  fi
  install -D -m 0644 "$tmp" "$SNAPROOT/$rel"
  rm -f "$tmp"
}

copy_file etc/apache2/sites-available/000-default.conf

printf 'Snapshot updated under %s\n' "$SNAPROOT"
printf '%s\n' 'Firewall rules are recorded by hand in live-config/gateway/README.md; compare with `sudo ufw status verbose` on the gateway.'
