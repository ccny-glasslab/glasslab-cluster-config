"""Cluster adapters behind one interface.

The orchestrator never talks to Kubernetes directly: the fake and the real
adapter implement the same submit/inspect/cancel contract so the engine is
interchangeable between test and production. The fake therefore mirrors the
real adapter's observable behavior (status vocabulary, idempotent submission,
digest-carrying artifacts), not just its signatures.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from hashlib import sha256
import json
from typing import Any

import httpx

from .schemas import ExpandedJobSpec, JobStatus


class ClusterExecutorError(RuntimeError):
    pass


@dataclass(frozen=True)
class ClusterSubmission:
    external_run_id: str
    job_name: str | None
    kubernetes_uid: str | None
    status: JobStatus


@dataclass(frozen=True)
class ClusterArtifact:
    type: str
    uri: str
    sha256: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ClusterJobSnapshot:
    status: JobStatus
    exit_information: dict[str, Any] = field(default_factory=dict)
    artifacts: list[ClusterArtifact] = field(default_factory=list)


class ClusterExecutor(ABC):
    @abstractmethod
    def submit(self, spec: ExpandedJobSpec) -> ClusterSubmission:
        raise NotImplementedError

    @abstractmethod
    def inspect(self, external_run_id: str) -> ClusterJobSnapshot:
        raise NotImplementedError

    @abstractmethod
    def cancel(self, external_run_id: str) -> None:
        raise NotImplementedError


class FakeClusterExecutor(ClusterExecutor):
    def __init__(self) -> None:
        self.submissions: dict[str, ClusterSubmission] = {}
        self.snapshots: dict[str, ClusterJobSnapshot] = {}

    def submit(self, spec: ExpandedJobSpec) -> ClusterSubmission:
        # Submission is idempotent on the orchestrator-generated key: a replay
        # returns the original submission, matching the real adapter.
        existing = self.submissions.get(spec.idempotency_key)
        if existing is not None:
            return existing
        # External ids are derived deterministically from orchestrator ids so
        # restarts observe the same runs, as the real adapter's run ids do.
        submission = ClusterSubmission(
            external_run_id=f'fake-{spec.orchestrator_job_id}',
            job_name=f'fake-{spec.variant_name}-{spec.seed}',
            kubernetes_uid=f'uid-{spec.orchestrator_job_id}',
            status=JobStatus.RUNNING,
        )
        self.submissions[spec.idempotency_key] = submission
        self.snapshots[submission.external_run_id] = ClusterJobSnapshot(
            status=JobStatus.RUNNING
        )
        return submission

    def inspect(self, external_run_id: str) -> ClusterJobSnapshot:
        try:
            return self.snapshots[external_run_id]
        except KeyError as exc:
            raise ClusterExecutorError(
                f'fake cluster run not found: {external_run_id}'
            ) from exc

    def complete(
        self,
        external_run_id: str,
        *,
        succeeded: bool = True,
        metrics: dict[str, Any] | None = None,
    ) -> None:
        # The fake emits the same artifact shape the real adapter reports: a
        # metrics artifact with a sha256 over canonical JSON so downstream
        # digest verification has something real to check.
        payload = json.dumps(metrics or {'score': 1.0}, sort_keys=True).encode()
        artifact = ClusterArtifact(
            type='metrics',
            uri=f'artifact://{external_run_id}/metrics.json',
            sha256=sha256(payload).hexdigest(),
            metadata={'metrics': metrics or {'score': 1.0}},
        )
        self.snapshots[external_run_id] = ClusterJobSnapshot(
            status=JobStatus.SUCCEEDED if succeeded else JobStatus.FAILED,
            exit_information={'exit_code': 0 if succeeded else 1},
            artifacts=[artifact],
        )

    def cancel(self, external_run_id: str) -> None:
        current = self.inspect(external_run_id)
        self.snapshots[external_run_id] = ClusterJobSnapshot(
            status=JobStatus.CANCELLED,
            exit_information={**current.exit_information, 'cancelled': True},
            artifacts=current.artifacts,
        )


WORKFLOW_STATUS_MAP = {
    # Collapse the workflow-api's richer status vocabulary onto JobStatus;
    # unrecognized values map to UNKNOWN rather than being guessed.
    'accepted': JobStatus.QUEUED,
    'submitted': JobStatus.QUEUED,
    'pending': JobStatus.QUEUED,
    'running': JobStatus.RUNNING,
    'succeeded': JobStatus.SUCCEEDED,
    'failed': JobStatus.FAILED,
    'rejected': JobStatus.FAILED,
    'cancelled': JobStatus.CANCELLED,
}


class WorkflowApiClusterExecutor(ClusterExecutor):
    """Bounded adapter; it never receives Kubernetes credentials."""

    def __init__(
        self,
        *,
        base_url: str,
        workload_id: str,
        experiment_type: str,
        caller_name: str = '',
        token: str = '',
        timeout_seconds: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip('/')
        self.workload_id = workload_id
        self.experiment_type = experiment_type
        self.caller_name = caller_name.strip()
        self.token = token
        self.timeout_seconds = timeout_seconds
        self._submitted: dict[str, ClusterSubmission] = {}

    def _client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self.base_url,
            timeout=self.timeout_seconds,
        )

    def _auth_headers(self) -> dict[str, str]:
        if not self.caller_name or not self.token.strip():
            raise ClusterExecutorError(
                'workflow API credentials are not configured'
            )
        return {
            'X-Glasslab-Caller': self.caller_name,
            'X-Glasslab-Workflow-Token': self.token,
        }

    def submit(self, spec: ExpandedJobSpec) -> ClusterSubmission:
        existing = self._submitted.get(spec.idempotency_key)
        if existing is not None:
            return existing
        body = {
            'objective': (
                f'Research orchestrator variant {spec.variant_name}, seed {spec.seed}'
            ),
            'experiment_type': spec.experiment_type or self.experiment_type,
            'workload_id': spec.workload_id or self.workload_id,
            'campaign_id': spec.run_id,
            'config_payload': {
                'orchestrator_job_id': spec.orchestrator_job_id,
                'variant_name': spec.variant_name,
                'seed': spec.seed,
                'base_config': spec.base_config,
                'overrides': spec.overrides,
                'evaluation_contract': {
                    'contract_id': spec.evaluation_contract_id,
                    'version': spec.evaluation_contract_version,
                    'digest': spec.evaluation_contract_digest,
                },
                # Task/source bundles mean a fixed workspace runner executes the
                # bounded workload; dataset bindings and the task spec are only
                # meaningful in that mode, so both are omitted otherwise.
                **(
                    {
                        'workspace': {
                            'task_bundle': spec.task_bundle,
                            'source_bundle': spec.source_bundle,
                            'command': spec.workspace_command,
                            'working_directory': '.',
                            'output_directory': '/outputs',
                            'network_policy': 'none',
                        },
                        'dataset_contracts': spec.dataset_contracts,
                        'task_spec': spec.task_spec,
                    }
                    if spec.task_bundle and spec.source_bundle
                    else {}
                ),
            },
            'dataset_bindings': spec.dataset_bindings,
            'resources': spec.resources.model_dump(mode='json'),
            'budget': {
                'max_wallclock_minutes': spec.resources.wallclock_minutes,
            },
            'artifact_contract': {
                # required_artifacts already unions the contract's and the
                # matrix's requirements during expansion; the cluster is told
                # exactly which evidence files must survive.
                'required': spec.required_artifacts,
                'optional': [],
            },
            'metric_contract': {
                # Carries the contract digest into the cluster so the evaluator
                # can confirm the metrics it scores came from the approved
                # contract, not a relabeled one.
                'evaluation_contract_id': spec.evaluation_contract_id,
                'evaluation_contract_version': spec.evaluation_contract_version,
                'evaluation_contract_digest': spec.evaluation_contract_digest,
            },
            'submitted_by': 'research-orchestrator',
            'run_priority': 'user',
        }
        # Runner images are fixed by the workflow registry for every workload.
        # A persisted job spec may describe provenance, but cannot select code.
        with self._client() as client:
            response = client.post(
                '/experiments/runs',
                json=body,
                headers=self._auth_headers(),
            )
            if response.is_error:
                try:
                    detail = response.json().get('detail')
                except (ValueError, AttributeError):
                    detail = response.text
                raise ClusterExecutorError(
                    'workflow-api rejected experiment submission '
                    f'({response.status_code}): {detail or response.reason_phrase}'
                )
            payload = response.json()
        submission_payload = payload.get('job_submission') or {}
        raw_status = str((payload.get('status') or {}).get('status', 'accepted'))
        submission = ClusterSubmission(
            external_run_id=str(payload['run_id']),
            job_name=submission_payload.get('job_name'),
            kubernetes_uid=submission_payload.get('job_uid'),
            status=WORKFLOW_STATUS_MAP.get(raw_status, JobStatus.UNKNOWN),
        )
        self._submitted[spec.idempotency_key] = submission
        return submission

    def inspect(self, external_run_id: str) -> ClusterJobSnapshot:
        with self._client() as client:
            response = client.get(
                f'/runs/{external_run_id}', headers=self._auth_headers()
            )
            response.raise_for_status()
            payload = response.json()
            raw_status = str((payload.get('status') or {}).get('status', 'unknown'))
            artifacts_response = client.get(
                f'/runs/{external_run_id}/artifacts', headers=self._auth_headers()
            )
        artifacts: list[ClusterArtifact] = []
        if artifacts_response.status_code == 200:
            for item in (
                (artifacts_response.json().get('artifacts') or {}).get(
                    'artifacts',
                    [],
                )
            ):
                digest = str(item.get('sha256') or '')
                if len(digest) != 64:
                    # A malformed digest means the artifact is not trustworthy
                    # evidence; skip it rather than passing it on to delivery.
                    continue
                artifacts.append(
                    ClusterArtifact(
                        type=str(item.get('name', 'artifact')),
                        uri=str(item.get('path', '')),
                        sha256=digest,
                        metadata={
                            key: value
                            for key, value in item.items()
                            if key not in {'name', 'path', 'sha256'}
                        },
                    )
                )
        return ClusterJobSnapshot(
            status=WORKFLOW_STATUS_MAP.get(raw_status, JobStatus.UNKNOWN),
            exit_information={'workflow_api_status': payload.get('status')},
            artifacts=artifacts,
        )

    def cancel(self, external_run_id: str) -> None:
        with self._client() as client:
            response = client.post(
                f'/runs/{external_run_id}/cancel',
                headers=self._auth_headers(),
            )
        if response.status_code == 404:
            # 404 means this workflow-api build has no bounded cancellation
            # surface; surface that as a hard error instead of silently
            # reporting the run as cancelled.
            raise ClusterExecutorError(
                'configured workflow-api does not expose bounded cancellation'
            )
        response.raise_for_status()
