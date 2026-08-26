#!/usr/bin/env bash
# Clean-install parity check for the research-orchestrator production image.
#
# Creates a throwaway virtualenv and installs exactly the layers the
# production Dockerfile installs (requirements.txt + requirements-dense.txt +
# the checked-out task-fabric package), then imports and starts the FastAPI
# application the way the container CMD does. This pins the dependency
# boundary: a developer machine with manually installed extras must not be
# the only environment where the service imports and serves.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
SERVICE_DIR="$ROOT_DIR/services/research-orchestrator"
WORK_ROOT="$(mktemp -d)"
cleanup() {
  chmod -R u+w "$WORK_ROOT" 2>/dev/null || true
  rm -rf "$WORK_ROOT"
}
trap cleanup EXIT

printf '[clean-install] creating throwaway virtualenv\n'
python3 -m venv "$WORK_ROOT/venv"
PYTHON="$WORK_ROOT/venv/bin/python"

printf '[clean-install] installing documented production requirements\n'
"$PYTHON" -m pip install --disable-pip-version-check \
  -r "$SERVICE_DIR/requirements.txt" \
  -r "$SERVICE_DIR/requirements-dense.txt"

printf '[clean-install] installing shared task-fabric package from source\n'
"$PYTHON" -m pip install --disable-pip-version-check "$ROOT_DIR/services/task-fabric"

printf '[clean-install] importing and starting the service like the container CMD\n'
cd "$SERVICE_DIR"
env \
  GLASSLAB_ORCHESTRATOR_DATABASE_PATH="$WORK_ROOT/orchestrator.db" \
  GLASSLAB_ORCHESTRATOR_WORKSPACE_ROOT="$WORK_ROOT/runs" \
  GLASSLAB_ORCHESTRATOR_ARTIFACT_ROOT="$WORK_ROOT/artifacts" \
  GLASSLAB_ORCHESTRATOR_PROMOTED_CONTRACT_ROOT="$WORK_ROOT/bundles" \
  GLASSLAB_ORCHESTRATOR_SEALED_CONTRACT_CANDIDATE_ROOT="$WORK_ROOT/contract-candidates" \
  GLASSLAB_ORCHESTRATOR_TRUSTED_CONTRACT_CATALOG_PATH="$WORK_ROOT/catalog.json" \
  GLASSLAB_ORCHESTRATOR_SHARED_MOUNT_ROOT="$WORK_ROOT" \
  GLASSLAB_ORCHESTRATOR_TASK_BUNDLE_ROOT="$WORK_ROOT/task-bundles" \
  GLASSLAB_ORCHESTRATOR_TASK_ASSET_ROOT="$WORK_ROOT/task-assets" \
  GLASSLAB_ORCHESTRATOR_DATASET_UPLOAD_ROOT="$WORK_ROOT/dataset-uploads" \
  GLASSLAB_ORCHESTRATOR_BENCHMARK_DATASET_CATALOG_PATH="$WORK_ROOT/datasets/catalog.json" \
  PYTHONPATH="$SERVICE_DIR" \
  "$PYTHON" - <<'PY'
"""Import + start smoke equivalent to `uvicorn app.main:app` in the image."""

from fastapi.testclient import TestClient

# Import-time surface of the shipped image: numpy backs the dense index
# modules, pymupdf the PDF extraction backend. Both must import from the
# documented requirements alone.
import numpy  # noqa: F401
import pymupdf  # noqa: F401

from app.knowledge_dense import NumpyChunkIndex  # module-level numpy consumer
from app.main import create_app

app = create_app(start_watcher=False)
with TestClient(app) as client:
    response = client.get('/health')
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['status'] == 'ok', body
    assert 'knowledge_dense' in body, body
    print('knowledge_dense health block:', body['knowledge_dense'])

print('clean-install smoke: import + start + /health OK')
PY

printf '[clean-install] OK\n'
