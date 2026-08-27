"""Tests for run-request validation, investigation plan graph checks, evaluation
contract resolution, and Kubernetes job rendering.

Verifies that invalid inputs, disallowed models, bad resource profiles,
cyclic execution graphs, unapproved mount URIs, tampered evaluation
contracts, and missing guardrail metrics are all rejected before a run
reaches the cluster.  Contract resolution tests check that digest
mismatches and agent-supplied override fields are caught.
"""

import sys
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

for module_name in list(sys.modules):
    if module_name == 'app' or module_name.startswith('app.'):
        del sys.modules[module_name]

from app.registry import WorkflowRegistry
from app.config import Settings
from app.investigation_routes import evaluator_contract_issues
from app.job_submission import (
    KubernetesJobSubmitter,
    _active_deadline_seconds,
    _asset_volume_subpath,
    _research_workspace_asset_locations,
    _research_workspace_volume_mount_specs,
    resolve_evaluation_contract,
)
import app.job_submission as job_submission_module
from app.schemas import InvestigationPlanCreateRequest, InvestigationWorkspaceSpec, RunCreateRequest
from app.validation import validate_run_request
from services.common.schemas import RunManifest
from services.common.schemas import WorkflowRegistryEntry

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize(
    'command',
    [
        'python3 run.py',
        [],
        ['sh', '-lc', 'python3 run.py'],
        ['bash', 'run.py'],
        ['python3', ''],
        ['python3', 'run.py\x00'],
    ],
)
def test_workspace_command_rejects_shell_form_or_unsafe_structure(command) -> None:
    with pytest.raises(ValidationError):
        InvestigationWorkspaceSpec.model_validate(
            {
                'task_bundle': {'uri': 's3://datasets/task.zip', 'sha256': 'a' * 64},
                'source_bundle': {'uri': 's3://artifacts/source.zip', 'sha256': 'b' * 64},
                'command': command,
            }
        )


def test_workspace_command_accepts_bounded_python_argv() -> None:
    workspace = InvestigationWorkspaceSpec.model_validate(
        {
            'task_bundle': {'uri': 's3://datasets/task.zip', 'sha256': 'a' * 64},
            'source_bundle': {'uri': 's3://artifacts/source.zip', 'sha256': 'b' * 64},
            'command': ['python3', 'run.py', '--seed', '17'],
        }
    )
    assert workspace.command == ['python3', 'run.py', '--seed', '17']


def test_active_registry_execution_policy_is_complete_and_immutable() -> None:
    registry = WorkflowRegistry(REPO_ROOT / 'services' / 'workflow-registry' / 'definitions')

    active = [
        entry
        for entry in registry.list_workflows()
        if entry.execution_status == 'ready' and entry.submission_backend == 'kubernetes'
    ]
    assert active
    for entry in active:
        assert '@sha256:' in entry.runner_image
        assert len(entry.runner_image.rsplit('@sha256:', 1)[1]) == 64
        assert entry.default_entrypoint
        assert entry.runner_service_account_name
        assert entry.resource_profile.requests
        assert entry.resource_profile.limits
        assert entry.max_wallclock_minutes > 0


@pytest.mark.parametrize(
    ('update', 'match'),
    [
        ({'runner_image': 'ghcr.io/example/runner:latest'}, 'digest-pinned'),
        ({'allow_custom_image': True}, 'Extra inputs are not permitted'),
        ({'allow_custom_entrypoint': True}, 'Extra inputs are not permitted'),
        ({'default_entrypoint': []}, 'fixed default_entrypoint'),
        ({'runner_service_account_name': ' '}, 'runner_service_account_name'),
        ({'max_wallclock_minutes': None}, 'wall-clock ceiling'),
    ],
)
def test_active_registry_rejects_incomplete_or_custom_execution_policy(
    update: dict[str, object],
    match: str,
) -> None:
    registry = WorkflowRegistry(REPO_ROOT / 'services' / 'workflow-registry' / 'definitions')
    source = registry.get_workflow('research-workspace-cpu-v1')
    assert source is not None
    payload = source.model_dump(mode='json')
    payload.update(update)

    with pytest.raises(ValidationError, match=match):
        WorkflowRegistryEntry.model_validate(payload)


