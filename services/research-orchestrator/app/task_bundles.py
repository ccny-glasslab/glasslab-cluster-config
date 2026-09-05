"""Task ZIP compilation and immutable asset ingestion.

Imports a ZIP containing one problem.md into a compiled TaskBundleRecord driven
by a validated TaskSpecProposal. The archive is normalized (stripped to exactly
the problem and optional evaluator rubric), assets are ingested immutably from
public HTTPS or an approved ingested-dataset registry, and every persisted file
is chmod'ed read-only so nothing written here can be mutated later. Preflight
re-verifies digests at decision time and fails closed.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import socket
from typing import Any
from urllib.parse import urljoin, urlparse
from uuid import uuid4
import zipfile

import httpx
from pydantic import BaseModel, ConfigDict, Field

from .schemas import TaskAssetProposal, TaskSpecProposal
from .spec_feedback import format_spec_feedback


class TaskBundleError(ValueError):
    pass


class DatasetAsset(BaseModel):
    model_config = ConfigDict(extra='forbid')

    name: str
    uri: str
    sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    role: str
    contains_labels: bool = False


class TaskBundleRecord(BaseModel):
    """Compiled task. The execution fields are policy output, not model output."""

    model_config = ConfigDict(extra='forbid')

    schema_version: str = 'glasslab-task-bundle-v1'
    compilation_source: str = 'legacy'
    task_id: str
    display_name: str
    digest: str = Field(pattern=r'^[a-f0-9]{64}$')
    archive_uri: str
    archive_path: str
    problem_path: str
    evaluator_prompt_path: str
    workload_id: str
    experiment_type: str
    runner_image: str
    command: list[str]
    source_subdirectory: str
    default_contract_id: str
    default_contract_version: str
    resources: dict[str, Any]
    required_artifacts: list[str]
    datasets: list[DatasetAsset]
    task_spec: dict[str, Any] | None = None
    missing_inputs: list[str] = Field(default_factory=list)


class TaskPreflight(BaseModel):
    model_config = ConfigDict(extra='forbid')

    task_id: str
    digest: str
    compiled: bool
    assets_ready: bool
    runtime_ready: bool
    evaluator_ready: bool
    missing_inputs: list[str]
    blocking_issues: list[str]
    ready: bool
    feedback: str = ''


@dataclass(frozen=True)
class RuntimeProfile:
    workload_id: str
    runner_image: str
    resources: dict[str, Any]


RUNTIME_PROFILES = {
    'cpu-ml-standard-v1': RuntimeProfile(
        workload_id='workspace-cpu-ml-v1',
        runner_image=(
            'ghcr.io/ccny-glasslab/'
            'glasslab-research-workspace-runner@sha256:'
            'dae5bc4967f5ac54edb6c6d63d8d3db9e4652cc46e035118b0c456eb70121061'
        ),
        resources={
            'cpu': 4,
            'memory_gib': 8,
            'gpus': 0,
            'wallclock_minutes': 60,
        },
    ),
    'gpu-ml-standard-v1': RuntimeProfile(
        workload_id='workspace-gpu-ml-v1',
        runner_image=(
            'ghcr.io/ccny-glasslab/'
            'glasslab-research-workspace-runner@sha256:'
            '9e7c18d186108847a485ab955194ced2dceee3ed8c8a624a2b0f3ee0e7628b60'
        ),
        resources={
            'cpu': 8,
            'memory_gib': 32,
            'gpus': 1,
            'wallclock_minutes': 240,
        },
    ),
}

# The workflow registry owns these fixed images. Persisted task metadata keeps
# the scientific bundle immutable, while execution binds to current deployment
# policy when the task is loaded.
FIXED_WORKLOAD_RUNNER_IMAGES = {
    'benchmark-workspace-cpu-v1': (
        'ghcr.io/ccny-glasslab/'
        'glasslab-research-workspace-runner@sha256:'
        'dae5bc4967f5ac54edb6c6d63d8d3db9e4652cc46e035118b0c456eb70121061'
    ),
    'workspace-cpu-ml-v1': RUNTIME_PROFILES['cpu-ml-standard-v1'].runner_image,
    'workspace-gpu-ml-v1': RUNTIME_PROFILES['gpu-ml-standard-v1'].runner_image,
}

BASE_REQUIRED_ARTIFACTS = (
    'run_manifest.json',
    'config.json',
    'metrics.json',
    'evaluation.json',
    'artifacts_index.json',
    'report.md',
    'status.json',
    'logs/',
    'source.zip',
)


@dataclass(frozen=True)
class StagedTaskBundle:
    filename: str
    digest: str
    root: Path
    archive_path: Path
    problem_path: Path
    evaluator_prompt_path: Path


class TaskAssetFetcher:
    """Fetch immutable assets while rejecting non-public HTTPS targets."""

    def __init__(
        self,
        *,
        root: str,
        shared_mount_root: str,
        maximum_bytes: int,
        timeout_seconds: float = 300.0,
        connect_timeout_seconds: float = 15.0,
        max_retries: int = 2,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.root = Path(root).resolve()
        self.shared_mount_root = Path(shared_mount_root).resolve()
        self.maximum_bytes = maximum_bytes
        # Large public datasets (e.g. cifar100, ~160 MB at ~100 KB/s from the
        # canonical host) can take many minutes; the per-read timeout must be
        # generous and the fetch must retry transient stalls.
        self.timeout_seconds = timeout_seconds
        self.connect_timeout_seconds = connect_timeout_seconds
        self.max_retries = max_retries
        self.transport = transport

    @staticmethod
    def _validate_url(url: str) -> None:
        try:
            parsed = urlparse(url)
            port = parsed.port
        except ValueError as exc:
            raise TaskBundleError('task asset URL is malformed') from exc
        # Assets are ingested by the orchestrator's own identity, so only
        # public HTTPS targets with no embedded credentials and no custom port
        # are acceptable; anything else is refused before a single byte is
        # fetched.
        if (
            parsed.scheme != 'https'
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or port not in {None, 443}
        ):
            raise TaskBundleError('task assets require a public HTTPS URL')
        # Resolve the host at ingestion time and reject any non-globally
        # routable address so a task cannot point the orchestrator at
        # cluster-internal or link-local endpoints.
        try:
            addresses = socket.getaddrinfo(
                parsed.hostname,
                443,
                type=socket.SOCK_STREAM,
            )
        except OSError as exc:
            raise TaskBundleError(
                f'cannot resolve task asset host: {parsed.hostname}'
            ) from exc
        for address in addresses:
            ip = ipaddress.ip_address(address[4][0])
            if not ip.is_global:
                raise TaskBundleError(
                    f'task asset host resolves to a non-public address: {ip}'
                )

    def fetch(
        self,
        *,
        task_digest: str,
        proposal: TaskAssetProposal,
    ) -> DatasetAsset:
        if not proposal.source_url:
            raise TaskBundleError(f'asset has no source URL: {proposal.name}')
        destination = self.root / task_digest / proposal.name
        metadata = destination / 'asset.json'
        if metadata.is_file():
            # Already ingested for this exact task digest: reuse the immutable
            # record rather than re-downloading (the digest key guarantees
            # content equality).
            return DatasetAsset.model_validate_json(metadata.read_text())
        staging = self.root / '.staging' / uuid4().hex
        staging.mkdir(parents=True, exist_ok=False)
        asset_path = staging / 'asset'
        current_url = proposal.source_url
        last_error: str | None = None
        for attempt in range(self.max_retries + 1):
            try:
                with httpx.Client(
                    follow_redirects=False,
                    timeout=httpx.Timeout(
                        self.timeout_seconds,
                        connect=self.connect_timeout_seconds,
                    ),
                    transport=self.transport,
                ) as client:
                    for _ in range(6):
                        # Follow redirects manually so every hop is re-validated
                        # against the same public-HTTPS + global-address rules;
                        # automatic redirects would bypass the allowlist.
                        self._validate_url(current_url)
                        with client.stream('GET', current_url) as response:
                            if response.status_code in {301, 302, 303, 307, 308}:
                                location = response.headers.get('location')
                                if not location:
                                    raise TaskBundleError(
                                        'task asset redirect has no location'
                                    )
                                current_url = urljoin(current_url, location)
                                continue
                            response.raise_for_status()
                            size = 0
                            digest = sha256()
                            with asset_path.open('wb') as output:
                                for chunk in response.iter_bytes():
                                    size += len(chunk)
                                    if size > self.maximum_bytes:
                                        raise TaskBundleError(
                                            f'task asset exceeds {self.maximum_bytes} bytes'
                                        )
                                    digest.update(chunk)
                                    output.write(chunk)
                            if size == 0:
                                raise TaskBundleError(
                                    f'task asset is empty: {proposal.name}'
                                )
                            break
                    else:
                        raise TaskBundleError('task asset redirected too many times')
            except httpx.TransportError as exc:
                if attempt >= self.max_retries:
                    raise TaskBundleError(
                        f'task asset download failed for {proposal.name}: {exc}. '
                        'Retry the import, or upload the dataset via '
                        '/dataset-upload and reference its '
                        'glasslab-dataset://<sha256> URI in the task bundle.'
                    ) from exc
                last_error = str(exc)
                shutil.rmtree(staging, ignore_errors=True)
                staging = self.root / '.staging' / uuid4().hex
                staging.mkdir(parents=True, exist_ok=False)
                asset_path = staging / 'asset'
                current_url = proposal.source_url
                continue
            except httpx.HTTPError as exc:
                raise TaskBundleError(
                    f'task asset download failed for {proposal.name}: {exc}'
                ) from exc
            break
        actual_digest = digest.hexdigest()
        if (
            proposal.expected_sha256
            and actual_digest != proposal.expected_sha256
        ):
            shutil.rmtree(staging, ignore_errors=True)
            raise TaskBundleError(
                f'task asset checksum mismatch for {proposal.name}'
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Staged-then-renamed so a partially downloaded asset is never visible
        # at the final path; a concurrent identical fetch that won the race
        # simply discards our duplicate staging directory.
        if destination.exists():
            shutil.rmtree(staging)
        else:
            os.replace(staging, destination)
        uri = (
            's3://artifacts/'
            + (destination / 'asset')
            .relative_to(self.shared_mount_root)
            .as_posix()
        )
        record = DatasetAsset(
            name=proposal.name,
            uri=uri,
            sha256=actual_digest,
            role=proposal.role,
            contains_labels=proposal.contains_labels,
        )
        metadata.write_text(record.model_dump_json(indent=2) + '\n')
        # Immutability: the asset blob, its sidecar record, and the directory
        # are read-only after ingestion, so nothing downstream can modify them.
        asset_path = destination / 'asset'
        asset_path.chmod(0o444)
        metadata.chmod(0o444)
        destination.chmod(0o555)
        return record


class TaskBundleManager:
    MAX_ARCHIVE_BYTES = 16 * 1024 * 1024
    MAX_FILES = 256
    MAX_EXPANDED_BYTES = 64 * 1024 * 1024

    def __init__(
        self,
        *,
        root: str,
        shared_mount_root: str,
        dataset_catalog_path: str,
        task_asset_root: str | None = None,
        maximum_asset_bytes: int = 2 * 1024 * 1024 * 1024,
        asset_download_timeout_seconds: float = 300.0,
        asset_download_connect_timeout_seconds: float = 15.0,
        asset_download_max_retries: int = 2,
        ingested_datasets=None,
    ) -> None:
        self.root = Path(root).resolve()
        self.shared_mount_root = Path(shared_mount_root).resolve()
        # Retained so old deployments and records remain configuration-compatible.
        self.dataset_catalog_path = Path(dataset_catalog_path).resolve()
        self.assets = TaskAssetFetcher(
            root=task_asset_root or str(self.root.parent / 'task-assets'),
            shared_mount_root=shared_mount_root,
            maximum_bytes=maximum_asset_bytes,
            timeout_seconds=asset_download_timeout_seconds,
            connect_timeout_seconds=asset_download_connect_timeout_seconds,
            max_retries=asset_download_max_retries,
        )
        self.ingested_datasets = ingested_datasets

    def stage_archive(self, *, filename: str, content: bytes) -> StagedTaskBundle:
        if not content or len(content) > self.MAX_ARCHIVE_BYTES:
            raise TaskBundleError('task archive has an invalid size')
        digest = sha256(content).hexdigest()
        staging = self.root / '.staging' / uuid4().hex
        staging.mkdir(parents=True, exist_ok=False)
        archive_path = staging / 'task.zip'
        archive_path.write_bytes(content)
        try:
            with zipfile.ZipFile(archive_path) as handle:
                files = [item for item in handle.infolist() if not item.is_dir()]
                if not files or len(files) > self.MAX_FILES:
                    raise TaskBundleError('task archive file count is invalid')
                expanded = 0
                for member in files:
                    path = PurePosixPath(member.filename)
                    mode = member.external_attr >> 16
                    # Zip-slip and symlink members are rejected outright: an
                    # absolute path, a `..` segment, or a symlink could escape
                    # the normalized tree during extraction.
                    if (
                        path.is_absolute()
                        or '..' in path.parts
                        or mode & 0o170000 == 0o120000
                    ):
                        raise TaskBundleError(
                            f'unsafe task archive member: {member.filename}'
                        )
                    # Decompression-bomb guard based on declared uncompressed
                    # sizes; the guard is cheap because only problem.md and the
                    # evaluator prompt are ever actually extracted below.
                    expanded += member.file_size
                if expanded > self.MAX_EXPANDED_BYTES:
                    raise TaskBundleError('task archive expands too large')
                problem_members = [
                    item
                    for item in files
                    if PurePosixPath(item.filename).name == 'problem.md'
                ]
                evaluator_members = [
                    item
                    for item in files
                    if PurePosixPath(item.filename).name == 'eval_agent_prompt.md'
                ]
                if len(problem_members) != 1 or len(evaluator_members) > 1:
                    raise TaskBundleError(
                        'task archive requires one problem.md and at most one '
                        'eval_agent_prompt.md'
                    )
                # Normalization: everything except the problem and the optional
                # rubric is discarded, so a compiled task's inputs are exactly
                # known regardless of what junk rode along in the ZIP.
                normalized = staging / 'normalized'
                normalized.mkdir()
                problem = normalized / 'problem.md'
                evaluator = normalized / 'eval_agent_prompt.md'
                problem.write_bytes(handle.read(problem_members[0]))
                evaluator.write_bytes(
                    handle.read(evaluator_members[0])
                    if evaluator_members
                    else b'# Evaluation notes\n\nNo supplied evaluator rubric.\n'
                )
        except zipfile.BadZipFile as exc:
            shutil.rmtree(staging, ignore_errors=True)
            raise TaskBundleError('task must be a ZIP archive') from exc
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        try:
            problem.read_text(encoding='utf-8')
            evaluator.read_text(encoding='utf-8')
        except UnicodeDecodeError as exc:
            shutil.rmtree(staging, ignore_errors=True)
            raise TaskBundleError(
                'problem and evaluator prompt must be UTF-8 text'
            ) from exc
        return StagedTaskBundle(
            filename=Path(filename).name,
            digest=digest,
            root=staging,
            archive_path=archive_path,
            problem_path=problem,
            evaluator_prompt_path=evaluator,
        )

    @staticmethod
    def _task_id(display_name: str, digest: str) -> str:
        # Deliberately content-addressed: the digest (not the display name)
        # determines the id, so re-importing the same archive never forks a new
        # task and a renamed archive stays the same task.
        del display_name
        return f'task-{digest[:16]}'

    def find_by_digest(self, digest: str) -> TaskBundleRecord | None:
        for record in self.list():
            if record.digest == digest:
                return record
        return None

    def compile(
        self,
        staged: StagedTaskBundle,
        proposal: TaskSpecProposal,
    ) -> TaskBundleRecord:
        profile = RUNTIME_PROFILES[proposal.runtime_profile]
        task_id = self._task_id(proposal.display_name, staged.digest)
        destination = self.root / task_id / staged.digest
        metadata_path = destination / 'task.json'
        if metadata_path.is_file():
            # Content-addressed idempotency: the identical archive was already
            # compiled, so the persisted immutable record is reused and the
            # fresh staging copy is dropped.
            shutil.rmtree(staged.root, ignore_errors=True)
            return TaskBundleRecord.model_validate_json(metadata_path.read_text())

        datasets: list[DatasetAsset] = []
        missing = list(proposal.missing_inputs)
        for asset in proposal.assets:
            if asset.approved_uri:
                # Approved URIs resolve through the ingested-dataset registry,
                # never through a network fetch; the registry verifies the
                # expected sha256 at resolution time.
                if self.ingested_datasets is None:
                    missing.append(
                        f'ingested dataset registry is unavailable: {asset.name}'
                    )
                    continue
                try:
                    datasets.append(
                        self.ingested_datasets.resolve(
                            asset.approved_uri,
                            name=asset.name,
                            role=asset.role,
                            contains_labels=asset.contains_labels,
                            expected_sha256=asset.expected_sha256,
                        )
                    )
                except TaskBundleError as exc:
                    missing.append(str(exc))
                continue
            if not asset.source_url:
                missing.append(
                    f'asset `{asset.name}` has no approved URI or source URL'
                )
                continue
            try:
                datasets.append(
                    self.assets.fetch(
                        task_digest=staged.digest,
                        proposal=asset,
                    )
                )
            except TaskBundleError as exc:
                missing.append(str(exc))
        # Compilation still succeeds with missing inputs so the record and its
        # diagnostics are durable; TaskPreflight later refuses to start the
        # task until every missing input is resolved.
        required_artifacts = list(
            dict.fromkeys(
                [
                    *BASE_REQUIRED_ARTIFACTS,
                    *proposal.required_artifacts,
                ]
            )
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            shutil.rmtree(staged.root)
        else:
            os.replace(staged.root, destination)
        record = TaskBundleRecord(
            schema_version='glasslab-task-bundle-v2',
            compilation_source='honeydew-task-spec',
            task_id=task_id,
            display_name=proposal.display_name,
            digest=staged.digest,
            archive_uri=(
                's3://artifacts/'
                + (destination / 'task.zip')
                .relative_to(self.shared_mount_root)
                .as_posix()
            ),
            archive_path=str(destination / 'task.zip'),
            problem_path=str(destination / 'normalized' / 'problem.md'),
            evaluator_prompt_path=str(
                destination / 'normalized' / 'eval_agent_prompt.md'
            ),
            workload_id=profile.workload_id,
            experiment_type='research-workspace-job',
            runner_image=profile.runner_image,
            command=['python3', 'run.py'],
            source_subdirectory=f'research-workspace/{task_id}',
            default_contract_id='generic-task-integrity-v1',
            default_contract_version='1.0.0',
            resources=profile.resources,
            required_artifacts=required_artifacts,
            datasets=datasets,
            task_spec=proposal.model_dump(mode='json'),
            missing_inputs=list(dict.fromkeys(missing)),
        )
        metadata_path.write_text(record.model_dump_json(indent=2) + '\n')
        # Persisted bundle is immutable: directories and files are read-only so
        # neither the agents nor later imports can mutate a compiled task.
        for path in destination.rglob('*'):
            path.chmod(0o555 if path.is_dir() else 0o444)
        return record

    def discard_staged(self, staged: StagedTaskBundle) -> None:
        shutil.rmtree(staged.root, ignore_errors=True)

    def preflight(
        self,
        record: TaskBundleRecord,
        *,
        permitted_images: set[str],
        evaluator_ready: bool,
    ) -> TaskPreflight:
        issues = list(record.missing_inputs)
        asset_issues: list[str] = []
        if record.runner_image not in permitted_images:
            issues.append('compiled runtime image is not permitted')
        if not evaluator_ready:
            issues.append('compiled evaluation contract is not installed')
        archive = Path(record.archive_path).resolve()
        # Re-verify the archive by content at decision time (not just at
        # compile time) so a tampered or evicted file is caught before any
        # cluster work depends on it.
        archive_ready = not (
            not archive.is_relative_to(self.root)
            or not archive.is_file()
            or self._file_sha256(archive) != record.digest
        )
        if not archive_ready:
            issues.append('task archive is unavailable or failed checksum verification')
        for asset in record.datasets:
            # Assets may only be referenced through the shared mounts; anything
            # else (e.g. a stale external URI) is a blocking issue.
            if not asset.uri.startswith(('s3://artifacts/', 's3://datasets/')):
                asset_issues.append(f'asset URI is not approved: {asset.name}')
            if asset.uri.startswith('s3://artifacts/'):
                relative = asset.uri.removeprefix('s3://artifacts/')
                path = (self.shared_mount_root / relative).resolve()
                if (
                    not path.is_relative_to(self.shared_mount_root)
                    or not path.is_file()
                    or self._file_sha256(path) != asset.sha256
                ):
                    asset_issues.append(
                        f'asset is unavailable or failed checksum verification: '
                        f'{asset.name}'
                    )
        issues.extend(asset_issues)
        ready = not issues and evaluator_ready
        return TaskPreflight(
            task_id=record.task_id,
            digest=record.digest,
            compiled=archive_ready,
            assets_ready=not record.missing_inputs and not asset_issues,
            runtime_ready=record.runner_image in permitted_images,
            evaluator_ready=evaluator_ready,
            missing_inputs=record.missing_inputs,
            blocking_issues=issues,
            ready=ready,
            feedback=format_spec_feedback(issues) if not ready else '',
        )

    @staticmethod
    def _file_sha256(path: Path) -> str:
        digest = sha256()
        with path.open('rb') as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b''):
                digest.update(chunk)
        return digest.hexdigest()

    def get(self, task_id: str, digest: str | None = None) -> TaskBundleRecord:
        task_root = (self.root / task_id).resolve()
        if not task_root.is_relative_to(self.root) or not task_root.is_dir():
            raise TaskBundleError(f'task bundle is not imported: {task_id}')
        candidates = sorted(
            (path for path in task_root.iterdir() if path.is_dir()),
            key=lambda path: path.stat().st_mtime_ns,
            reverse=True,
        )
        if digest:
            candidates = [path for path in candidates if path.name == digest]
        if not candidates:
            raise TaskBundleError(f'task bundle digest is not imported: {task_id}')
        record = TaskBundleRecord.model_validate_json(
            (candidates[0] / 'task.json').read_text()
        )
        # Rebind the runner image to the current deployment's fixed image for
        # this workload id: the persisted bundle stays immutable and scientific
        # (the digest), while execution always tracks current deployment policy.
        runner_image = FIXED_WORKLOAD_RUNNER_IMAGES.get(record.workload_id)
        if runner_image and runner_image != record.runner_image:
            record = record.model_copy(update={'runner_image': runner_image})
        return record

    def list(self) -> list[TaskBundleRecord]:
        records: list[TaskBundleRecord] = []
        if not self.root.is_dir():
            return records
        for task_root in sorted(self.root.iterdir()):
            if not task_root.is_dir() or task_root.name.startswith('.'):
                continue
            try:
                records.append(self.get(task_root.name))
            except (OSError, ValueError):
                continue
        return records
