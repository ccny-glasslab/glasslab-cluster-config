#!/usr/bin/env bash
# DRAFT. Lab-machine variant of scripts/start-production.sh: TLS ends at the
# gateway, so this uses compose.lan.yaml instead of compose.https.yaml.
# Copy to scripts/start-lab.sh in the extracted release.
set -euo pipefail
cd "$(dirname "$0")/.."
root=${EXAM_ROOT:-/srv/exam}
: "${EXAM_LAN_IP:?Set EXAM_LAN_IP}"
: "${EXAM_DISK_UUID:?Set the verified data-disk UUID}"
: "${EXAM_FIREWALL_CHECK:?Set the absolute reviewed firewall-check executable}"
[[ $EXAM_FIREWALL_CHECK == /* && -x $EXAM_FIREWALL_CHECK ]] || { echo 'Firewall checker missing' >&2; exit 1; }
mountpoint -q "$root" || { echo 'Data disk missing' >&2; exit 1; }
[[ $(findmnt -n -o UUID --target "$root") == "$EXAM_DISK_UUID" ]] || { echo 'Wrong data disk' >&2; exit 1; }
"$EXAM_FIREWALL_CHECK"
docker compose -f compose.yaml -f compose.lan.yaml config --quiet
docker compose -f compose.yaml -f compose.lan.yaml up -d --no-build
echo 'Hub started. Run the live traffic probes next:'
echo "  sudo -E $EXAM_FIREWALL_CHECK --probe"
