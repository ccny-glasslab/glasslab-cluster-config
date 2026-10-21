#!/usr/bin/env bash
# Push the tracked gateway web-serving config back onto the live gateway.
# Runs from a contributor workstation over SSH. Each file gets a .bak-<stamp>
# copy; if `apache2ctl configtest` fails the previous files are put back and
# Apache is not reloaded.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SNAPROOT="$ROOT/live-config/gateway"
GATEWAY_SSH="${GATEWAY_SSH:-glasslab-gateway}"
STAMP="$(date +%Y%m%d-%H%M%S)"
RESTORED=()

restore_file() {
  local rel="$1"
  local src="$SNAPROOT/$rel"
  local dest="/$rel"
  [ -f "$src" ] || return 0
  ssh -o BatchMode=yes "$GATEWAY_SSH" "sudo -n sh -c '
    set -e
    install -D -m 0644 /dev/stdin \"$dest.tmp.$STAMP\"
    if [ -f \"$dest\" ]; then cp \"$dest\" \"$dest.bak-$STAMP\"; fi
    mv \"$dest.tmp.$STAMP\" \"$dest\"
  '" <"$src"
  RESTORED+=("$dest")
}

rollback() {
  local dest
  for dest in "${RESTORED[@]}"; do
    ssh -o BatchMode=yes "$GATEWAY_SSH" \
      "sudo -n sh -c 'if [ -f \"$dest.bak-$STAMP\" ]; then cp \"$dest.bak-$STAMP\" \"$dest\"; else rm -f \"$dest\"; fi'"
  done
}

restore_file etc/apache2/sites-available/000-default.conf
restore_file etc/apache2/sites-available/glasslab-ssl.conf

if ! ssh -o BatchMode=yes "$GATEWAY_SSH" 'sudo -n apache2ctl configtest'; then
  rollback
  printf 'apache2ctl configtest failed; previous gateway files restored, Apache not reloaded.\n' >&2
  exit 1
fi
ssh -o BatchMode=yes "$GATEWAY_SSH" 'sudo -n systemctl reload apache2'
printf 'Gateway configs restored from %s and Apache reloaded.\n' "$SNAPROOT"
printf '%s\n' 'ufw rules, a2ensite links and /etc/letsencrypt are not managed by this script; see live-config/gateway/README.md.'
