#!/bin/bash
set -euo pipefail

ROLE="${GLASSLAB_EXO_ROLE:?GLASSLAB_EXO_ROLE is required}"
SELF_IP="${GLASSLAB_EXO_SELF_IP:?GLASSLAB_EXO_SELF_IP is required}"
PEER_IP="${GLASSLAB_EXO_PEER_IP:?GLASSLAB_EXO_PEER_IP is required}"
EXO_HOME="${GLASSLAB_EXO_HOME:?GLASSLAB_EXO_HOME is required}"
EXO_REPO="${GLASSLAB_EXO_REPO:-/Users/glasslab/exo}"
EXO_BIN="${EXO_REPO}/.venv/bin/exo"
API_PORT="${GLASSLAB_EXO_API_PORT:-52415}"
LIBP2P_PORT="${GLASSLAB_EXO_LIBP2P_PORT:-54216}"
NAMESPACE="${GLASSLAB_EXO_NAMESPACE:-glasslab-rdma-prod}"
IFACE="${GLASSLAB_EXO_IFACE:-en5}"

log() {
  printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"
}

if [[ "$ROLE" != "master" && "$ROLE" != "worker" ]]; then
  log "invalid role: ${ROLE}"
  exit 2
fi

if [[ ! -x "$EXO_BIN" ]]; then
  log "exo executable not found: ${EXO_BIN}"
  exit 3
fi

mkdir -p "$EXO_HOME"

until /sbin/ifconfig "$IFACE" 2>/dev/null | /usr/bin/grep -q "inet ${SELF_IP} "; do
  log "waiting for ${SELF_IP} on ${IFACE}"
  sleep 10
done

common_args=(
  -vv
  --libp2p-port "$LIBP2P_PORT"
  --fast-synch
)

export EXO_HOME
export EXO_LIBP2P_NAMESPACE="$NAMESPACE"
export EXO_FAST_SYNCH=1
export EXO_INFO_DISK_INTERVAL=60

cd "$EXO_REPO"

if [[ "$ROLE" == "master" ]]; then
  log "starting deterministic master on ${SELF_IP}"
  exec /usr/bin/caffeinate -dimsu "$EXO_BIN" \
    "${common_args[@]}" \
    -m \
    --api-port "$API_PORT"
fi

until /sbin/ping -S "$SELF_IP" -c 1 -t 3 "$PEER_IP" >/dev/null 2>&1; do
  log "waiting for Thunderbolt peer ${PEER_IP}"
  sleep 10
done

master_id=""
until master_id=$(/usr/bin/curl -fsS --max-time 5 \
  "http://${PEER_IP}:${API_PORT}/node_id" 2>/dev/null | /usr/bin/tr -d '"[:space:]') && \
  [[ -n "$master_id" ]]; do
  log "waiting for master API at ${PEER_IP}:${API_PORT}"
  sleep 10
done

bootstrap_peer="/ip4/${PEER_IP}/tcp/${LIBP2P_PORT}/p2p/${master_id}"
log "starting worker with bootstrap peer ${bootstrap_peer}"
exec /usr/bin/caffeinate -dimsu "$EXO_BIN" \
  "${common_args[@]}" \
  --no-api \
  --bootstrap-peers "$bootstrap_peer"
