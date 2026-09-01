#!/bin/bash
# Reconcile the approved exo model placement.
#
# Preferred state: a two-node Pipeline + MlxJaccl placement of $MODEL over the
# $RDMA_IFACE Thunderbolt link. When the pair is unavailable (RDMA device
# drift on either Mac — JACCL queue-pair RTR errno 96 — or a link down), the
# approved model still gets a SINGLE-NODE placement so the orchestrator is
# never left without its model. The two-node path is re-tried continuously and
# the fallback is replaced automatically when the pair returns.
set -euo pipefail

API_BASE="${GLASSLAB_EXO_API_BASE:-http://127.0.0.1:52415}"
MODEL="${GLASSLAB_EXO_MODEL:-mlx-community/Qwen3-Coder-Next-4bit}"
RDMA_IFACE="${GLASSLAB_EXO_RDMA_IFACE:-rdma_en5}"
INTERVAL="${GLASSLAB_EXO_RECONCILE_INTERVAL:-30}"
PAIR_GRACE_CHECKS="${GLASSLAB_EXO_PAIR_GRACE_CHECKS:-3}"

log() {
  printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"
}

needs_probe=1
placement_grace_until=0
pair_down_checks=0
serving_single=0

topology_ok() {
  local state="$1"
  printf '%s' "$state" | /usr/bin/jq -e --arg iface "$RDMA_IFACE" '
    (.topology.nodes | length) == 2 and
    ([
      .topology.connections
      | to_entries[]?.value
      | to_entries[]?.value[]?
      | select(.sourceRdmaIface? == $iface and .sinkRdmaIface? == $iface)
    ] | length) >= 2
  ' >/dev/null 2>&1
}

instances_for_model() {
  local state="$1"
  printf '%s' "$state" | /usr/bin/jq -r --arg model "$MODEL" '
    .instances
    | to_entries[]?
    | select([.value | .. | strings | select(. == $model)] | length > 0)
    | .key
  '
}

probe() {
  local payload
  payload=$(/usr/bin/jq -nc --arg model "$MODEL" '{
    model: $model,
    messages: [{role: "user", content: "ping"}],
    max_tokens: 1,
    temperature: 0
  }')
  /usr/bin/curl -sS --max-time 120 -o /dev/null -w '%{http_code}' \
    -H 'Content-Type: application/json' \
    --data-binary "$payload" \
    "${API_BASE}/v1/chat/completions" || true
}

submit_preview() {
  local node_count="$1"
  local previews instance payload
  previews=$(/usr/bin/curl -fsS --max-time 15 --get \
    --data-urlencode "model_id=${MODEL}" \
    "${API_BASE}/instance/previews" 2>/dev/null || true)
  if [[ "$node_count" == "2" ]]; then
    instance=$(printf '%s' "$previews" | /usr/bin/jq -c '
      first(
        .previews[]?
        | select(
            .error == null and
            .sharding == "Pipeline" and
            .instance_meta == "MlxJaccl" and
            (.memory_delta_by_node | length) == 2
          )
        | .instance
      )
    ' 2>/dev/null || true)
  else
    instance=$(printf '%s' "$previews" | /usr/bin/jq -c '
      first(
        .previews[]?
        | select(
            .error == null and
            .sharding == "Pipeline" and
            (.memory_delta_by_node | length) == 1
          )
        | .instance
      )
    ' 2>/dev/null || true)
  fi
  if [[ -z "$instance" || "$instance" == "null" ]]; then
    log "no ${node_count}-node Pipeline placement is available for ${MODEL}"
    return 1
  fi
  payload=$(/usr/bin/jq -nc --argjson instance "$instance" '{instance: $instance}')
  if /usr/bin/curl -fsS --max-time 30 -X POST \
    -H 'Content-Type: application/json' \
    --data-binary "$payload" \
    "${API_BASE}/instance" >/dev/null; then
    needs_probe=1
    placement_grace_until=$(($(date +%s) + 180))
    log "submitted ${node_count}-node placement for ${MODEL}"
    return 0
  fi
  log "failed to submit ${node_count}-node placement for ${MODEL}"
  return 1
}

while true; do
  state=$(/usr/bin/curl -fsS --max-time 5 "${API_BASE}/state" 2>/dev/null || true)
  if [[ -z "$state" ]]; then
    log "master API unavailable"
    sleep "$INTERVAL"
    continue
  fi

  if topology_ok "$state"; then
    pair_down_checks=0
    if (( serving_single == 1 )); then
      log "two-node ${RDMA_IFACE} topology restored; removing single-node fallback"
      while IFS= read -r instance_id; do
        [[ -n "$instance_id" ]] || continue
        /usr/bin/curl -fsS --max-time 30 -X DELETE \
          "${API_BASE}/instance/${instance_id}" >/dev/null || true
      done <<< "$(instances_for_model "$state")"
      serving_single=0
    fi
  else
    pair_down_checks=$((pair_down_checks + 1))
    if (( pair_down_checks == 1 )); then
      if ! /usr/sbin/ibv_devices 2>/dev/null | /usr/bin/awk '{print $1}' | grep -qx "$RDMA_IFACE"; then
        log "REPAIR REQUIRED: ${RDMA_IFACE} is not registered on this Mac (ibv_devices); reboot to re-register"
      fi
      log "two-node ${RDMA_IFACE} topology unavailable; will fall back to single-node after ${PAIR_GRACE_CHECKS} checks"
    fi
  fi

  instance_ids=$(instances_for_model "$state")

  if [[ -n "$instance_ids" ]]; then
    if (( needs_probe == 0 )); then
      sleep "$INTERVAL"
      continue
    fi
    probe_code=$(probe)
    if [[ "$probe_code" == "200" ]]; then
      needs_probe=0
      placement_grace_until=0
      mode="two-node"
      (( serving_single == 1 )) && mode="single-node fallback"
      log "verified inference for ${MODEL} (${mode})"
      sleep "$INTERVAL"
      continue
    fi
    now=$(date +%s)
    if (( now < placement_grace_until )); then
      log "placement is still starting; inference returned HTTP ${probe_code}"
      sleep "$INTERVAL"
      continue
    fi
    log "removing stale instance after inference returned HTTP ${probe_code}"
    while IFS= read -r instance_id; do
      [[ -n "$instance_id" ]] || continue
      /usr/bin/curl -fsS --max-time 30 -X DELETE \
        "${API_BASE}/instance/${instance_id}" >/dev/null || true
    done <<< "$instance_ids"
    serving_single=0
    sleep "$INTERVAL"
    continue
  fi

  if topology_ok "$state"; then
    submit_preview 2 || true
  elif (( pair_down_checks >= PAIR_GRACE_CHECKS )); then
    if submit_preview 1; then
      serving_single=1
      log "serving ${MODEL} on single-node fallback while the ${RDMA_IFACE} pair is unavailable"
    fi
  fi

  sleep "$INTERVAL"
done