def build_workspace_execution(
    execution_id: str,
    *,
    depends_on: list[str] | None = None,
) -> dict[str, object]:
    return {
        'execution_id': execution_id,
        'objective': f'Execute the frozen {execution_id} workspace.',
        'experiment_type': 'research-workspace-job',
        'workload_id': 'research-workspace-cpu-v1',
        'data_access_scope': 'solve',
        'depends_on': depends_on or [],
        'workspace': {
            'task_bundle': {
                'uri': 's3://datasets/task.zip',
                'sha256': 'a' * 64,
            },
            'source_bundle': {
                'uri': 's3://artifacts/submissions/source.zip',
                'sha256': 'b' * 64,
            },
            'command': ['python3', 'run.py'],
        },
        'budget': {
            'budget_mode': 'wallclock',
            'max_wallclock_minutes': 5,
        },
        'artifact_contract': {'required': ['status.json']},
        'evaluator_contract': {'evaluator_type': 'rubric-gated-v1'},
    }


def test_validation_rejects_missing_inputs_and_bad_resource_profile() -> None:
    registry = WorkflowRegistry(REPO_ROOT / 'services' / 'workflow-registry' / 'definitions')
    workflow = registry.get_workflow('generic-tabular-benchmark')
    assert workflow is not None

    request = RunCreateRequest(
        workflow_id='generic-tabular-benchmark',
        objective='Benchmark approved models on Titanic.',
        inputs={
            'dataset_name': 'titanic',
            'train_uri': 's3://datasets/titanic/train.csv',
        },
        models=['logistic_regression'],
        resource_profile='gpu-small',
    )

    issues = validate_run_request(request, workflow)
    fields = {issue.field for issue in issues}
    assert 'inputs.test_uri' in fields
    assert 'inputs.target_column' in fields
    assert 'resource_profile' in fields


def test_validation_rejects_disallowed_models() -> None:
    registry = WorkflowRegistry(REPO_ROOT / 'services' / 'workflow-registry' / 'definitions')
    workflow = registry.get_workflow('generic-tabular-benchmark')
    assert workflow is not None

    request = RunCreateRequest(
        workflow_id='generic-tabular-benchmark',
        objective='Benchmark approved models on Titanic.',
        inputs={
            'dataset_name': 'titanic',
            'train_uri': 's3://datasets/titanic/train.csv',
            'test_uri': 's3://datasets/titanic/test.csv',
            'target_column': 'Survived',
        },
        models=['made_up_model'],
    )

    issues = validate_run_request(request, workflow)
    assert len(issues) == 1
    assert issues[0].field == 'models'
    assert 'made_up_model' in issues[0].message


def test_validation_accepts_gpu_neural_net_workflow_request() -> None:
    registry = WorkflowRegistry(REPO_ROOT / 'services' / 'workflow-registry' / 'definitions')
    workflow = registry.get_workflow('gpu-experiment')
    assert workflow is not None

    request = RunCreateRequest(
        workflow_id='gpu-experiment',
        objective='Train a bounded neural-net experiment on the approved GPU worker.',
        inputs={
            'dataset_uri': 's3://datasets/neural-net/train',
            'model_family': 'pytorch-template-v1',
            'training_notes': 'Use a single GPU, bounded epochs, and report validation loss.',
        },
        models=['pytorch-template-v1'],
        resource_profile='gpu-small',
    )

    issues = validate_run_request(request, workflow)
    assert issues == []


def test_generic_run_wallclock_budget_becomes_kubernetes_deadline() -> None:
    manifest = RunManifest(
        run_id='run-1',
        workflow_id='research-workspace-cpu-v1',
        workflow_family='research-workspace',
        display_name='Research Workspace CPU v1',
        objective='Execute one frozen research workspace.',
        submitted_by='test-suite',
        submitted_at=datetime.now(timezone.utc),
        inputs={},
        requested_models=['agent-generated-python'],
        resource_profile='cpu-research-medium',
        runner_image='ghcr.io/example/research-workspace:0.1.0',
        runner_service_account_name='registry-runner',
        maximum_wallclock_minutes=20,
        evaluator_type='rubric-gated-v1',
        approval_tier='tier-2-approved-execution',
        expected_artifacts={'required': ['status.json'], 'optional': []},
        experiment_type='research-workspace-job',
        workload_id='research-workspace-cpu-v1',
        entrypoint=['python3', '-m', 'runner'],
        budget={'max_wallclock_minutes': 17},
    )

    assert _active_deadline_seconds(manifest) == 17 * 60

    excessive = manifest.model_copy(update={'budget': {'max_wallclock_minutes': 21}})
    with pytest.raises(ValueError, match='registry ceiling'):
        _active_deadline_seconds(excessive)


