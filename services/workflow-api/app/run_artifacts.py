"""Artifact discovery and status resolution for completed runs.

Three layers of status resolution (on-disk, live Kubernetes, stored record)
let callers observe terminal state regardless of whether the run is still in
flight or has already persisted its status.json. Artifacts are discovered from
the shared PVC directory rather than trusting a pre-built index alone.
"""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
import re
from pathlib import Path

from services.common.schemas import ArtifactIndexEntry, ArtifactsIndex, RunStatus

from .config import Settings
from .job_submission import (
    JobSubmitter,
    LiveStatusUnavailableError,
    validate_run_artifact_subpath,
)
from .schemas import LogEntry, RunRecord

MEDIA_TYPES = {
    '.json': 'application/json',
    '.md': 'text/markdown',
    '.csv': 'text/csv',
    '.txt': 'text/plain',
    '.log': 'text/plain',
}

LOG_LINE_RE = re.compile(r'^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) (?P<level>[A-Z]+) (?P<logger>[^ ]+) (?P<message>.*)$')


def file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def artifact_run_dir(settings: Settings, run_id: str) -> Path:
    return Path(settings.artifacts_mount_path) / validate_run_artifact_subpath(run_id)


def load_status_from_disk(settings: Settings, run_id: str) -> RunStatus | None:
    path = artifact_run_dir(settings, run_id) / 'status.json'
    if not path.exists():
        return None
    payload = json.loads(path.read_text())
    # Identity binding: a status.json that omits run_id or names a different
    # run was not written by this run and must never resolve as its status.
    if not isinstance(payload, dict) or payload.get('run_id') != run_id:
        return None
    payload.setdefault('updated_at', datetime.now(timezone.utc).isoformat())
    try:
        return RunStatus.model_validate(payload)
    except Exception:
        return None


def build_artifacts_from_directory(settings: Settings, run_id: str) -> ArtifactsIndex | None:
    root = artifact_run_dir(settings, run_id)
    if not root.exists():
        return None
    artifacts: list[ArtifactIndexEntry] = []
    for path in sorted(root.rglob('*')):
        if path.is_symlink() or not _path_is_within(path, root):
            continue
        relative = path.relative_to(root).as_posix()
        if path.is_dir():
            artifacts.append(
                ArtifactIndexEntry(
                    name=f'{relative}/',
                    path=f'artifacts/{run_id}/{relative}/',
                    media_type='inode/directory',
                    required=relative == 'logs',
                    description='Discovered from shared artifacts volume',
                )
            )
            continue
        if not path.is_file():
            continue
        artifacts.append(
            ArtifactIndexEntry(
                name=relative,
                path=f'artifacts/{run_id}/{relative}',
                media_type=MEDIA_TYPES.get(path.suffix.lower(), 'application/octet-stream'),
                required=relative in {'run_manifest.json', 'config.json', 'metrics.json', 'artifacts_index.json', 'report.md', 'status.json', 'logs/runner.log'},
                size_bytes=path.stat().st_size,
                sha256=file_sha256(path),
                description='Discovered from shared artifacts volume',
            )
        )
    return ArtifactsIndex(run_id=run_id, artifacts=artifacts)


def load_artifacts_from_disk(settings: Settings, run_id: str) -> ArtifactsIndex | None:
    # Prefer the runner-written index; fall back to directory scanning.
    # The runner's index is authoritative because it includes digest
    # verification; directory scanning is a best-effort fallback.
    index_path = artifact_run_dir(settings, run_id) / 'artifacts_index.json'
    if index_path.exists():
        payload = json.loads(index_path.read_text())
        # The runner-written index is only authoritative for the run it names;
        # a foreign or unbound index must never be served for this run.
        if not isinstance(payload, dict) or payload.get('run_id') != run_id:
            return None
        return ArtifactsIndex.model_validate(payload)
    return build_artifacts_from_directory(settings, run_id)


