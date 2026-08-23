#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODE="default"

usage() {
  cat <<'USAGE'
Usage: check-before-push.sh [--default] [--docs] [--configs] [--python-core]

Run the fast local checks that mirror the default Glasslab CI signal.

Modes:
  --default      Run configs, docs, shell syntax, Python syntax, workflow-api core tests.
  --docs         Check Markdown links only.
  --configs      Validate current YAML and JSON only.
  --python-core  Run service Python syntax and workflow-api core tests only.

Default mode is --default.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --default)
      MODE="default"
      shift
      ;;
    --docs)
      MODE="docs"
      shift
      ;;
    --configs)
      MODE="configs"
      shift
      ;;
    --python-core)
      MODE="python-core"
      shift
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      printf '[check-before-push] unknown argument: %s\n' "$1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

cd "$ROOT_DIR"

run_configs() {
  printf '[check-before-push] validating YAML/JSON\n'
  python3 scripts/validate-configs.py
}

run_credential_hygiene() {
  printf '[check-before-push] scanning credential hygiene\n'
  python3 scripts/check-credential-hygiene.py .
}

run_secret_boundary_tests() {
  printf '[check-before-push] running secret process and recovery boundary tests\n'
  python3 -m unittest \
    tests.security.test_secret_process_boundaries \
    tests.security.test_secret_backup_restore \
    tests.security.test_lab_security_agent \
    tests.scripts.test_glasslab_opencode \
    tests.security.test_workflow_security_manifests \
    tests.security.test_task_fabric_manifests \
    -v
}

run_docs() {
  printf '[check-before-push] checking Markdown links\n'
  python3 scripts/check-doc-links.py
}

run_shell() {
  printf '[check-before-push] checking shell syntax\n'
  bash -n \
    scripts/check-before-push.sh \
    scripts/glasslab-opencode.sh \
    scripts/lab-security-agent \
    scripts/research-session-cli.sh \
    scripts/smoke-test-research-orchestrator.sh \
    scripts/submit-learning-task.sh \
    scripts/submit-sample-experiment.sh
}

run_python_syntax() {
  printf '[check-before-push] checking Python syntax\n'
  python3 <<'PY'
from pathlib import Path

failures = []
for path in Path('services').rglob('*.py'):
    try:
        compile(path.read_text(), str(path), 'exec')
    except Exception as exc:
        failures.append((str(path), str(exc)))

if failures:
    for path, exc in failures:
        print(f'{path}: {exc}')
    raise SystemExit(1)

print('All Python files compiled successfully.')
PY
}

run_workflow_api_tests() {
  printf '[check-before-push] running core service tests\n'
  (
    cd services/workflow-api
    PYTHONPATH="../..:.${PYTHONPATH:+:$PYTHONPATH}" pytest \
      -p no:cacheprovider \
      tests/test_api.py \
      tests/test_persistence.py \
      tests/test_run_artifacts.py \
      tests/test_schedule_execution.py \
      tests/test_validation.py \
      -q
  )
  (
    cd services/task-fabric
    # Shared task-fabric protocol package: stdlib-only, no service imports.
    PYTHONPATH=".${PYTHONPATH:+:$PYTHONPATH}" pytest \
      -p no:cacheprovider \
      tests \
      -q
  )
  (
    cd services/research-workspace-runner
    PYTHONPATH=".${PYTHONPATH:+:$PYTHONPATH}" pytest \
      -p no:cacheprovider \
      tests/test_runner.py \
      -q
  )
  (
    cd services/research-orchestrator
    # Narrow structural gate for the SQLite/PostgreSQL ResearchStore surface.
    mypy --config-file mypy-research-store.ini
    contract_test_root="$(mktemp -d)"
    trap 'chmod -R u+w "$contract_test_root" 2>/dev/null || true; rm -rf "$contract_test_root"' EXIT
    GLASSLAB_ORCHESTRATOR_DATABASE_PATH="$contract_test_root/orchestrator.db" \
    GLASSLAB_ORCHESTRATOR_WORKSPACE_ROOT="$contract_test_root/runs" \
    GLASSLAB_ORCHESTRATOR_ARTIFACT_ROOT="$contract_test_root/artifacts" \
    GLASSLAB_ORCHESTRATOR_PROMOTED_CONTRACT_ROOT="$contract_test_root/bundles" \
    GLASSLAB_ORCHESTRATOR_SEALED_CONTRACT_CANDIDATE_ROOT="$contract_test_root/contract-candidates" \
    GLASSLAB_ORCHESTRATOR_TRUSTED_CONTRACT_CATALOG_PATH="$contract_test_root/catalog.json" \
    GLASSLAB_ORCHESTRATOR_SHARED_MOUNT_ROOT="$contract_test_root" \
    GLASSLAB_ORCHESTRATOR_TASK_BUNDLE_ROOT="$contract_test_root/task-bundles" \
    GLASSLAB_ORCHESTRATOR_TASK_ASSET_ROOT="$contract_test_root/task-assets" \
    GLASSLAB_ORCHESTRATOR_DATASET_UPLOAD_ROOT="$contract_test_root/dataset-uploads" \
    GLASSLAB_ORCHESTRATOR_BENCHMARK_DATASET_CATALOG_PATH="$contract_test_root/datasets/catalog.json" \
    PYTHONPATH=".${PYTHONPATH:+:$PYTHONPATH}" pytest \
      -p no:cacheprovider \
      tests \
      -q
  )
}

case "$MODE" in
  default)
    run_configs
    run_credential_hygiene
    run_secret_boundary_tests
    run_docs
    run_shell
    run_python_syntax
    run_workflow_api_tests
    ;;
  docs)
    run_docs
    ;;
  configs)
    run_configs
    ;;
  python-core)
    run_shell
    run_python_syntax
    run_workflow_api_tests
    ;;
esac

printf '[check-before-push] ok\n'
