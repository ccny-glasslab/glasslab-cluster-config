#!/usr/bin/env bash
set -euo pipefail

KUBECTL="${KUBECTL:-kubectl}"
NAMESPACE="${GLASSLAB_V2_NAMESPACE:-glasslab-v2}"
SECRET_NAME="${GLASSLAB_GHCR_PULL_SECRET_NAME:-glasslab-ghcr-pull}"
REGISTRY_HOST="${GLASSLAB_WORKFLOW_API_REGISTRY_HOST:-ghcr.io}"
REGISTRY_USERNAME="${GHCR_USERNAME:-${GITHUB_ACTOR:-ccny-glasslab}}"
REGISTRY_TOKEN="${GHCR_TOKEN:-}"
unset GHCR_TOKEN

usage() {
  cat <<'USAGE'
Usage: create-ghcr-pull-secret.sh [--namespace <ns>] [--secret-name <name>] [--username <user>]

Create or refresh the private GHCR Docker registry secret used by Glasslab v2.

Environment:
  GHCR_TOKEN    GitHub token with package read access
  GHCR_USERNAME Registry username. Defaults to ccny-glasslab.

If GHCR_TOKEN is unset, the token is read from standard input.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --namespace)
      NAMESPACE="$2"
      shift 2
      ;;
    --secret-name)
      SECRET_NAME="$2"
      shift 2
      ;;
    --username)
      REGISTRY_USERNAME="$2"
      shift 2
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      printf '[create-ghcr-pull-secret] unknown argument: %s\n' "$1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

if [[ -z "$REGISTRY_TOKEN" ]]; then
  IFS= read -r REGISTRY_TOKEN || true
fi
if [[ -z "$REGISTRY_TOKEN" ]]; then
  printf '[create-ghcr-pull-secret] a token is required via GHCR_TOKEN or standard input\n' >&2
  exit 1
fi

umask 077
TEMP_DIR="$(mktemp -d)"
chmod 0700 "$TEMP_DIR"
DOCKER_CONFIG_PATH="$TEMP_DIR/config.json"
cleanup() {
  rm -rf -- "$TEMP_DIR"
}
trap cleanup EXIT

printf '%s' "$REGISTRY_TOKEN" | python3 -c '
import base64
import json
import os
import sys

registry_host, registry_username, config_path = sys.argv[1:]
registry_token = sys.stdin.read()
auth = base64.b64encode(f"{registry_username}:{registry_token}".encode("utf-8")).decode("ascii")
config = {
    "auths": {
        registry_host: {
            "username": registry_username,
            "password": registry_token,
            "auth": auth,
        }
    }
}
descriptor = os.open(config_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
    json.dump(config, stream, separators=(",", ":"))
' "$REGISTRY_HOST" "$REGISTRY_USERNAME" "$DOCKER_CONFIG_PATH"
unset REGISTRY_TOKEN

printf '[create-ghcr-pull-secret] refreshing %s in namespace %s\n' "$SECRET_NAME" "$NAMESPACE"
"$KUBECTL" -n "$NAMESPACE" create secret generic "$SECRET_NAME" \
  --type=kubernetes.io/dockerconfigjson \
  "--from-file=.dockerconfigjson=$DOCKER_CONFIG_PATH" \
  --dry-run=client \
  -o yaml | "$KUBECTL" apply -f -

printf '[create-ghcr-pull-secret] done\n'