def parse_log_line(line: str) -> LogEntry:
    match = LOG_LINE_RE.match(line)
    if not match:
        return LogEntry(timestamp=datetime.now(timezone.utc), level='INFO', message=line)
    timestamp = datetime.strptime(match.group('ts'), '%Y-%m-%d %H:%M:%S,%f').replace(tzinfo=timezone.utc)
    return LogEntry(timestamp=timestamp, level=match.group('level'), message=match.group('message'), payload={'logger': match.group('logger')})


def load_logs_from_disk(settings: Settings, run_id: str) -> list[LogEntry]:
    log_path = artifact_run_dir(settings, run_id) / 'logs' / 'runner.log'
    if not log_path.exists():
        return []
    return [parse_log_line(line) for line in log_path.read_text().splitlines() if line.strip()]


def load_terminal_bundle(
    settings: Settings,
    record: RunRecord,
) -> tuple[RunStatus, dict[str, object], dict[str, str], ArtifactsIndex]:
    run_id = record.run_id
    root = artifact_run_dir(settings, run_id)
    disk_status = load_status_from_disk(settings, run_id)
    if disk_status is None:
        raise ValueError('terminal bundle is missing status.json')
    if disk_status.run_id != run_id:
        raise ValueError('terminal bundle status.json run_id does not match the run')
    if disk_status.status not in {'succeeded', 'failed', 'rejected'}:
        raise ValueError('terminal bundle status is not terminal')

    expected = record.manifest.expected_artifacts
    required = [str(name) for name in expected.get('required', [])]
    missing: list[str] = []
    for name in required:
        path = root / name.rstrip('/')
        if path.is_symlink():
            missing.append(name)
            continue
        if name.endswith('/'):
            exists = path.is_dir()
        else:
            exists = path.is_file()
        if not exists or not _path_is_within(path, root):
            missing.append(name)
    if disk_status.status == 'succeeded' and missing:
        raise ValueError(
            'successful terminal bundle is missing required artifacts: '
            + ', '.join(sorted(missing))
        )

    metrics: dict[str, object] = {}
    metrics_path = root / 'metrics.json'
    if metrics_path.is_file() and _path_is_within(metrics_path, root):
        payload = json.loads(metrics_path.read_text())
        if not isinstance(payload, dict):
            raise ValueError('metrics.json must contain a JSON object')
        metrics = payload

    artifacts = build_artifacts_from_directory(settings, run_id)
    if artifacts is None:
        raise ValueError('terminal bundle artifact directory is missing')
    refs = {
        entry.name: entry.path
        for entry in artifacts.artifacts
        if entry.name and _path_is_within(root / entry.name.rstrip('/'), root)
    }
    return disk_status, metrics, refs, artifacts


def _path_is_within(path: Path, root: Path) -> bool:
    # Resolves both paths before checking containment, so symlinks are
    # resolved to their real targets. A path that is a symlink pointing
    # inside the root is accepted; a symlink pointing outside is rejected.
    resolved_root = root.resolve()
    resolved_path = path.resolve()
    return resolved_path == resolved_root or resolved_path.is_relative_to(resolved_root)


def resolve_run_status(record: RunRecord, settings: Settings, submitter: JobSubmitter) -> RunStatus:
    # Prefer on-disk terminal status first (authoritative once written), then
    # live Kubernetes status (for in-flight jobs), falling back to the stored
    # record (accepted but not yet scheduled). A Kubernetes API/transport
    # outage degrades to the stored record with an explicit note rather than
    # surfacing an HTTP 500.
    disk_status = load_status_from_disk(settings, record.run_id)
    if disk_status is not None:
        return disk_status
    try:
        live_status = submitter.get_live_status(record)
    except LiveStatusUnavailableError:
        durable = record.status
        durable_detail = f'{durable.detail}; ' if durable.detail else ''
        return RunStatus(
            run_id=record.run_id,
            status=durable.status,
            updated_at=durable.updated_at,
            detail=(
                f'{durable_detail}Live Kubernetes status unavailable; '
                'showing durable stored status.'
            ),
        )
    if live_status is not None:
        return live_status
    return record.status