def test_evaluation_contract_must_match_trusted_catalog() -> None:
    digest = 'a' * 64
    manifest = RunManifest(
        run_id='run-contract',
        workflow_id='metric-search-v0',
        workflow_family='metric-learning',
        display_name='Metric Search',
        objective='Execute one contract-bound experiment.',
        submitted_by='test-suite',
        submitted_at=datetime.now(timezone.utc),
        inputs={},
        requested_models=['agent-generated-python'],
        resource_profile='gpu-small',
        runner_image='ghcr.io/example/runner:test',
        runner_service_account_name='registry-runner',
        evaluator_type='contract',
        approval_tier='tier-2-approved-execution',
        expected_artifacts={'required': ['metrics.json'], 'optional': []},
        experiment_type='gpu-training-job',
        workload_id='metric-search-v0',
        entrypoint=['python3', 'run.py'],
        config_payload={
            'evaluation_contract': {
                'contract_id': 'example',
                'version': '1.0.0',
                'digest': digest,
            }
        },
        budget={'max_wallclock_minutes': 5},
    )
    trusted = {
        'contract_id': 'example',
        'version': '1.0.0',
        'digest': digest,
        'container_image_digest': f'ghcr.io/example/contract@sha256:{"b" * 64}',
        'execution_wrapper': 'run_contract.py',
        'evaluation_entry_point': 'evaluator.py',
    }
    settings = Settings(
        evaluation_contracts={'example@1.0.0': trusted}
    )
    assert resolve_evaluation_contract(manifest, settings) == trusted

    replaced = manifest.model_copy(
        update={
            'config_payload': {
                'evaluation_contract': {
                    'contract_id': 'example',
                    'version': '1.0.0',
                    'digest': 'c' * 64,
                }
            }
        }
    )
    with pytest.raises(ValueError, match='digest mismatch'):
        resolve_evaluation_contract(replaced, settings)


def test_evaluation_contract_resolves_verified_shared_bundle(
    tmp_path: Path,
) -> None:
    bundle = tmp_path / 'trusted' / 'candidate-v1' / '1.0.0'
    bundle.mkdir(parents=True)
    descriptor = {
        'contract_id': 'candidate-v1',
        'version': '1.0.0',
        'execution_wrapper': 'run_contract.py',
        'evaluation_entry_point': 'evaluator.py',
    }
    (bundle / 'contract.json').write_text(json.dumps(descriptor))
    (bundle / 'run_contract.py').write_text('print("wrapper")\n')
    (bundle / 'evaluator.py').write_text('print("evaluate")\n')
    digest_builder = sha256()
    for path in sorted(bundle.rglob('*')):
        if not path.is_file():
            continue
        relative = path.relative_to(bundle).as_posix().encode()
        content = path.read_bytes()
        digest_builder.update(len(relative).to_bytes(8, 'big'))
        digest_builder.update(relative)
        digest_builder.update(len(content).to_bytes(8, 'big'))
        digest_builder.update(content)
    digest = digest_builder.hexdigest()
    (bundle / 'contract.sha256').write_text(digest + '\n')
    trusted = {
        'contract_id': 'candidate-v1',
        'version': '1.0.0',
        'digest': digest,
        'bundle_path': 'trusted/candidate-v1/1.0.0',
        'execution_wrapper': 'run_contract.py',
        'evaluation_entry_point': 'evaluator.py',
    }
    catalog = tmp_path / 'catalog.json'
    catalog.write_text(json.dumps({'candidate-v1@1.0.0': trusted}))
    manifest = RunManifest(
        run_id='run-shared-contract',
        workflow_id='metric-search-v0',
        workflow_family='metric-learning',
        display_name='Metric Search',
        objective='Execute one shared-bundle contract.',
        submitted_by='test-suite',
        submitted_at=datetime.now(timezone.utc),
        inputs={},
        requested_models=['agent-generated-python'],
        resource_profile='gpu-small',
        runner_image='ghcr.io/example/runner:test',
        runner_service_account_name='registry-runner',
        evaluator_type='contract',
        approval_tier='tier-2-approved-execution',
        expected_artifacts={'required': ['metrics.json'], 'optional': []},
        experiment_type='gpu-training-job',
        workload_id='metric-search-v0',
        entrypoint=['python3', 'run.py'],
        config_payload={
            'evaluation_contract': {
                'contract_id': 'candidate-v1',
                'version': '1.0.0',
                'digest': digest,
            }
        },
        budget={'max_wallclock_minutes': 5},
    )
    settings = Settings(
        evaluation_contract_catalog_path=str(catalog),
        evaluation_contract_bundle_root=str(tmp_path),
    )

    assert resolve_evaluation_contract(manifest, settings) == trusted
    (bundle / 'evaluator.py').write_text('print("tampered")\n')
    with pytest.raises(ValueError, match='bundle digest mismatch'):
        resolve_evaluation_contract(manifest, settings)


