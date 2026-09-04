"""Bundle a run's digest-verified artifacts for Discord delivery.

Reads artifacts only from the shared mount root, verifies every SHA-256 against
the authoritative record, and packages the selected set into one zip with a
manifest. An artifact that fails verification is reported as unavailable rather
than failing the whole delivery; nothing is ever read from outside the mount.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import io
import json
from pathlib import Path, PurePosixPath
from typing import Any, Iterable
import zipfile

from .report_bundle import (
    ArtifactDeliveryError,
    ReportBundle,
    build_report_bundle,
)
from .schemas import ArtifactRecord, JobRecord, JobStatus


@dataclass(frozen=True)
class ArtifactBundle:
    filename: str
    content: bytes
    artifact_count: int


class VerifiedArtifactReader:
    def __init__(self, shared_mount_root: str) -> None:
        self.root = Path(shared_mount_root).resolve()

    def resolve(self, artifact: ArtifactRecord) -> Path:
        metadata_path = artifact.metadata.get('path')
        candidates: list[Path] = []
        if isinstance(metadata_path, str) and metadata_path:
            candidates.append(Path(metadata_path))

        uri = artifact.uri
        if uri.startswith('artifact://'):
            relative = uri.removeprefix('artifact://')
            candidates.append(self.root / relative)
        elif not uri.startswith(('s3://', 'job://', 'contract://')):
            candidates.append(Path(uri) if uri.startswith('/') else self.root / uri)
            # workflow-api artifact references historically include the
            # logical bucket name even though the mounted PVC is that bucket.
            if uri.startswith('artifacts/'):
                candidates.append(self.root / uri.removeprefix('artifacts/'))

        # Candidates are tried in order (explicit metadata path first, then
        # URI-derived) and the first that is a regular file inside the root AND
        # hashes to the recorded digest wins. A symlink or any path escaping
        # the mount is skipped, never followed.
        for candidate in candidates:
            resolved = candidate.resolve()
            if not resolved.is_relative_to(self.root):
                continue
            if resolved.is_symlink() or not resolved.is_file():
                continue
            digest = sha256()
            with resolved.open('rb') as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b''):
                    digest.update(chunk)
            if digest.hexdigest() != artifact.sha256:
                raise ArtifactDeliveryError(
                    f'artifact digest mismatch: {artifact.uri}'
                )
            return resolved
        raise ArtifactDeliveryError(
            f'artifact content is unavailable: {artifact.uri}'
        )

    def read(self, artifact: ArtifactRecord, *, maximum_bytes: int) -> bytes:
        path = self.resolve(artifact)
        size = path.stat().st_size
        # Per-file cap before read_bytes() materializes the file; the aggregate
        # bundle limit is enforced separately in build_run_artifact_bundle.
        if size > maximum_bytes:
            raise ArtifactDeliveryError(
                f'artifact exceeds delivery limit: {artifact.uri}'
            )
        return path.read_bytes()


def _safe_archive_name(value: str) -> str:
    # Normalize to forward slashes and strip leading separators, then reject
    # any traversal component ('..') or empty part so no archive member can
    # escape its intended directory inside the zip.
    normalized = value.strip().replace('\\', '/').lstrip('/')
    path = PurePosixPath(normalized)
    if not normalized or '..' in path.parts or any(not part for part in path.parts):
        raise ArtifactDeliveryError(f'unsafe artifact archive path: {value}')
    return path.as_posix()


def _artifact_archive_name(artifact: ArtifactRecord) -> str:
    basename = Path(artifact.uri).name or Path(artifact.type).name
    if artifact.job_id:
        relative_type = _safe_archive_name(artifact.type)
        return f'jobs/{artifact.job_id}/{relative_type}'
    return f'run/{_safe_archive_name(artifact.type)}/{_safe_archive_name(basename)}'


def _latest_run_artifacts(
    artifacts: Iterable[ArtifactRecord],
) -> list[ArtifactRecord]:
    # Job artifacts are kept verbatim; run-level artifacts are deduplicated by
    # type with the LAST one winning (dict overwrite), so a re-recorded
    # artifact does not produce duplicate members in the bundle.
    latest: dict[str, ArtifactRecord] = {}
    job_artifacts: list[ArtifactRecord] = []
    for artifact in artifacts:
        if artifact.job_id:
            job_artifacts.append(artifact)
        else:
            latest[artifact.type] = artifact
    return [*job_artifacts, *latest.values()]


def build_run_artifact_bundle(
    *,
    run_id: str,
    artifacts: list[ArtifactRecord],
    jobs: list[JobRecord],
    shared_mount_root: str,
    maximum_bytes: int,
    include_source: bool = False,
) -> ArtifactBundle:
    # Only artifacts from succeeded jobs are eligible; a failed or still-running
    # job has no authoritative evidence worth shipping.
    succeeded_job_ids = {
        job.job_id for job in jobs if job.status == JobStatus.SUCCEEDED
    }
    selected = [
        artifact
        for artifact in _latest_run_artifacts(artifacts)
        if artifact.job_id is None or artifact.job_id in succeeded_job_ids
    ]
    if not include_source:
        # The uploaded task/source archive is bulk and duplicative; by default
        # it is excluded so the bundle stays within the Discord size budget.
        selected = [
            artifact
            for artifact in selected
            if Path(artifact.uri).name not in {'source.zip', 'task.zip'}
            and Path(artifact.type).name not in {'source.zip', 'task.zip'}
        ]

    reader = VerifiedArtifactReader(shared_mount_root)
    delivered: list[tuple[ArtifactRecord, str, bytes]] = []
    used_names: set[str] = set()
    total = 0
    unavailable: list[dict[str, str]] = []
    for artifact in selected:
        try:
            content = reader.read(artifact, maximum_bytes=maximum_bytes)
            archive_name = _artifact_archive_name(artifact)
            if archive_name in used_names:
                # Distinct artifacts can collide on the derived name; a digest
                # prefix disambiguates instead of overwriting the first member.
                archive_name = (
                    f'{archive_name}.{artifact.sha256[:12]}'
                )
            total += len(content)
            if total > maximum_bytes:
                raise ArtifactDeliveryError(
                    'artifact bundle exceeds the Discord delivery limit; '
                    'retry without source bundles or use the artifact store'
                )
            used_names.add(archive_name)
            delivered.append((artifact, archive_name, content))
        except ArtifactDeliveryError as exc:
            # A single bad artifact is reported, not fatal: the rest of the run
            # may still deliver and the manifest records the gap.
            unavailable.append({'uri': artifact.uri, 'reason': str(exc)})

    if not delivered:
        # An empty zip would look like success; fail loudly instead so the
        # caller can surface that every artifact was unavailable.
        raise ArtifactDeliveryError(
            'no digest-verified artifacts are currently available for this run'
        )

    manifest: dict[str, Any] = {
        'schema_version': 'glasslab-artifact-delivery-v1',
        'run_id': run_id,
        'include_source': include_source,
        'artifacts': [
            {
                'archive_path': archive_name,
                'artifact_id': artifact.artifact_id,
                'job_id': artifact.job_id,
                'type': artifact.type,
                'uri': artifact.uri,
                'sha256': artifact.sha256,
                'size_bytes': len(content),
            }
            for artifact, archive_name, content in delivered
        ],
        'unavailable': unavailable,
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            'artifact-manifest.json',
            json.dumps(manifest, indent=2, sort_keys=True) + '\n',
        )
        for _, archive_name, content in delivered:
            archive.writestr(archive_name, content)
    payload = output.getvalue()
    if len(payload) > maximum_bytes:
        raise ArtifactDeliveryError(
            'compressed artifact bundle exceeds the Discord delivery limit'
        )
    return ArtifactBundle(
        filename=f'glasslab-{run_id[:12]}-artifacts.zip',
        content=payload,
        artifact_count=len(delivered),
    )
