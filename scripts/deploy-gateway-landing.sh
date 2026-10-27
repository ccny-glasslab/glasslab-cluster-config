#!/usr/bin/env bash
# Publish the tracked landing page (live-config/gateway/var/www/html) to the
# gateway's /var/www/html. Runs from a contributor workstation over SSH.
# Only the tracked files are written; other content in /var/www/html (course
# pages, the old glasslab_log.gif) is left alone. The files being replaced are
# copied to /var/backups/glasslab-landing-<stamp>/ first (outside the web root,
# so the backups are not served). Static files only: Apache is not reloaded.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$ROOT/live-config/gateway/var/www/html"
GATEWAY_SSH="${GATEWAY_SSH:-glasslab-gateway}"
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP="/var/backups/glasslab-landing-$STAMP"
# index.html last, so a visitor never gets a page whose assets are missing.
FILES=(lottie.min.js glasslab.json index.html)

for f in "${FILES[@]}"; do
  ssh -o BatchMode=yes "$GATEWAY_SSH" "sudo -n sh -c '
    set -e
    mkdir -p \"$BACKUP\"
    if [ -f \"/var/www/html/$f\" ]; then cp -a \"/var/www/html/$f\" \"$BACKUP/\"; fi
    install -o root -g root -m 0644 /dev/stdin \"/var/www/html/$f.tmp.$STAMP\"
    mv \"/var/www/html/$f.tmp.$STAMP\" \"/var/www/html/$f\"
  '" <"$SRC/$f"
done
printf 'Landing page published from %s; replaced files backed up to %s on the gateway.\n' "$SRC" "$BACKUP"