def test_evaluation_contract_rejects_agent_supplied_execution_fields() -> None:
    manifest = RunManifest(
        run_id='run-contract-override',
        workflow_id='metric-search-v0',
        workflow_family='metric-learning',
        display_name='Metric Search',
        objective='Reject a replaced evaluation entry point.',
        submitted_by='test-suite',
        submitted_at=datetime.now(timezone.utc),
        inputs={},
        requested_models=['agent-generated-python'],
        resource_profile='gpu-small',
        runner_image='ghcr.io/example/runner:test',
        runner_service_account_name='registry-runner',
        evaluator_type='contract',
        approval_tier='tier-2-approved-execution',
        expected_artifacts={'required': ['metrics.json'], 'optional': []},
        experiment_type='gpu-training-job',
        workload_id='metric-search-v0',
        entrypoint=['python3', 'run.py'],
        config_payload={
            'evaluation_contract': {
                'contract_id': 'example',
                'version': '1.0.0',
                'digest': 'a' * 64,
                'evaluation_entry_point': 'attacker.py',
            }
        },
        budget={'max_wallclock_minutes': 5},
    )
    with pytest.raises(ValueError, match='may contain only'):
        resolve_evaluation_contract(manifest, Settings())


def test_kubernetes_job_mounts_trusted_contract_read_only(monkeypatch) -> None:
    # Renders a full Kubernetes job to verify: (1) the evaluation contract
    # container image is mounted as an init container, (2) the workload
    # command is replaced by the contract execution wrapper, (3) the
    # contract volume is read-only, and (4) the service account token is
    # not automounted.
    digest = 'a' * 64
    trusted = {
        'contract_id': 'example',
        'version': '1.0.0',
        'digest': digest,
        'container_image_digest': f'ghcr.io/example/contract@sha256:{"b" * 64}',
        'execution_wrapper': 'run_contract.py',
        'evaluation_entry_point': 'evaluator.py',
    }
    manifest = RunManifest(
        run_id='run-contract-job',
        workflow_id='metric-search-v0',
        workflow_family='metric-learning',
        display_name='Metric Search',
        objective='Render one immutable contract job.',
        submitted_by='test-suite',
        submitted_at=datetime.now(timezone.utc),
        inputs={},
        requested_models=['agent-generated-python'],
        resource_profile='gpu-small',
        resource_requests={'cpu': '1'},
        resource_limits={'cpu': '1'},
        runner_image='ghcr.io/example/runner:test',
        runner_service_account_name='registry-runner',
        evaluator_type='contract',
        approval_tier='tier-2-approved-execution',
        expected_artifacts={'required': ['metrics.json'], 'optional': []},
        experiment_type='gpu-training-job',
        workload_id='metric-search-v0',
        entrypoint=['python3', 'run.py'],
        config_payload={
            'evaluation_contract': {
                'contract_id': 'example',
                'version': '1.0.0',
                'digest': digest,
            }
        },
        budget={'max_wallclock_minutes': 5},
    )

    class Record(SimpleNamespace):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)

    class BatchApi:
        submitted = None

        def create_namespaced_job(self, *, namespace, body):
            self.submitted = (namespace, body)

    batch = BatchApi()
    client = SimpleNamespace(
        BatchV1Api=lambda: batch,
        CoreV1Api=lambda: Record(),
        **{
            name: Record
            for name in (
                'V1Capabilities',
                'V1Container',
                'V1EmptyDirVolumeSource',
                'V1EnvVar',
                'V1Job',
                'V1JobSpec',
                'V1LocalObjectReference',
                'V1ObjectMeta',
                'V1PersistentVolumeClaimVolumeSource',
                'V1PodSecurityContext',
                'V1PodSpec',
                'V1PodTemplateSpec',
                'V1ResourceRequirements',
                'V1SeccompProfile',
                'V1SecurityContext',
                'V1Volume',
                'V1VolumeMount',
            )
        },
    )
    kube_config = SimpleNamespace(load_incluster_config=lambda: None)
    monkeypatch.setattr(
        job_submission_module,
        '_load_kube_modules',
        lambda: (client, kube_config, RuntimeError, RuntimeError),
    )
    submitter = KubernetesJobSubmitter(
        Settings(evaluation_contracts={'example@1.0.0': trusted})
    )
    submitter.submit_run(manifest)

    _, job = batch.submitted
    pod = job.spec.template.spec
    assert pod.automount_service_account_token is False
    assert pod.service_account_name == 'registry-runner'
    assert pod.containers[0].image == manifest.runner_image
    assert pod.containers[0].resources.requests == manifest.resource_requests
    assert pod.containers[0].resources.limits == manifest.resource_limits
    assert pod.init_containers[0].image == trusted['container_image_digest']
    assert pod.containers[0].command == [
        'python3',
        '/evaluation-contract/run_contract.py',
    ]
    contract_mount = next(
        mount
        for mount in pod.containers[0].volume_mounts
        if mount.name == 'evaluation-contract'
    )
    assert contract_mount.read_only is True


