"""Kubernetes Job submission and lifecycle observation.

Translates validated RunManifest records into Kubernetes Job specs with
appropriate volumes, resource requests, security contexts, and evaluation
contract bindings. The submitter is the only code path that touches the
Kubernetes API; all callers interact through the abstract submit_run /
get_live_status / get_live_logs interface. Supports null mode for local
development without a cluster.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path, PurePosixPath
import re
import socket
import ssl
from typing import Any

import urllib3

from services.common.schemas import RunManifest, RunStatus

from .config import Settings
from .schemas import JobSubmissionReceipt, LogEntry, RunRecord


class LiveStatusUnavailableError(Exception):
    """The Kubernetes live-status lookup failed for an expected infrastructure
    reason (API error, DNS/connection failure, or TLS transport failure).

    Raised instead of returning a fabricated status so callers can surface the
    durable stored status with an explicit degradation note. Deliberately not a
    subclass of the transport exceptions below, and never raised for
    programming errors, which continue to propagate unchanged.
    """


# Transport exceptions the Kubernetes Python client raises for expected
# infrastructure outages, kept distinct from ApiException and from unrelated
# programming errors. urllib3.MaxRetryError is the umbrella for DNS,
# connection-refused, timeout, and TLS failures; the narrower classes cover
# connection timeouts, read timeouts, and the raw stdlib equivalents that
# occasionally escape urllib3's wrapping.
_KUBE_TRANSPORT_EXCEPTIONS = (
    urllib3.exceptions.MaxRetryError,
    urllib3.exceptions.NewConnectionError,
    urllib3.exceptions.ConnectionError,
    urllib3.exceptions.ConnectTimeoutError,
    urllib3.exceptions.ReadTimeoutError,
    urllib3.exceptions.SSLError,
    ssl.SSLError,
    socket.gaierror,
)


def workflow_submission_ready(workflow: RunManifest | Any) -> tuple[bool, list[str]]:
    blockers = list(getattr(workflow, 'execution_blockers', []) or [])
    execution_status = str(getattr(workflow, 'execution_status', 'ready')).strip()
    submission_backend = str(getattr(workflow, 'submission_backend', 'unimplemented')).strip()

    if execution_status != 'ready':
        blockers.append(f'workflow execution_status is {execution_status}')
    if submission_backend != 'kubernetes':
        blockers.append(f'workflow submission_backend is {submission_backend}')

    return (not blockers, blockers)


class JobSubmitter(ABC):
    @abstractmethod
    def submit_run(self, manifest: RunManifest) -> JobSubmissionReceipt:
        raise NotImplementedError

    def get_live_status(self, record: RunRecord) -> RunStatus | None:
        return None

    def get_live_logs(self, record: RunRecord) -> list[LogEntry]:
        return []

    def cancel_run(self, record: RunRecord) -> None:
        """Cancel the submitted workload, or raise when cancellation is uncertain."""
        raise NotImplementedError('job submitter does not support cancellation')


class NullJobSubmitter(JobSubmitter):
    def __init__(self, namespace: str) -> None:
        self.namespace = namespace

    def submit_run(self, manifest: RunManifest) -> JobSubmissionReceipt:
        return JobSubmissionReceipt(
            job_name=f'{manifest.workflow_id}-{manifest.run_id[:8]}',
            namespace=self.namespace,
            accepted_at=datetime.now(timezone.utc),
            status='accepted',
            detail='Job submission interface is present but not wired to Kubernetes yet.',
        )

    def cancel_run(self, record: RunRecord) -> None:
        return None


def _load_kube_modules() -> tuple[Any, Any, type[Exception], type[Exception]]:
    from kubernetes import client as kube_client
    from kubernetes import config as kube_config
    from kubernetes.client.exceptions import ApiException
    from kubernetes.config.config_exception import ConfigException as KubeConfigException

    return kube_client, kube_config, KubeConfigException, ApiException


def _load_kube_config(kube_config: Any, config_exception: type[Exception]) -> None:
    try:
        kube_config.load_incluster_config()
    except config_exception:
        kube_config.load_kube_config()


def _sanitize_label(value: str) -> str:
    cleaned = re.sub(r'[^a-zA-Z0-9-.]+', '-', value).strip('-').lower()
    return cleaned[:63] or 'run'


def _build_job_name(manifest: RunManifest) -> str:
    prefix = _sanitize_label(manifest.workflow_id).replace('.', '-')
    return f"{prefix}-{manifest.run_id[:8]}"[:63]


def resolve_evaluation_contract(
    manifest: RunManifest,
    settings: Settings,
) -> dict[str, str] | None:
    # Evaluation contracts are immutable and digest-pinned. The workflow can
    # request a specific (contract_id, version, digest) triple, but only the
    # trusted catalog (static env + optional JSON file) is consulted; the
    # workflow never specifies the contract path or image directly.
    requested = manifest.config_payload.get('evaluation_contract')
    if requested is None:
        return None
    if not isinstance(requested, dict):
        raise ValueError('evaluation_contract must be an object')
    allowed_keys = {'contract_id', 'version', 'digest'}
    if set(requested) != allowed_keys:
        raise ValueError(
            'evaluation_contract may contain only contract_id, version, and digest'
        )
    contract_id = str(requested.get('contract_id', '')).strip()
    version = str(requested.get('version', '')).strip()
    digest = str(requested.get('digest', '')).strip().lower()
    key = f'{contract_id}@{version}'
    catalog = dict(settings.evaluation_contracts)
    if settings.evaluation_contract_catalog_path:
        catalog_path = Path(settings.evaluation_contract_catalog_path)
        if catalog_path.is_file():
            try:
                dynamic = json.loads(catalog_path.read_text(encoding='utf-8'))
            except (OSError, json.JSONDecodeError) as exc:
                raise ValueError('trusted evaluation contract catalog is invalid') from exc
            if not isinstance(dynamic, dict):
                raise ValueError('trusted evaluation contract catalog must be an object')
            catalog.update(dynamic)
    trusted = catalog.get(key)
    if trusted is None:
        raise ValueError(f'evaluation contract is not trusted: {key}')
    common = {
        'contract_id',
        'version',
        'digest',
        'execution_wrapper',
        'evaluation_entry_point',
    }
    image_keys = common | {'container_image_digest'}
    bundle_keys = common | {'bundle_path'}
    if frozenset(trusted) not in {frozenset(image_keys), frozenset(bundle_keys)}:
        raise ValueError(f'trusted evaluation contract is malformed: {key}')
    if (
        trusted['contract_id'] != contract_id
        or trusted['version'] != version
        or trusted['digest'].lower() != digest
    ):
        raise ValueError(f'evaluation contract identity or digest mismatch: {key}')
    if 'container_image_digest' in trusted:
        image = trusted['container_image_digest']
        if '@sha256:' not in image:
            raise ValueError(f'evaluation contract image is not digest-pinned: {key}')
    else:
        relative = PurePosixPath(trusted['bundle_path'])
        if (
            relative.is_absolute()
            or '..' in relative.parts
            or relative.as_posix() in {'', '.'}
        ):
            raise ValueError(f'evaluation contract has unsafe bundle_path: {key}')
        root = Path(settings.evaluation_contract_bundle_root).resolve()
        bundle = (root / relative.as_posix()).resolve()
        if not bundle.is_relative_to(root) or bundle.is_symlink() or not bundle.is_dir():
            raise ValueError(f'evaluation contract bundle is unavailable: {key}')
        digest_builder = sha256()
        files = sorted(
            path
            for path in bundle.rglob('*')
            if (
                path.is_file()
                and path.name != 'contract.sha256'
                and '__pycache__' not in path.relative_to(bundle).parts
                and path.suffix != '.pyc'
            )
        )
        if not files:
            raise ValueError(f'evaluation contract bundle is empty: {key}')
        for path in files:
            if path.is_symlink():
                raise ValueError(f'evaluation contract bundle contains a symlink: {key}')
            relative_file = path.relative_to(bundle).as_posix().encode()
            content = path.read_bytes()
            digest_builder.update(len(relative_file).to_bytes(8, 'big'))
            digest_builder.update(relative_file)
            digest_builder.update(len(content).to_bytes(8, 'big'))
            digest_builder.update(content)
        actual_digest = digest_builder.hexdigest()
        checksum_path = bundle / 'contract.sha256'
        descriptor_path = bundle / 'contract.json'
        if not checksum_path.is_file() or not descriptor_path.is_file():
            raise ValueError(f'evaluation contract bundle is incomplete: {key}')
        if (
            checksum_path.read_text(encoding='ascii').strip().lower() != digest
            or actual_digest != digest
        ):
            raise ValueError(f'evaluation contract bundle digest mismatch: {key}')
        try:
            descriptor = json.loads(descriptor_path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f'evaluation contract descriptor is invalid: {key}') from exc
        if (
            not isinstance(descriptor, dict)
            or descriptor.get('contract_id') != contract_id
            or descriptor.get('version') != version
            or descriptor.get('execution_wrapper') != trusted['execution_wrapper']
            or descriptor.get('evaluation_entry_point')
            != trusted['evaluation_entry_point']
        ):
            raise ValueError(f'evaluation contract descriptor mismatch: {key}')
    for field in ('execution_wrapper', 'evaluation_entry_point'):
        path = PurePosixPath(trusted[field])
        if path.is_absolute() or '..' in path.parts or path.as_posix() in {'', '.'}:
            raise ValueError(f'evaluation contract has unsafe {field}: {key}')
    return dict(trusted)


def _build_runner_spec(manifest: RunManifest, settings: Settings) -> dict:
    if manifest.workflow_id == 'generic-tabular-benchmark':
        dataset_name = str(manifest.inputs.get('dataset_name', '')).strip()
        if not dataset_name:
            raise ValueError('generic-tabular-benchmark requires dataset_name for runner submission')

        return {
            'pipeline': 'titanic_baseline' if dataset_name == 'titanic' else 'generic_tabular_benchmark',
            'dataset': dataset_name,
            'models': manifest.requested_models,
            'feature_profile': 'basic',
            'resource_profile': manifest.resource_profile,
            'compare_to': 'none',
            'produce_submission': True,
        }

    if manifest.workflow_id == 'literature-to-experiment':
        paper_id = str(manifest.inputs.get('paper_id', '')).strip()
        source_notes = str(manifest.inputs.get('source_notes', '')).strip()
        dataset_uri = str(manifest.inputs.get('dataset_uri', '')).strip()
        if not paper_id:
            raise ValueError('literature-to-experiment requires paper_id for runner submission')
        if not source_notes:
            raise ValueError('literature-to-experiment requires source_notes for runner submission')
        if not dataset_uri:
            raise ValueError('literature-to-experiment requires dataset_uri for runner submission')

        # Resolve dataset_uri to actual path
        resolved_dataset_uri = resolve_dataset_uri(dataset_uri, settings)

        return {
            'pipeline': 'literature_to_experiment',
            'dataset': resolved_dataset_uri,
            'paper_id': paper_id,
            'source_notes': source_notes,
            'dataset_uri': resolved_dataset_uri,
            'models': manifest.requested_models,
            'feature_profile': 'basic',
            'resource_profile': manifest.resource_profile,
            'compare_to': 'none',
            'produce_submission': False,
        }

    if manifest.workflow_id == 'gpu-experiment':
        dataset_uri = str(manifest.inputs.get('dataset_uri', '')).strip()
        model_family = str(manifest.inputs.get('model_family', '')).strip()
        training_notes = str(manifest.inputs.get('training_notes', '')).strip()
        evaluation_target = str(manifest.inputs.get('evaluation_target', '')).strip()
        validation_strategy = str(manifest.inputs.get('validation_strategy', '')).strip()
        validation_split = str(manifest.inputs.get('validation_split', '')).strip()
        technique_candidate_models = manifest.inputs.get('technique_candidate_models', [])
        technique_baseline_models = manifest.inputs.get('technique_baseline_models', [])
        technique_loss_or_distance = str(manifest.inputs.get('technique_loss_or_distance', '')).strip()
        technique_task_type = str(manifest.inputs.get('technique_task_type', '')).strip()
        technique_metrics = manifest.inputs.get('technique_metrics', [])
        if not dataset_uri:
            raise ValueError('gpu-experiment requires dataset_uri for runner submission')
        if not model_family:
            raise ValueError('gpu-experiment requires model_family for runner submission')
        if not training_notes:
            raise ValueError('gpu-experiment requires training_notes for runner submission')

        # Resolve dataset_uri to actual path
        resolved_dataset_uri = resolve_dataset_uri(dataset_uri, settings)

        return {
            'pipeline': 'gpu_experiment',
            'dataset': resolved_dataset_uri,
            'dataset_uri': resolved_dataset_uri,
            'model_family': model_family,
            'training_notes': training_notes,
            'evaluation_target': evaluation_target,
            'validation_strategy': validation_strategy,
            'validation_split': validation_split,
            'technique_candidate_models': technique_candidate_models,
            'technique_baseline_models': technique_baseline_models,
            'technique_loss_or_distance': technique_loss_or_distance,
            'technique_task_type': technique_task_type,
            'technique_metrics': technique_metrics,
            'models': manifest.requested_models,
            'feature_profile': 'gpu_ml',
            'resource_profile': manifest.resource_profile,
            'compare_to': 'baseline',
            'produce_submission': False,
        }

    raise ValueError(f'workflow job submission is not implemented yet for {manifest.workflow_id}')


def _is_generic_experiment_manifest(manifest: RunManifest) -> bool:
    return bool(manifest.experiment_type or manifest.workload_id)


def _active_deadline_seconds(manifest: RunManifest) -> int | None:
    raw_minutes = manifest.budget.get('max_wallclock_minutes')
    if raw_minutes is None:
        return None
    if (
        not isinstance(raw_minutes, int)
        or isinstance(raw_minutes, bool)
        or raw_minutes < 1
    ):
        raise ValueError('budget.max_wallclock_minutes must be a positive integer')
    if (
        manifest.maximum_wallclock_minutes is not None
        and raw_minutes > manifest.maximum_wallclock_minutes
    ):
        raise ValueError('budget.max_wallclock_minutes exceeds the registry ceiling')
    return raw_minutes * 60


def _is_research_workspace_manifest(manifest: RunManifest) -> bool:
    return manifest.schema_ref in {
        'glasslab-investigation-workspace-v1',
        'glasslab-benchmark-workspace-v1',
    }


def _asset_volume_subpath(uri: str) -> tuple[str, str]:
    if uri.startswith(('s3://datasets/', 's3://glasslab-datasets/')):
        volume_name = 'dataset-volume'
        relative = (
            uri.removeprefix('s3://datasets/')
            if uri.startswith('s3://datasets/')
            else uri.removeprefix('s3://glasslab-datasets/')
        )
    elif uri.startswith('s3://artifacts/'):
        volume_name = 'artifacts-volume'
        relative = uri.removeprefix('s3://artifacts/')
    else:
        raise ValueError(
            'research workspace assets must use an approved data or artifact URI'
        )
    path = PurePosixPath(relative)
    if (
        not relative
        or path.as_posix() == '.'
        or path.is_absolute()
        or '..' in path.parts
    ):
        raise ValueError(f'research workspace asset URI has an invalid path: {uri}')
    return volume_name, path.as_posix()


def _research_workspace_asset_locations(
    manifest: RunManifest,
) -> list[tuple[str, str]]:
    workspace = manifest.config_payload.get('workspace')
    if not isinstance(workspace, dict):
        raise ValueError('research workspace manifest is missing workspace config')
    references: list[Any] = [
        workspace.get('task_bundle'),
        workspace.get('source_bundle'),
    ]
    dataset_contracts = manifest.config_payload.get('dataset_contracts', [])
    if not isinstance(dataset_contracts, list):
        raise ValueError('research workspace dataset_contracts must be a list')
    references.extend(
        contract.get('asset')
        for contract in dataset_contracts
        if isinstance(contract, dict)
    )
    locations: list[tuple[str, str]] = []
    for reference in references:
        if not isinstance(reference, dict):
            raise ValueError('research workspace asset reference is missing')
        uri = str(reference.get('uri', '')).strip()
        locations.append(_asset_volume_subpath(uri))
    return list(dict.fromkeys(locations))


def _research_workspace_volume_mount_specs(
    manifest: RunManifest,
    settings: Settings,
) -> list[dict[str, str | bool]]:
    input_mounts: list[dict[str, str | bool]] = []
    for volume_name, subpath in _research_workspace_asset_locations(manifest):
        mount_root = (
            settings.dataset_mount_path
            if volume_name == 'dataset-volume'
            else settings.artifacts_mount_path
        )
        input_mounts.append(
            {
                'name': volume_name,
                'mount_path': f'{mount_root}/{subpath}',
                'sub_path': subpath,
                'read_only': True,
            }
        )
    return [
        *input_mounts,
        {
            'name': 'artifacts-volume',
            'mount_path': f'{settings.artifacts_mount_path}/{manifest.run_id}',
            'sub_path': manifest.run_id,
            'read_only': False,
        },
    ]


def resolve_dataset_uri(dataset_uri: str, settings: Settings) -> str:
    """Resolve dataset aliases that are backed by the mounted dataset plane."""
    if dataset_uri.startswith('s3://datasets/'):
        path = dataset_uri.removeprefix('s3://datasets/')
        return f'{settings.dataset_mount_path}/{path}'
    if dataset_uri.startswith('s3://glasslab-datasets/'):
        path = dataset_uri.removeprefix('s3://glasslab-datasets/')
        return f'{settings.dataset_mount_path}/{path}'
    if dataset_uri.startswith('s3://artifacts/'):
        path = dataset_uri.removeprefix('s3://artifacts/')
        return f'{settings.artifacts_mount_path}/{path}'
    return dataset_uri


def validate_workflow_submission_support(workflow: Any, settings: Settings) -> list[str]:
    _, blockers = workflow_submission_ready(workflow)
    if blockers:
        return blockers
    if getattr(workflow, 'experiment_type', None):
        if not list(getattr(workflow, 'default_entrypoint', []) or []):
            blockers.append('generic workload is missing a default_entrypoint')
        return blockers

    placeholder_inputs: dict[str, Any] = {}
    for input_spec in getattr(workflow, 'required_inputs', []) or []:
        input_type = getattr(input_spec, 'input_type', 'text')
        name = getattr(input_spec, 'name', 'input')
        if input_type in {'dataset', 'url'}:
            placeholder_inputs[name] = f's3://placeholder/{name}'
        elif input_type == 'notes':
            placeholder_inputs[name] = f'placeholder {name} notes'
        elif input_type == 'parameter_set':
            placeholder_inputs[name] = f'placeholder_{name}'
        else:
            placeholder_inputs[name] = f'placeholder-{name}'

    try:
        manifest = RunManifest(
            run_id='preflight-dry-run',
            workflow_id=workflow.workflow_id,
            workflow_family=workflow.workflow_family,
            display_name=workflow.display_name,
            objective='dry-run submission validation',
            submitted_by='workflow-api-preflight',
            submitted_at=datetime.now(timezone.utc),
            run_priority='user',
            inputs=placeholder_inputs,
            requested_models=list(workflow.allowed_models[:1]),
            resource_profile=workflow.resource_profile.profile_name,
            resource_requests=workflow.resource_profile.requests,
            resource_limits=workflow.resource_profile.limits,
            node_selector=workflow.resource_profile.node_selector,
            runner_image=workflow.runner_image,
            runner_service_account_name=workflow.runner_service_account_name,
            maximum_wallclock_minutes=workflow.max_wallclock_minutes,
            entrypoint=list(workflow.default_entrypoint),
            evaluator_type=workflow.evaluator_type,
            approval_tier=workflow.approval_tier,
            expected_artifacts=workflow.expected_artifacts.model_dump(mode='json'),
        )
        _build_runner_spec(manifest, settings)
    except Exception as exc:
        blockers.append(f'workflow submission contract is not implemented: {exc}')
    return blockers


class KubernetesJobSubmitter(JobSubmitter):
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client, kube_config, config_exception, self.api_exception = _load_kube_modules()
        _load_kube_config(kube_config, config_exception)
        self.batch_api = self.client.BatchV1Api()
        self.core_api = self.client.CoreV1Api()

    def submit_run(self, manifest: RunManifest) -> JobSubmissionReceipt:
        evaluation_contract = resolve_evaluation_contract(manifest, self.settings)
        job_name = _build_job_name(manifest)
        labels = {
            'app.kubernetes.io/name': 'glasslab-v2-runner',
            'glasslab.io/run-id': manifest.run_id,
            'glasslab.io/workflow-id': _sanitize_label(manifest.workflow_id),
            'glasslab.io/run-priority': manifest.run_priority,
        }
        if manifest.workload_id:
            labels['glasslab.io/workload-id'] = _sanitize_label(manifest.workload_id)
        workspace_config = manifest.config_payload.get('workspace')
        if isinstance(workspace_config, dict):
            network_policy = str(workspace_config.get('network_policy', '')).strip()
            if network_policy:
                labels['glasslab.io/network-policy'] = _sanitize_label(network_policy)

        priority_class_name = ''
        if manifest.run_priority == 'autonomous':
            priority_class_name = self.settings.autonomous_priority_class_name
        else:
            priority_class_name = self.settings.user_priority_class_name
        env = [
            self.client.V1EnvVar(name='GLASSLAB_RUNNER_EXPERIMENT_ID', value=manifest.run_id),
            self.client.V1EnvVar(name='GLASSLAB_RUNNER_TRACE_ID', value=manifest.run_id),
            self.client.V1EnvVar(name='GLASSLAB_RUNNER_MANIFEST_JSON', value=manifest.model_dump_json()),
            self.client.V1EnvVar(name='GLASSLAB_RUNNER_ARTIFACTS_ROOT', value=self.settings.artifacts_mount_path),
        ]

        if _is_generic_experiment_manifest(manifest):
            # Resolve dataset bindings URIs
            resolved_dataset_bindings = {}
            if manifest.dataset_bindings:
                for binding_name, dataset_uri in manifest.dataset_bindings.items():
                    resolved_dataset_bindings[binding_name] = resolve_dataset_uri(str(dataset_uri), self.settings)
            
            env.extend(
                [
                    self.client.V1EnvVar(
                        name='GLASSLAB_GENERIC_CONFIG_JSON',
                        value=json.dumps(manifest.config_payload, sort_keys=True),
                    ),
                    self.client.V1EnvVar(
                        name='GLASSLAB_GENERIC_DATASET_BINDINGS_JSON',
                        value=json.dumps(resolved_dataset_bindings or {}, sort_keys=True),
                    ),
                    self.client.V1EnvVar(
                        name='GLASSLAB_GENERIC_BUDGET_JSON',
                        value=json.dumps(manifest.budget, sort_keys=True),
                    ),
                    self.client.V1EnvVar(
                        name='GLASSLAB_GENERIC_METRIC_CONTRACT_JSON',
                        value=json.dumps(manifest.metric_contract, sort_keys=True),
                    ),
                    self.client.V1EnvVar(name='GLASSLAB_DATASET_ROOT', value=self.settings.dataset_mount_path),
                ]
            )
        else:
            spec = _build_runner_spec(manifest, self.settings)
            env.extend(
                [
                    self.client.V1EnvVar(name='GLASSLAB_RUNNER_SPEC_JSON', value=json.dumps(spec, sort_keys=True)),
                    self.client.V1EnvVar(
                        name='GLASSLAB_RUNNER_DATASET_ROOT',
                        value=f"{self.settings.dataset_mount_path}/{spec['dataset']}",
                    ),
                ]
            )

        container = self.client.V1Container(
            name='runner',
            image=manifest.runner_image,
            image_pull_policy=self.settings.runner_image_pull_policy,
            env=env,
            resources=self.client.V1ResourceRequirements(
                requests=manifest.resource_requests or None,
                limits=manifest.resource_limits or None,
            ),
            # Container runs unprivileged: no escalation, no capabilities.
            security_context=self.client.V1SecurityContext(
                allow_privilege_escalation=False,
                capabilities=self.client.V1Capabilities(drop=['ALL']),
            ),
        )
        if manifest.entrypoint:
            container.command = manifest.entrypoint
        original_entrypoint = list(container.command or [])
        init_containers = None
        if evaluation_contract:
            contract_mount = '/evaluation-contract'
            wrapper = f"{contract_mount}/{evaluation_contract['execution_wrapper']}"
            evaluator = (
                f"{contract_mount}/"
                f"{evaluation_contract['evaluation_entry_point']}"
            )
            env.extend(
                [
                    self.client.V1EnvVar(
                        name='GLASSLAB_EXPERIMENT_ENTRYPOINT_JSON',
                        value=json.dumps(original_entrypoint),
                    ),
                    self.client.V1EnvVar(
                        name='GLASSLAB_EVALUATION_ENTRY_POINT',
                        value=evaluator,
                    ),
                    self.client.V1EnvVar(
                        name='GLASSLAB_EVALUATION_CONTRACT_ID',
                        value=evaluation_contract['contract_id'],
                    ),
                    self.client.V1EnvVar(
                        name='GLASSLAB_EVALUATION_CONTRACT_VERSION',
                        value=evaluation_contract['version'],
                    ),
                    self.client.V1EnvVar(
                        name='GLASSLAB_EVALUATION_CONTRACT_DIGEST',
                        value=evaluation_contract['digest'],
                    ),
                ]
            )
            container.env = env
            container.command = ['python3', wrapper]
            if 'container_image_digest' in evaluation_contract:
                init_containers = [
                    self.client.V1Container(
                    name='evaluation-contract',
                    image=evaluation_contract['container_image_digest'],
                    image_pull_policy=self.settings.runner_image_pull_policy,
                    command=[
                        '/bin/sh',
                        '-c',
                        'cp -a /contract/. /contract-copy/',
                    ],
                    security_context=self.client.V1SecurityContext(
                        allow_privilege_escalation=False,
                        read_only_root_filesystem=True,
                        run_as_non_root=True,
                        capabilities=self.client.V1Capabilities(drop=['ALL']),
                    ),
                    volume_mounts=[
                        self.client.V1VolumeMount(
                            name='evaluation-contract',
                            mount_path='/contract-copy',
                        )
                    ],
                    )
                ]

        research_workspace = _is_research_workspace_manifest(manifest)

        runtime_class_name = None
        requested_gpu = manifest.resource_requests.get('nvidia.com/gpu') or manifest.resource_limits.get('nvidia.com/gpu')
        if requested_gpu:
            runtime_class_name = self.settings.gpu_runtime_class_name

        pod_spec = self.client.V1PodSpec(
            restart_policy='Never',
            service_account_name=manifest.runner_service_account_name,
            automount_service_account_token=False,
            image_pull_secrets=[self.client.V1LocalObjectReference(name=self.settings.image_pull_secret_name)],
            containers=[container],
            init_containers=init_containers,
            priority_class_name=priority_class_name or None,
            runtime_class_name=runtime_class_name,
            node_selector=manifest.node_selector or None,
            # RuntimeDefault seccomp profile: the workload cannot install
            # custom seccomp filters or bypass the pod-level sandbox.
            security_context=self.client.V1PodSecurityContext(
                seccomp_profile=self.client.V1SeccompProfile(type='RuntimeDefault'),
            ),
            volumes=[
                self.client.V1Volume(
                    name='dataset-volume',
                    persistent_volume_claim=self.client.V1PersistentVolumeClaimVolumeSource(
                        claim_name=self.settings.dataset_pvc_name,
                    ),
                ),
                self.client.V1Volume(
                    name='artifacts-volume',
                    persistent_volume_claim=self.client.V1PersistentVolumeClaimVolumeSource(
                        claim_name=self.settings.artifacts_pvc_name,
                    ),
                ),
                *(
                    [
                        self.client.V1Volume(
                            name='evaluation-contract',
                            **(
                                {
                                    'empty_dir': self.client.V1EmptyDirVolumeSource()
                                }
                                if 'container_image_digest' in evaluation_contract
                                else {
                                    'persistent_volume_claim': (
                                        self.client.V1PersistentVolumeClaimVolumeSource(
                                            claim_name=self.settings.artifacts_pvc_name,
                                        )
                                    )
                                }
                            ),
                        )
                    ]
                    if evaluation_contract
                    else []
                ),
            ],
        )

        if research_workspace:
            artifacts_root = Path(self.settings.artifacts_mount_path)
            run_artifact_dir = artifacts_root / manifest.run_id
            if run_artifact_dir.is_symlink():
                raise ValueError('research workspace artifact directory cannot be a symlink')
            run_artifact_dir.mkdir(parents=True, exist_ok=True)
            container.volume_mounts = [
                self.client.V1VolumeMount(**mount_spec)
                for mount_spec in _research_workspace_volume_mount_specs(
                    manifest,
                    self.settings,
                )
            ]
        else:
            container.volume_mounts = [
                self.client.V1VolumeMount(
                    name='dataset-volume',
                    mount_path=self.settings.dataset_mount_path,
                    read_only=True,
                ),
                self.client.V1VolumeMount(
                    name='artifacts-volume',
                    mount_path=self.settings.artifacts_mount_path,
                ),
            ]
        if evaluation_contract:
            container.volume_mounts.append(
                self.client.V1VolumeMount(
                    name='evaluation-contract',
                    mount_path='/evaluation-contract',
                    read_only=True,
                    **(
                        {'sub_path': evaluation_contract['bundle_path']}
                        if 'bundle_path' in evaluation_contract
                        else {}
                    ),
                )
            )

        job = self.client.V1Job(
            metadata=self.client.V1ObjectMeta(
                name=job_name,
                labels=labels,
                annotations=(
                    {
                        'glasslab.io/evaluation-contract-digest': (
                            evaluation_contract['digest']
                        )
                    }
                    if evaluation_contract
                    else None
                ),
            ),
            spec=self.client.V1JobSpec(
                backoff_limit=self.settings.runner_backoff_limit,
                active_deadline_seconds=_active_deadline_seconds(manifest),
                ttl_seconds_after_finished=self.settings.runner_job_ttl_seconds,
                template=self.client.V1PodTemplateSpec(
                    metadata=self.client.V1ObjectMeta(labels=labels),
                    spec=pod_spec,
                ),
            ),
        )

        self.batch_api.create_namespaced_job(namespace=self.settings.runner_namespace, body=job)
        return JobSubmissionReceipt(
            job_name=job_name,
            namespace=self.settings.runner_namespace,
            accepted_at=datetime.now(timezone.utc),
            status='submitted',
            detail='Run submitted to Kubernetes Job API.',
        )

    def get_live_status(self, record: RunRecord) -> RunStatus | None:
        try:
            job = self.batch_api.read_namespaced_job(
                name=record.job_submission.job_name,
                namespace=record.job_submission.namespace,
            )
        except self.api_exception as exc:
            raise LiveStatusUnavailableError(
                'Kubernetes API error during live status lookup'
            ) from exc
        except _KUBE_TRANSPORT_EXCEPTIONS as exc:
            raise LiveStatusUnavailableError(
                'Kubernetes transport failure during live status lookup'
            ) from exc

        now = datetime.now(timezone.utc)
        status = job.status
        detail = None
        if status.conditions:
            detail = '; '.join(filter(None, [condition.message for condition in status.conditions])) or None
        if status.succeeded:
            return RunStatus(run_id=record.run_id, status='succeeded', updated_at=now, detail=detail or 'Kubernetes Job completed successfully.')
        if status.failed:
            return RunStatus(run_id=record.run_id, status='failed', updated_at=now, detail=detail or 'Kubernetes Job reported failure.')
        if status.active:
            return RunStatus(run_id=record.run_id, status='running', updated_at=now, detail='Kubernetes Job is active.')
        return RunStatus(run_id=record.run_id, status='queued', updated_at=now, detail='Kubernetes Job is queued.')

    def cancel_run(self, record: RunRecord) -> None:
        try:
            self.batch_api.delete_namespaced_job(
                name=record.job_submission.job_name,
                namespace=record.job_submission.namespace,
                propagation_policy='Foreground',
            )
        except self.api_exception as exc:
            if getattr(exc, 'status', None) == 404:
                return
            raise LiveStatusUnavailableError(
                'Kubernetes API error during job cancellation'
            ) from exc
        except _KUBE_TRANSPORT_EXCEPTIONS as exc:
            raise LiveStatusUnavailableError(
                'Kubernetes transport failure during job cancellation'
            ) from exc

    def get_live_logs(self, record: RunRecord) -> list[LogEntry]:
        try:
            pods = self.core_api.list_namespaced_pod(
                namespace=record.job_submission.namespace,
                label_selector=f'job-name={record.job_submission.job_name}',
            )
        except self.api_exception:
            return []

        entries: list[LogEntry] = []
        for pod in pods.items:
            try:
                raw_log = self.core_api.read_namespaced_pod_log(
                    name=pod.metadata.name,
                    namespace=record.job_submission.namespace,
                )
            except self.api_exception:
                continue
            for line in raw_log.splitlines():
                if not line.strip():
                    continue
                entries.append(
                    LogEntry(
                        timestamp=datetime.now(timezone.utc),
                        level='INFO',
                        message=line,
                        payload={'pod_name': pod.metadata.name},
                    )
                )
        return entries


def create_job_submitter(settings: Settings) -> JobSubmitter:
    if settings.job_submission_mode == 'kubernetes':
        return KubernetesJobSubmitter(settings)
    return NullJobSubmitter(namespace=settings.runner_namespace)
