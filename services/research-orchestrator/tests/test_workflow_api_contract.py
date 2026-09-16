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
from pydantic import ValidationError

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
# The receiver model that validates config_payload['workspace'] inside
# build_generic_run_record; exercised here so the optional workspace payload is
# guarded by the receiver's own nested spec, not only the request's free-form
# config_payload dict.
INVESTIGATION_WORKSPACE_SPEC = WORKFLOW_API_SCHEMAS.InvestigationWorkspaceSpec


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


def test_workspace_payload_validates_against_receiver_workspace_spec() -> None:
    # build_generic_run_record validates config_payload['workspace'] with the
    # receiver's InvestigationWorkspaceSpec. Validate the real sender payload
    # with that same nested model so a workspace-shape drift fails here.
    spec = _spec(workspace=True)
    body = _submission_body(spec)

    workspace = INVESTIGATION_WORKSPACE_SPEC.model_validate(
        body['config_payload']['workspace']
    )

    assert workspace.task_bundle.uri == spec.task_bundle['uri']
    assert workspace.task_bundle.sha256 == spec.task_bundle['sha256']
    assert workspace.source_bundle.uri == spec.source_bundle['uri']
    assert workspace.command == spec.workspace_command
    assert workspace.network_policy == 'none'


@pytest.mark.parametrize('workspace', [True, False])
def test_optional_payload_fields_round_trip_through_receiver_model(
    workspace: bool,
) -> None:
    spec = _spec(workspace=workspace).model_copy(
        update={
            'dataset_bindings': {'train': 'glasslab-dataset://' + 'd' * 64},
            'dataset_contracts': [{'name': 'train', 'role': 'train'}],
            'task_spec': {'display_name': 'Round-trip task'},
        }
    )

    body = _submission_body(spec)
    request = GENERIC_RUN_REQUEST.model_validate(body)

    assert request.budget == {
        'max_wallclock_minutes': spec.resources.wallclock_minutes,
    }
    assert request.dataset_bindings == spec.dataset_bindings
    assert request.campaign_id == spec.run_id
    assert request.config_payload['evaluation_contract'] == {
        'contract_id': spec.evaluation_contract_id,
        'version': spec.evaluation_contract_version,
        'digest': spec.evaluation_contract_digest,
    }
    assert request.metric_contract['evaluation_contract_digest'] == (
        spec.evaluation_contract_digest
    )
    if workspace:
        assert request.config_payload['workspace']['network_policy'] == 'none'
        assert request.config_payload['dataset_contracts'] == (
            spec.dataset_contracts
        )
        assert request.config_payload['task_spec'] == spec.task_spec
    else:
        assert 'workspace' not in request.config_payload
        assert 'dataset_contracts' not in request.config_payload


def _with_unknown_top_level_resources(body: dict) -> dict:
    return {**body, 'resources': {'cpu': 2, 'memory_gib': 4}}


def _without_objective(body: dict) -> dict:
    return {key: value for key, value in body.items() if key != 'objective'}


def _with_budget_as_list(body: dict) -> dict:
    return {**body, 'budget': [60]}


def _with_renamed_campaign_id(body: dict) -> dict:
    return {
        **{key: value for key, value in body.items() if key != 'campaign_id'},
        'campaign': body['campaign_id'],
    }


@pytest.mark.parametrize(
    'mutate',
    [
        _with_unknown_top_level_resources,
        _without_objective,
        _with_budget_as_list,
        _with_renamed_campaign_id,
    ],
    ids=[
        'unknown-top-level-resources',
        'missing-objective',
        'budget-wrong-type',
        'renamed-campaign-id',
    ],
)
def test_receiver_schema_rejects_drifted_submission_bodies(mutate) -> None:
    # Proves the guard above actually catches sender drift: the unmutated body
    # validates, and each representative drift class (the #491 top-level
    # 'resources', a dropped required field, a retyped field, a renamed field)
    # is rejected rather than silently accepted.
    body = _submission_body(_spec(workspace=True))
    GENERIC_RUN_REQUEST.model_validate(body)
    drifted = mutate(body)

    with pytest.raises(ValidationError):
        GENERIC_RUN_REQUEST.model_validate(drifted)


def test_receiver_workspace_spec_rejects_non_python_command() -> None:
    body = _submission_body(_spec(workspace=True))
    body['config_payload']['workspace']['command'] = ['bash', 'run.sh']

    with pytest.raises(ValidationError):
        INVESTIGATION_WORKSPACE_SPEC.model_validate(
            body['config_payload']['workspace']
        )
