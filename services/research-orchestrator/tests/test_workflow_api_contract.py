"""Cross-schema contract: orchestrator submission body vs workflow-api request.

Issue #491: the fake cluster executor hid a wire-contract defect. The real
``WorkflowApiClusterExecutor`` sent a top-level ``resources`` field on
``POST /experiments/runs`` that workflow-api's ``GenericExperimentRunRequest``
forbids (``extra='forbid'``), so every real submission was rejected with HTTP
422 and no Kubernetes Job was ever created.

These tests build the submission body through the sender's own code path and
validate it with the receiver's pydantic model, so sender/receiver drift fails
in CI instead of only on a live run. The receiver model is loaded directly from
its source file because both services own a top-level ``app`` package and
cannot be imported into one interpreter under the same name.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType

import httpx
import pytest

from app.cluster import WorkflowApiClusterExecutor
from app.schemas import ExpandedJobSpec, ResourceRequest

REPO_ROOT = Path(__file__).resolve().parents[3]
_WORKFLOW_API_SCHEMAS_SOURCE = (
    REPO_ROOT / 'services' / 'workflow-api' / 'app' / 'schemas.py'
)

if str(REPO_ROOT) not in sys.path:
    # workflow-api's schemas.py imports the shared services.common.schemas
    # package, whose import root is the repository root.
    sys.path.insert(0, str(REPO_ROOT))


def _load_workflow_api_schemas() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        'workflow_api_contract_schemas',
        _WORKFLOW_API_SCHEMAS_SOURCE,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(
            f'cannot load workflow-api schemas from {_WORKFLOW_API_SCHEMAS_SOURCE}'
        )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


WORKFLOW_API_SCHEMAS = _load_workflow_api_schemas()
GENERIC_RUN_REQUEST = WORKFLOW_API_SCHEMAS.GenericExperimentRunRequest


def _spec(*, workspace: bool) -> ExpandedJobSpec:
    return ExpandedJobSpec(
        orchestrator_job_id='job-1',
        run_id='run-1',
        action_id='action-1',
        variant_name='candidate',
        seed=17,
        idempotency_key='key-1',
        base_config='configs/candidate.yaml',
        overrides={'learning_rate': 0.01},
        runner_image='example.invalid/stale-runner:v1',
        resources=ResourceRequest(
            cpu=2,
            memory_gib=4,
            gpus=0,
            wallclock_minutes=45,
        ),
        required_artifacts=['metrics.json', 'report.md'],
        evaluation_contract_id='classification-metric-v1',
        evaluation_contract_version='1.0.0',
        evaluation_contract_digest='a' * 64,
        workload_id='workspace-cpu-ml-v1' if workspace else 'gpu-experiment',
        experiment_type=(
            'research-workspace-job' if workspace else 'gpu-training-job'
        ),
        task_bundle=(
            {'uri': 's3://artifacts/task.zip', 'sha256': 'b' * 64}
            if workspace
            else None
        ),
        source_bundle=(
            {'uri': 's3://artifacts/source.zip', 'sha256': 'c' * 64}
            if workspace
            else None
        ),
        workspace_command=['python3', 'run.py'] if workspace else [],
    )


def _submission_body(spec: ExpandedJobSpec) -> dict:
    # Exercise the real submit() body construction over an in-memory transport
    # so the contract under test is the bytes the orchestrator would POST.
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            201,
            json={'run_id': 'external-1', 'status': {'status': 'accepted'}},
        )

    executor = WorkflowApiClusterExecutor(
        base_url='http://workflow-api.test',
        workload_id='gpu-experiment',
        experiment_type='gpu-training-job',
        caller_name='research-orchestrator',
        token='orchestrator-secret',
    )
    transport = httpx.MockTransport(handler)
    executor._client = lambda: httpx.Client(  # type: ignore[method-assign]
        base_url='http://workflow-api.test',
        transport=transport,
    )
    executor.submit(spec)
    return captured


@pytest.mark.parametrize('workspace', [True, False])
def test_submission_body_validates_against_workflow_api_request_schema(workspace: bool) -> None:
    # The 422 in issue #491 was this validation failing on the live path; the
    # test fails if either side drifts (unknown sender key or newly required
    # receiver field type change).
    spec = _spec(workspace=workspace)

    request = GENERIC_RUN_REQUEST.model_validate(_submission_body(spec))

    assert request.workload_id == spec.workload_id
    assert request.experiment_type == spec.experiment_type


@pytest.mark.parametrize('workspace', [True, False])
def test_submission_body_sends_no_fields_workflow_api_forbids(workspace: bool) -> None:
    # GenericExperimentRunRequest sets extra='forbid', so any key outside its
    # declared fields is rejected on the real path. The top-level 'resources'
    # key was exactly that defect; keep the explicit assertion alongside the
    # schema-driven one so a regression names the offending field.
    body = _submission_body(_spec(workspace=workspace))

    assert 'resources' not in body
    assert set(body) <= set(GENERIC_RUN_REQUEST.model_fields)


@pytest.mark.parametrize('workspace', [True, False])
def test_submission_body_supplies_every_required_workflow_api_field(workspace: bool) -> None:
    # Reverse direction: every field workflow-api requires must be present in
    # what the orchestrator sends, or the request never validates.
    required = {
        name
        for name, field in GENERIC_RUN_REQUEST.model_fields.items()
        if field.is_required()
    }
    body = _submission_body(_spec(workspace=workspace))

    assert required <= set(body)


def test_submission_body_keeps_wallclock_inside_budget() -> None:
    # Registry-owned cpu/memory/gpus requests and limits are bound server-side,
    # but the per-job wallclock value only reaches workflow-api through
    # budget.max_wallclock_minutes (validated there against the registry
    # ceiling and rendered as the Job deadline). Dropping the forbidden
    # 'resources' field must not also drop this value.
    spec = _spec(workspace=True)

    body = _submission_body(spec)

    assert body['budget'] == {
        'max_wallclock_minutes': spec.resources.wallclock_minutes,
    }
