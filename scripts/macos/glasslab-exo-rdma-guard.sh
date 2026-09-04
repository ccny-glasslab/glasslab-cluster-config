#!/bin/bash
# RDMA device drift guard for the exo pair.
#
# The recurring "No instance found for <model>" / JACCL queue-pair RTR errno 96
# outage is caused by macOS failing to register the Thunderbolt RDMA verbs
# device (rdma_en5) on a Mac until reboot. This job runs periodically and
# makes the drift LOUD immediately instead of letting the reconcile spin in
# silence, so the single-node fallback is understood as degraded and the
# reboot repair is triggered by an operator.
set -euo pipefail

RDMA_DEVICE="${GLASSLAB_EXO_RDMA_DEVICE:-rdma_en5}"
LOG_FILE="${GLASSLAB_EXO_RDMA_GUARD_LOG:-/Users/glasslab/Library/Logs/glasslab-exo-rdma-guard.log}"

log() {
  printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"
}

if ! /usr/bin/ibv_devices 2>/dev/null | /usr/bin/awk '{print $1}' | grep -qx "$RDMA_DEVICE"; then
  log "RDMA device ${RDMA_DEVICE} is NOT registered (ibv_devices). Two-node JACCL placement will fail. Reboot this Mac to re-register (launchd auto-restores exo); the reconcile keeps a single-node fallback serving meanwhile." >>"$LOG_FILE"
  exit 1
fi

exit 0