def test_investigation_plan_accepts_acyclic_execution_graph() -> None:
    plan = InvestigationPlanCreateRequest(
        title='Two-stage frozen plan',
        rationale='Pre-register training and evaluation as one approved graph.',
        hypothesis_ids=['hypothesis-1'],
        executions=[
            build_workspace_execution('train'),
            build_workspace_execution('evaluate', depends_on=['train']),
        ],
    )

    assert [execution.execution_id for execution in plan.executions] == [
        'train',
        'evaluate',
    ]


@pytest.mark.parametrize(
    'executions, expected_message',
    [
        (
            [
                build_workspace_execution('train', depends_on=['evaluate']),
                build_workspace_execution('evaluate', depends_on=['train']),
            ],
            'acyclic graph',
        ),
        (
            [build_workspace_execution('evaluate', depends_on=['missing'])],
            'unknown dependencies',
        ),
    ],
)
def test_investigation_plan_rejects_invalid_execution_graph(
    executions: list[dict[str, object]],
    expected_message: str,
) -> None:
    with pytest.raises(ValidationError, match=expected_message):
        InvestigationPlanCreateRequest(
            title='Invalid execution graph',
            rationale='This plan must be rejected before it can be approved.',
            hypothesis_ids=['hypothesis-1'],
            executions=executions,
        )


