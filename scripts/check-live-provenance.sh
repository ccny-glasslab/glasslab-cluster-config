#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "== workflow-api =="
"$ROOT_DIR/scripts/check-workflow-api-provenance.sh"
