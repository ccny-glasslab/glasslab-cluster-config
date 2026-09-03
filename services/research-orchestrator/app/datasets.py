"""Immutable dataset uploads and content-addressed resolution.

Uploaded bytes are written once under a digest-derived directory and re-verified
at bind time, so a dataset reference always resolves to the exact bytes that
were registered.
"""

from __future__ import annotations

from hashlib import sha256
import ipaddress
import os
from pathlib import Path
import re
import socket
from tempfile import NamedTemporaryFile
from typing import BinaryIO
from urllib.parse import urlparse
import urllib.request

from .schemas import CatalogDatasetRecord, IngestedDatasetRecord
from .research_store import ResearchStore
from .storage import RecordNotFound
from .task_bundles import DatasetAsset, TaskBundleError


class DatasetIngestionError(ValueError):
    pass


class DatasetIngestionManager:
    """Stores immutable uploads and resolves stable dataset references."""

    _NAME_PATTERN = re.compile(r'^[a-z0-9][a-z0-9_-]{0,62}$')

    def __init__(
        self,
        *,
        store: ResearchStore,
        root: str,
        shared_mount_root: str,
        maximum_bytes: int,
    ) -> None:
        self.store = store
        self.root = Path(root).resolve()
        self.shared_mount_root = Path(shared_mount_root).resolve()
        self.maximum_bytes = maximum_bytes
        if not self.root.is_relative_to(self.shared_mount_root):
            raise DatasetIngestionError(
                'dataset upload root must be inside the shared mount'
            )

    @staticmethod
    def _safe_filename(filename: str) -> str:
        # The filename must be a bare basename: storage layout is derived from
        # content (the digest directory), so untrusted uploader input can never
        # influence the directory structure or escape the upload root.
        value = Path(filename).name.strip()
        if (
            not value
            or value in {'.', '..'}
            or len(value) > 255
            or any(character in value for character in ('\x00', '/', '\\'))
        ):
            raise DatasetIngestionError('dataset filename is invalid')
        return value

    @classmethod
    def _safe_name(cls, name: str) -> str:
        value = name.strip().lower().replace(' ', '_')
        if not cls._NAME_PATTERN.fullmatch(value):
            raise DatasetIngestionError(
                'dataset name must use lowercase letters, numbers, _ or -'
            )
        return value

    def ingest(
        self,
        source: BinaryIO,
        *,
        filename: str,
        name: str,
        role: str = 'input',
        contains_labels: bool = False,
        media_type: str | None = None,
        uploaded_by: str | None = None,
    ) -> IngestedDatasetRecord:
        safe_filename = self._safe_filename(filename)
        safe_name = self._safe_name(name)
        if not role.strip():
            raise DatasetIngestionError('dataset role is required')
        self.root.mkdir(parents=True, exist_ok=True)
        digest = sha256()
        size = 0
        with NamedTemporaryFile(dir=self.root, delete=False) as staged:
            staged_path = Path(staged.name)
            try:
                while chunk := source.read(1024 * 1024):
                    size += len(chunk)
                    if size > self.maximum_bytes:
                        raise DatasetIngestionError(
                            'dataset exceeds the configured upload size limit'
                        )
                    digest.update(chunk)
                    staged.write(chunk)
            except Exception:
                staged_path.unlink(missing_ok=True)
                raise
        if size == 0:
            staged_path.unlink(missing_ok=True)
            raise DatasetIngestionError('dataset is empty')

        actual_digest = digest.hexdigest()
        try:
            existing = self.store.get_dataset(actual_digest)
        except RecordNotFound:
            existing = None
        if existing is not None:
            # Content-addressed dedup: identical bytes return the first record.
            # contains_labels is a declaration about those bytes, so a mismatch
            # is an error rather than a silent overwrite; other metadata fields
            # are deliberately not compared (the first registration wins).
            staged_path.unlink(missing_ok=True)
            if existing.contains_labels != contains_labels:
                raise DatasetIngestionError(
                    'dataset content already exists with a different label '
                    'declaration'
                )
            return existing

        destination = self.root / actual_digest / safe_filename
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            staged_path.unlink(missing_ok=True)
        else:
            os.replace(staged_path, destination)
        destination.chmod(0o444)
        destination.parent.chmod(0o555)
        # Write-once on disk: the file is 0o444 and its digest directory 0o555,
        # so nothing later can mutate a registered dataset in place.
        artifact_uri = (
            's3://artifacts/'
            + destination.relative_to(self.shared_mount_root).as_posix()
        )
        record = IngestedDatasetRecord(
            dataset_id=actual_digest,
            name=safe_name,
            filename=safe_filename,
            reference_uri=f'glasslab-dataset://{actual_digest}',
            artifact_uri=artifact_uri,
            path=str(destination),
            sha256=actual_digest,
            size_bytes=size,
            media_type=media_type,
            role=role.strip(),
            contains_labels=contains_labels,
            uploaded_by=uploaded_by,
        )
        return self.store.save_dataset(record)

    def ingest_bytes(self, content: bytes, **metadata) -> IngestedDatasetRecord:
        from io import BytesIO

        return self.ingest(BytesIO(content), **metadata)

    def resolve(
        self,
        reference_uri: str,
        *,
        name: str,
        role: str,
        contains_labels: bool,
        expected_sha256: str | None = None,
    ) -> DatasetAsset:
        prefix = 'glasslab-dataset://'
        if not reference_uri.startswith(prefix):
            raise TaskBundleError('dataset reference is not approved')
        dataset_id = reference_uri.removeprefix(prefix)
        try:
            record = self.store.get_dataset(dataset_id)
        except RecordNotFound as exc:
            raise TaskBundleError(
                f'ingested dataset is not registered: {reference_uri}'
            ) from exc
        path = Path(record.path).resolve()
        if (
            not path.is_relative_to(self.root)
            or not path.is_file()
            or path.stat().st_size != record.size_bytes
        ):
            raise TaskBundleError(
                f'ingested dataset is unavailable: {reference_uri}'
            )
        actual_digest = self._file_sha256(path)
        # Beyond the registry record, the on-disk bytes are re-hashed so
        # tampering or partial overwrite is caught at bind time, and the
        # proposal's expected checksum plus label declaration are enforced.
        if actual_digest != record.sha256:
            raise TaskBundleError(
                f'ingested dataset failed checksum verification: {reference_uri}'
            )
        if expected_sha256 and expected_sha256 != record.sha256:
            raise TaskBundleError(
                f'ingested dataset checksum does not match proposal: {name}'
            )
        if contains_labels != record.contains_labels:
            raise TaskBundleError(
                f'ingested dataset label declaration does not match registry: '
                f'{name}'
            )
        return DatasetAsset(
            name=name,
            uri=record.artifact_uri,
            sha256=record.sha256,
            role=role,
            contains_labels=record.contains_labels,
        )

    def register_upload(
        self,
        source: BinaryIO,
        *,
        filename: str,
        name: str,
        role: str = 'input',
        contains_labels: bool = False,
        media_type: str | None = None,
        created_by: str = 'operator',
    ) -> CatalogDatasetRecord:
        """Register an uploaded dataset in the catalog (provenance: upload)."""
        ingested = self.ingest(
            source,
            filename=filename,
            name=name,
            role=role,
            contains_labels=contains_labels,
            media_type=media_type,
            uploaded_by=created_by,
        )
        record = CatalogDatasetRecord(
            name=ingested.name,
            reference_uri=ingested.reference_uri,
            artifact_uri=ingested.artifact_uri,
            sha256=ingested.sha256,
            size_bytes=ingested.size_bytes,
            provenance='upload',
            created_by=created_by,
        )
        return self.store.save_catalog_dataset(record)

    @staticmethod
    def _validate_public_url(url: str) -> None:
        try:
            parsed = urlparse(url)
            port = parsed.port
        except ValueError as exc:
            raise DatasetIngestionError(
                'dataset URL is malformed'
            ) from exc
        if (
            parsed.scheme != 'https'
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or port not in {None, 443}
        ):
            raise DatasetIngestionError(
                'dataset URLs require a public HTTPS URL'
            )
        try:
            addresses = socket.getaddrinfo(
                parsed.hostname,
                443,
                type=socket.SOCK_STREAM,
            )
        except OSError as exc:
            raise DatasetIngestionError(
                f'cannot resolve dataset host: {parsed.hostname}'
            ) from exc
        for address in addresses:
            ip = ipaddress.ip_address(address[4][0])
            if not ip.is_global:
                raise DatasetIngestionError(
                    f'dataset host resolves to a non-public address: {ip}'
                )

    def register_url(
        self,
        *,
        url: str,
        name: str,
        expected_sha256: str | None = None,
        role: str = 'input',
        contains_labels: bool = False,
        created_by: str = 'operator',
    ) -> CatalogDatasetRecord:
        """Register a dataset by URL (provenance: url).

        Only public HTTPS targets are accepted (mirrors the task-asset
        fetcher); each redirect hop is re-validated. The stream is size-capped
        and sha256-verified, then lands through the immutable ingest path.
        """
        self._validate_public_url(url)
        self.root.mkdir(parents=True, exist_ok=True)
        current = url
        for _ in range(5):
            request = urllib.request.Request(
                current,
                headers={'User-Agent': 'glasslab-research-orchestrator/1'},
            )
            digest = sha256()
            size = 0
            with NamedTemporaryFile(dir=self.root, delete=False) as staged:
                staged_path = Path(staged.name)
                try:
                    with urllib.request.urlopen(request, timeout=60) as resp:
                        if resp.geturl() != current:
                            self._validate_public_url(resp.geturl())
                            current = resp.geturl()
                        while chunk := resp.read(1024 * 1024):
                            size += len(chunk)
                            if size > self.maximum_bytes:
                                raise DatasetIngestionError(
                                    'dataset URL exceeds the configured '
                                    'size limit'
                                )
                            digest.update(chunk)
                            staged.write(chunk)
                except urllib.error.HTTPError as exc:
                    if exc.code in (301, 302, 303, 307, 308):
                        location = exc.headers.get('Location')
                        if not location:
                            raise DatasetIngestionError(
                                'dataset URL redirect without a target'
                            ) from exc
                        self._validate_public_url(location)
                        current = location
                        continue
                    raise DatasetIngestionError(
                        f'dataset URL fetch failed: HTTP {exc.code}'
                    ) from exc
                except urllib.error.URLError as exc:
                    raise DatasetIngestionError(
                        f'dataset URL fetch failed: {exc.reason}'
                    ) from exc
                break
        else:
            raise DatasetIngestionError(
                'dataset URL exceeded the redirect limit'
            )
        fetched_digest = digest.hexdigest()
        if expected_sha256 and fetched_digest != expected_sha256:
            raise DatasetIngestionError(
                'dataset URL content does not match expected_sha256'
            )
        filename = Path(urlparse(url).path).name or 'dataset'
        with staged_path.open('rb') as handle:
            ingested = self.ingest(
                handle,
                filename=filename,
                name=name,
                role=role,
                contains_labels=contains_labels,
                media_type='application/octet-stream',
                uploaded_by=created_by,
            )
        staged_path.unlink(missing_ok=True)
        record = CatalogDatasetRecord(
            name=ingested.name,
            reference_uri=ingested.reference_uri,
            artifact_uri=ingested.artifact_uri,
            sha256=ingested.sha256,
            size_bytes=ingested.size_bytes,
            provenance='url',
            source_url=url,
            created_by=created_by,
        )
        return self.store.save_catalog_dataset(record)

    @staticmethod
    def _file_sha256(path: Path) -> str:
        digest = sha256()
        with path.open('rb') as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b''):
                digest.update(chunk)
        return digest.hexdigest()