# Verifies that the workspace runner only sees the sub-paths declared in
# the investigation plan (task bundle, source bundle, dataset bindings)
# and its own writable run directory — no sibling runs are exposed.
def test_research_workspace_mounts_only_declared_asset_subpaths() -> None:
    manifest = RunManifest(
        run_id='run-isolated',
        workflow_id='research-workspace-cpu-v1',
        workflow_family='research-workspace',
        display_name='Research Workspace CPU v1',
        objective='Execute one isolated research workspace.',
        submitted_by='test-suite',
        submitted_at=datetime.now(timezone.utc),
        inputs={},
        requested_models=['agent-generated-python'],
        resource_profile='cpu-research-medium',
        runner_image='ghcr.io/example/research-workspace:0.1.0',
        runner_service_account_name='registry-runner',
        evaluator_type='rubric-gated-v1',
        approval_tier='tier-2-approved-execution',
        expected_artifacts={'required': ['status.json'], 'optional': []},
        experiment_type='research-workspace-job',
        workload_id='research-workspace-cpu-v1',
        schema_ref='glasslab-investigation-workspace-v1',
        entrypoint=['python3', '-m', 'runner'],
        config_payload={
            'workspace': {
                'task_bundle': {
                    'uri': 's3://datasets/benchmarks/adult/task.zip',
                },
                'source_bundle': {
                    'uri': 's3://artifacts/submissions/adult/source.zip',
                },
            },
            'dataset_contracts': [
                {
                    'name': 'adult_train',
                    'asset': {
                        'uri': 's3://datasets/uci-adult/adult.data',
                    },
                }
            ],
        },
        budget={'max_wallclock_minutes': 5},
    )

    assert _research_workspace_asset_locations(manifest) == [
        ('dataset-volume', 'benchmarks/adult/task.zip'),
        ('artifacts-volume', 'submissions/adult/source.zip'),
        ('dataset-volume', 'uci-adult/adult.data'),
    ]
    settings = type(
        'MountSettings',
        (),
        {
            'dataset_mount_path': '/mnt/datasets',
            'artifacts_mount_path': '/mnt/artifacts',
        },
    )()
    assert _research_workspace_volume_mount_specs(manifest, settings) == [
        {
            'name': 'dataset-volume',
            'mount_path': '/mnt/datasets/benchmarks/adult/task.zip',
            'sub_path': 'benchmarks/adult/task.zip',
            'read_only': True,
        },
        {
            'name': 'artifacts-volume',
            'mount_path': '/mnt/artifacts/submissions/adult/source.zip',
            'sub_path': 'submissions/adult/source.zip',
            'read_only': True,
        },
        {
            'name': 'dataset-volume',
            'mount_path': '/mnt/datasets/uci-adult/adult.data',
            'sub_path': 'uci-adult/adult.data',
            'read_only': True,
        },
        {
            'name': 'artifacts-volume',
            'mount_path': '/mnt/artifacts/run-isolated',
            'sub_path': 'run-isolated',
            'read_only': False,
        },
    ]


# Only s3://datasets/ and s3://artifacts/ URIs are allowed; https://,
# file://, and parent-traversal paths are all rejected to prevent the
# agent from referencing assets outside the approved mount roots.
@pytest.mark.parametrize(
    'uri',
    [
        'file:///mnt/datasets/secret.csv',
        'https://example.com/source.zip',
        's3://datasets/../hidden.csv',
        's3://artifacts/../other-run/source.zip',
    ],
)
def test_research_workspace_rejects_unapproved_mount_uri(uri: str) -> None:
    with pytest.raises(ValueError):
        _asset_volume_subpath(uri)


def test_claim_evaluator_contract_requires_primary_metric_and_guardrails() -> None:
    execution_payload = build_workspace_execution('evaluate')
    execution_payload['evaluator_contract'] = {
        'evaluator_type': 'rubric-gated-v1',
        'primary_metric': {
            'name': 'rubric_score',
            'direction': 'maximize',
        },
        'guardrails': [
            {
                'name': 'integrity_pass',
                'minimum': 1,
                'required': True,
            }
        ],
    }
    plan = InvestigationPlanCreateRequest(
        title='Evaluator contract plan',
        rationale='Require an integrity gate before storing scientific claims.',
        hypothesis_ids=['hypothesis-1'],
        executions=[execution_payload],
    )
    execution = plan.executions[0]

    assert evaluator_contract_issues(
        {'rubric_score': 88, 'integrity_pass': 1},
        execution,
    ) == []
    assert evaluator_contract_issues(
        {'rubric_score': 88, 'integrity_pass': 0},
        execution,
    ) == ['guardrail integrity_pass is below minimum 1.0']
    assert evaluator_contract_issues({}, execution) == [
        'primary metric is missing or non-numeric: rubric_score',
        'required guardrail metric is missing: integrity_pass',
    ]
