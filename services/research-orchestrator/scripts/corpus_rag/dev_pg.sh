#!/usr/bin/env bash
# Throwaway pgvector dev container for the corpus_rag prototype.
#
# Subcommands: up | down | status
#
# Env conventions for corpus_rag prototype work (export these in your shell):
#   CORPUS_RAG_PG_DSN=postgresql://postgres:ragdev@127.0.0.1:5433/postgres
#   HF_HOME=/home/gr66ss/.cache/huggingface
#   TMPDIR=/home/gr66ss/tmp-pip
#
# This container is throwaway dev-only infrastructure. POSTGRES_PASSWORD=ragdev
# is a throwaway dev-only credential, never used elsewhere (bound to 127.0.0.1).
set -euo pipefail

IMAGE="pgvector/pgvector:pg16"
NAME="glasslab-ragdev-pg"
HOST_PORT=5433
PGDATA_HOST="/home/gr66ss/tmp-pg/pgdata"

container_exists() { docker ps -a --format '{{.Names}}' | grep -qx "$NAME"; }
container_running() { [ "$(docker inspect -f '{{.State.Running}}' "$NAME" 2>/dev/null)" = "true" ]; }

wait_ready() {
    # The official image entrypoint briefly starts a temporary server during
    # first-time initdb, then restarts. Require readiness twice, 2s apart, so we
    # do not race that window.
    local hits=0
    for _ in $(seq 1 45); do
        if docker exec "$NAME" pg_isready -U postgres -p 5432 >/dev/null 2>&1; then
            hits=$((hits + 1))
            if [ "$hits" -ge 2 ]; then
                return 0
            fi
        else
            hits=0
        fi
        sleep 2
    done
    echo "ERROR: postgres not ready after ~90s" >&2
    return 1
}

cmd_up() {
    mkdir -p "$PGDATA_HOST"
    if container_running; then
        echo "$NAME already running"
    elif container_exists; then
        echo "$NAME exists but stopped; starting"
        docker start "$NAME" >/dev/null
    else
        docker run -d --name "$NAME" \
            -e POSTGRES_PASSWORD=ragdev \
            -p "127.0.0.1:${HOST_PORT}:5432" \
            -v "${PGDATA_HOST}:/var/lib/postgresql/data" \
            "$IMAGE" >/dev/null
        echo "$NAME started on port ${HOST_PORT}"
    fi
    wait_ready
    docker exec "$NAME" psql -U postgres -v ON_ERROR_STOP=1 \
        -c "CREATE EXTENSION IF NOT EXISTS vector;"
    echo "Extensions:"
    docker exec "$NAME" psql -U postgres -tAc "SELECT extname FROM pg_extension ORDER BY extname;"
}

cmd_down() {
    if container_exists; then
        docker rm -f "$NAME" >/dev/null
        echo "$NAME removed."
    else
        echo "$NAME does not exist."
    fi
    echo "Note: data persists in ${PGDATA_HOST} until you delete that directory manually."
}

cmd_status() {
    if ! container_exists; then
        echo "$NAME: no such container"
        return 1
    fi
    docker ps -a --filter "name=${NAME}" --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
    if docker exec "$NAME" pg_isready -U postgres -p 5432; then
        return 0
    fi
    return 1
}

case "${1:-}" in
    up) cmd_up ;;
    down) cmd_down ;;
    status) cmd_status ;;
    *)
        echo "usage: $0 {up|down|status}" >&2
        exit 2
        ;;
esac
