"""Seal, verify, and promote evaluation-contract candidates.

Candidates are agent-proposed bundles; sealing freezes them into an immutable,
digest-named blob; promotion binds a contract_id/version to exactly one digest
in the trusted catalog. Digests are recomputed and cross-checked at every stage,
so nothing is trusted from the proposing agent except the file bytes that pass
validation.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
from uuid import uuid4

from .contracts import (
    ContractIntegrityError,
    compute_contract_digest,
)
from .schemas import EvaluationContractDescriptor


class ContractCandidateError(ValueError):
    pass


@dataclass(frozen=True)
class SealedContractCandidate:
    contract_id: str
    version: str
    digest: str
    sealed_path: Path
    descriptor: EvaluationContractDescriptor


class ContractCandidateManager:
    MAX_FILES = 64
    MAX_TOTAL_BYTES = 2 * 1024 * 1024
    ALLOWED_SUFFIXES = {'.json', '.md', '.py', '.txt'}

    def __init__(
        self,
        *,
        sealed_root: str,
        promoted_root: str,
        catalog_path: str,
        shared_mount_root: str,
    ) -> None:
        self.sealed_root = Path(sealed_root).resolve()
        self.promoted_root = Path(promoted_root).resolve()
        self.catalog_path = Path(catalog_path).resolve()
        self.shared_mount_root = Path(shared_mount_root).resolve()

    @staticmethod
    def _validate_source_tree(source: Path) -> list[Path]:
        # Symlinks are rejected anywhere in the tree (even under a directory
        # that would otherwise be skipped) and a pre-existing contract.sha256
        # is forbidden: the agent must never control the checksum file, and
        # every hashed byte must be a real file a reviewer saw. Restricting to
        # text suffixes keeps the bundle auditable and diffable.
        if source.is_symlink() or not source.is_dir():
            raise ContractCandidateError(
                'contract candidate must be a real directory'
            )
        files: list[Path] = []
        total_bytes = 0
        for path in sorted(source.rglob('*')):
            if path.is_symlink():
                raise ContractCandidateError(
                    f'contract candidate cannot contain symlinks: {path}'
                )
            if path.is_dir():
                continue
            relative = path.relative_to(source)
            # Bytecode caches are interpreter byproducts of the agent's local
            # checks, not candidate content; the review copy already ignores
            # them, so sealing skips them instead of failing the candidate.
            if '__pycache__' in relative.parts or path.suffix == '.pyc':
                continue
            if (
                relative.name == 'contract.sha256'
                or path.suffix not in ContractCandidateManager.ALLOWED_SUFFIXES
            ):
                raise ContractCandidateError(
                    f'unsupported contract candidate file: {relative}'
                )
            files.append(path)
            total_bytes += path.stat().st_size
        if not files:
            raise ContractCandidateError('contract candidate is empty')
        if len(files) > ContractCandidateManager.MAX_FILES:
            raise ContractCandidateError('contract candidate has too many files')
        if total_bytes > ContractCandidateManager.MAX_TOTAL_BYTES:
            raise ContractCandidateError('contract candidate is too large')
        return files

    @staticmethod
    def _validate_descriptor(
        root: Path,
        *,
        contract_id: str,
        version: str,
    ) -> EvaluationContractDescriptor:
        descriptor_path = root / 'contract.json'
        if not descriptor_path.is_file():
            raise ContractCandidateError('candidate is missing contract.json')
        try:
            descriptor = EvaluationContractDescriptor.model_validate_json(
                descriptor_path.read_text(encoding='utf-8')
            )
        except Exception as exc:
            raise ContractCandidateError(
                f'candidate contract.json is invalid: {exc}'
            ) from exc
        if descriptor.contract_id != contract_id or descriptor.version != version:
            raise ContractCandidateError(
                'candidate descriptor identity does not match the proposal'
            )
        if descriptor.container_image_digest is not None:
            # A candidate cannot pin its own container image; image selection
            # stays with promotion/registration so agents cannot choose to run
            # unvetted code as the evaluator.
            raise ContractCandidateError(
                'shared-bundle candidates cannot choose a container image'
            )
        primary_metric = str(descriptor.manifest.get('primary_metric', '')).strip()
        direction = str(
            descriptor.manifest.get('primary_metric_direction', '')
        ).strip()
        if not primary_metric or direction not in {'maximize', 'minimize'}:
            raise ContractCandidateError(
                'manifest requires primary_metric and a valid direction'
            )
        try:
            for field in (
                descriptor.execution_wrapper,
                descriptor.evaluation_entry_point,
                descriptor.expected_input_schema,
                descriptor.expected_output_schema,
            ):
                target = (root / field).resolve()
                if not target.is_relative_to(root) or not target.is_file():
                    raise ContractCandidateError(
                        f'candidate references missing file: {field}'
                    )
            for schema_path in (
                descriptor.expected_input_schema,
                descriptor.expected_output_schema,
            ):
                parsed = json.loads((root / schema_path).read_text(encoding='utf-8'))
                if not isinstance(parsed, dict):
                    raise ContractCandidateError(
                        f'JSON schema must be an object: {schema_path}'
                    )
            for python_path in (
                descriptor.execution_wrapper,
                descriptor.evaluation_entry_point,
            ):
                # AST parsing is deliberately syntax-only: candidates are Python
                # that will later run as the evaluator, so static validation
                # catches breakage without executing anything untrusted.
                ast.parse(
                    (root / python_path).read_text(encoding='utf-8'),
                    filename=python_path,
                )
        except (OSError, ValueError, SyntaxError, json.JSONDecodeError) as exc:
            raise ContractCandidateError(str(exc)) from exc
        return descriptor

    def seal(
        self,
        *,
        source: Path,
        contract_id: str,
        version: str,
    ) -> SealedContractCandidate:
        self._validate_source_tree(source)
        descriptor = self._validate_descriptor(
            source,
            contract_id=contract_id,
            version=version,
        )
        staging_parent = self.sealed_root / '.staging'
        staging_parent.mkdir(parents=True, exist_ok=True)
        staging = staging_parent / uuid4().hex
        # Bytecode caches are interpreter byproducts of the agent's local
        # checks, never reviewed content; they are excluded so the sealed
        # digest only covers what a reviewer could read.
        shutil.copytree(
            source,
            staging,
            ignore=shutil.ignore_patterns('__pycache__', '*.pyc'),
        )
        # The digest is computed over the staging copy before contract.sha256
        # exists, so the checksum file can never influence its own digest.
        digest = compute_contract_digest(staging)
        (staging / 'contract.sha256').write_text(digest + '\n', encoding='ascii')
        destination = self.sealed_root / contract_id / version / digest
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            # The same digest was sealed before: discard the new staging copy
            # rather than overwriting, so sealing is idempotent and the first
            # seal always wins.
            shutil.rmtree(staging)
        else:
            os.replace(staging, destination)
        # Read-only bits are set after the atomic move so a sealed bundle is
        # write-once from the orchestrator's perspective.
        for path in destination.rglob('*'):
            path.chmod(0o555 if path.is_dir() else 0o444)
        return SealedContractCandidate(
            contract_id=contract_id,
            version=version,
            digest=digest,
            sealed_path=destination,
            descriptor=descriptor,
        )

    def verify_seal(
        self,
        *,
        sealed_path: Path,
        expected_digest: str,
    ) -> EvaluationContractDescriptor:
        path = sealed_path.resolve()
        if not path.is_relative_to(self.sealed_root):
            raise ContractCandidateError('sealed candidate escapes candidate root')
        expected = (path / 'contract.sha256').read_text().strip()
        actual = compute_contract_digest(path)
        # Recompute the digest over the sealed tree and compare against both
        # the recorded expected digest and the bundle's own checksum file;
        # either mismatch means the sealed bytes drifted from what was approved.
        if expected != expected_digest or actual != expected_digest:
            raise ContractIntegrityError('sealed candidate digest mismatch')
        return EvaluationContractDescriptor.model_validate_json(
            (path / 'contract.json').read_text()
        )

    def promote(
        self,
        *,
        sealed_path: Path,
        expected_digest: str,
    ) -> Path:
        descriptor = self.verify_seal(
            sealed_path=sealed_path,
            expected_digest=expected_digest,
        )
        destination = (
            self.promoted_root / descriptor.contract_id / descriptor.version
        )
        # A promoted id/version is immutable: if it already exists under a
        # different digest the promotion is refused rather than overwritten.
        if destination.exists():
            existing = compute_contract_digest(destination)
            if existing != expected_digest:
                raise ContractCandidateError(
                    'contract version is already promoted with another digest'
                )
        else:
            # Copy-then-rename keeps promotion atomic so a resolver never
            # observes a half-written contract tree.
            staging = destination.parent / f'.{descriptor.version}.{uuid4().hex}'
            staging.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(sealed_path, staging)
            os.replace(staging, destination)
        for path in destination.rglob('*'):
            path.chmod(0o555 if path.is_dir() else 0o444)
        self._write_catalog_entry(
            descriptor=descriptor,
            digest=expected_digest,
            promoted_path=destination,
        )
        return destination

    def install_repository_contract(self, source: Path) -> Path:
        """Install a checksum-pinned contract shipped in the service image."""
        source = source.resolve()
        descriptor = self._validate_descriptor(
            source,
            contract_id=source.parent.name,
            version=source.name,
        )
        # Repository-shipped contracts already carry a pinned contract.sha256;
        # the checksum is verified before install.
        expected = (source / 'contract.sha256').read_text(
            encoding='ascii'
        ).strip()
        actual = compute_contract_digest(source)
        if expected != actual:
            raise ContractIntegrityError(
                f'repository contract digest mismatch: {descriptor.contract_id}'
            )
        destination = (
            self.promoted_root / descriptor.contract_id / descriptor.version
        )
        if destination.exists():
            if compute_contract_digest(destination) != actual:
                raise ContractCandidateError(
                    'repository contract conflicts with a promoted version: '
                    f'{descriptor.contract_id}@{descriptor.version}'
                )
        else:
            staging = destination.parent / f'.{descriptor.version}.{uuid4().hex}'
            staging.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(
                source,
                staging,
                # Ignore pycache/pyc because the digest excludes them anyway,
                # keeping the promoted tree byte-identical to what was hashed.
                ignore=shutil.ignore_patterns('__pycache__', '*.pyc'),
            )
            os.replace(staging, destination)
        for path in destination.rglob('*'):
            path.chmod(0o555 if path.is_dir() else 0o444)
        self._write_catalog_entry(
            descriptor=descriptor,
            digest=actual,
            promoted_path=destination,
        )
        return destination

    def _write_catalog_entry(
        self,
        *,
        descriptor: EvaluationContractDescriptor,
        digest: str,
        promoted_path: Path,
    ) -> None:
        # The catalog is the resolution anchor for cluster jobs: bundle paths
        # are stored relative to the shared mount so renderers never depend on
        # host paths. The tmp+replace write keeps the catalog atomic under
        # concurrent readers.
        if not promoted_path.is_relative_to(self.shared_mount_root):
            raise ContractCandidateError(
                'promoted contract is outside the shared mount root'
            )
        self.catalog_path.parent.mkdir(parents=True, exist_ok=True)
        catalog: dict[str, dict[str, str]] = {}
        if self.catalog_path.is_file():
            parsed = json.loads(self.catalog_path.read_text(encoding='utf-8'))
            if not isinstance(parsed, dict):
                raise ContractCandidateError('trusted contract catalog is invalid')
            catalog = parsed
        key = f'{descriptor.contract_id}@{descriptor.version}'
        catalog[key] = {
            'contract_id': descriptor.contract_id,
            'version': descriptor.version,
            'digest': digest,
            'bundle_path': promoted_path.relative_to(
                self.shared_mount_root
            ).as_posix(),
            'execution_wrapper': descriptor.execution_wrapper,
            'evaluation_entry_point': descriptor.evaluation_entry_point,
        }
        temporary = self.catalog_path.with_suffix('.tmp')
        temporary.write_text(
            json.dumps(catalog, indent=2, sort_keys=True) + '\n',
            encoding='utf-8',
        )
        os.replace(temporary, self.catalog_path)
