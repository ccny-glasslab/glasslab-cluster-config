#!/usr/bin/env bash
set -euo pipefail

REPOSITORY="${GLASSLAB_GITHUB_REPOSITORY:-ccny-glasslab/glasslab-cluster-config}"
WORKFLOW="${GLASSLAB_IMAGE_WORKFLOW:-manual-docker.yml}"

usage() {
  cat <<'USAGE'
Usage: request-service-image.sh <service> [tag]

Ask the repository's GitHub Actions workflow to build and publish an image.
This uses the caller's GitHub identity to request the workflow; GHCR
publication uses the workflow's short-lived GITHUB_TOKEN.

Services:
  workflow-api
  research-command-router
  research-orchestrator
  research-workspace-runner
  runner
USAGE
}

if [[ $# -lt 1 || $# -gt 2 ]]; then
  usage >&2
  exit 2
fi

SERVICE="$1"
TAG="${2:-}"

case "$SERVICE" in
  workflow-api|research-command-router|research-orchestrator|research-workspace-runner|runner)
    ;;
  *)
    printf 'Unsupported service: %s\n' "$SERVICE" >&2
    usage >&2
    exit 2
    ;;
esac

if ! command -v gh >/dev/null 2>&1; then
  printf 'GitHub CLI is required. Ask an administrator to install gh.\n' >&2
  exit 1
fi

if ! gh auth status >/dev/null 2>&1; then
  printf 'Authenticate your own GitHub account first: gh auth login --web\n' >&2
  exit 1
fi

args=(
  workflow run "$WORKFLOW"
  --repo "$REPOSITORY"
  --ref main
  -f "service=$SERVICE"
  -f push=true
)

if [[ -n "$TAG" ]]; then
  args+=( -f "tag=$TAG" )
fi

gh "${args[@]}"

printf 'Image publication requested for %s. Recent runs:\n' "$SERVICE"
gh run list --repo "$REPOSITORY" --workflow "$WORKFLOW" --limit 3
