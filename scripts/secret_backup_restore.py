#!/usr/bin/env python3
"""Inventory-driven backup and staged restore for an encrypted SOPS vault.

This module deliberately has no decryption operation.  It copies and validates
already-encrypted SOPS YAML documents plus non-secret recovery metadata.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import ctypes
import datetime as dt
import errno
import hashlib
import io
import os
import re
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Iterable

import yaml


ARCHIVE_PREFIX = "glasslab-secrets-"
ARCHIVE_SUFFIX = ".tar.gz"
CHECKSUM_NAME = "SHA256SUMS"
INVENTORY_ARCHIVE_PATH = "vault/inventory.yaml"
POLICY_ARCHIVE_PATH = "policy/.sops.yaml"
MAX_METADATA_BYTES = 4 * 1024 * 1024
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_MEMBERS = 10_000
SHA256_LINE = re.compile(r"^([0-9a-f]{64})  ([^\x00\r\n]+)$")
STAMP = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
FINGERPRINT = re.compile(r"^[0-9A-Fa-f]{40}$")
ENC_ENVELOPE = re.compile(
    r"^ENC\[AES256_GCM,data:([A-Za-z0-9+/]+={0,2}),"
    r"iv:([A-Za-z0-9+/]+={0,2}),tag:([A-Za-z0-9+/]+={0,2}),type:str\]$"
)
SOPS_CREATED_AT = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$"
)
PGP_MESSAGE_BEGIN = "-----BEGIN PGP MESSAGE-----"
PGP_MESSAGE_END = "-----END PGP MESSAGE-----"
PGP_BODY_LINE = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")
PGP_CRC_LINE = re.compile(r"^=[A-Za-z0-9+/]{4}$")
UNSUPPORTED_RECIPIENT_KEYS = ("age", "kms", "gcp_kms", "azure_kv", "hc_vault")
TRUSTED_TAR_PATH = Path("/usr/bin/tar")


class ArchiveBoundaryError(RuntimeError):
    """An expected safety validation failed without exposing file contents."""


class OperationInterrupted(ArchiveBoundaryError):
    """The caller interrupted an operation before its critical section."""


class ManualRecoveryRequired(ArchiveBoundaryError):
    """A completed exchange left the old vault in staging for manual recovery."""

    def __init__(self, preserved_vault: Path):
        self.preserved_vault = preserved_vault
        super().__init__(
            "vault exchange completed but automatic rollback failed; "
            f"the previous vault remains at {preserved_vault}"
        )


@dataclass(frozen=True)
class InventoryRecord:
    name: str
    relative_path: str


@dataclass
class SignalState:
    """Distinguish pre-commit interruption from a signal observed after commit."""

    critical_section: bool = False
    restore_committed: bool = False
    deferred_signals: list[int] = field(default_factory=list)


class UniqueKeySafeLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects duplicate mappings instead of hiding data."""


def _construct_unique_mapping(
    loader: UniqueKeySafeLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[object, object]:
    loader.flatten_mapping(node)
    mapping: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as exc:
            raise yaml.YAMLError("unhashable YAML mapping key") from exc
        if duplicate:
            raise yaml.YAMLError("duplicate YAML mapping key")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


UniqueKeySafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping
)


def _safe_relative_path(value: object, *, suffix: str | None = None) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ArchiveBoundaryError("inventory contains an invalid relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ArchiveBoundaryError("inventory contains an unsafe relative path")
    normalized = path.as_posix()
    if normalized != value:
        raise ArchiveBoundaryError("inventory contains a non-normalized relative path")
    if suffix is not None and not normalized.endswith(suffix):
        raise ArchiveBoundaryError(f"inventory paths must end with {suffix}")
    return normalized


def _read_regular_file(path: Path, *, maximum: int = MAX_METADATA_BYTES) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ArchiveBoundaryError(f"required regular file is unavailable: {path}") from exc
    try:
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise ArchiveBoundaryError(f"path is not a regular file: {path}")
        if file_stat.st_size > maximum:
            raise ArchiveBoundaryError(f"file exceeds the recovery size limit: {path}")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            data = stream.read(maximum + 1)
        if len(data) > maximum:
            raise ArchiveBoundaryError(f"file exceeds the recovery size limit: {path}")
        return data
    finally:
        os.close(descriptor)


def _load_yaml_bytes(contents: bytes, *, label: str) -> object:
    try:
        text = contents.decode("utf-8")
        return yaml.load(text, Loader=UniqueKeySafeLoader)
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ArchiveBoundaryError(f"invalid YAML in {label}") from exc


def _load_inventory_bytes(contents: bytes) -> list[InventoryRecord]:
    document = _load_yaml_bytes(contents, label="inventory")
    if not isinstance(document, dict) or document.get("version") != 1:
        raise ArchiveBoundaryError("inventory must declare version 1")
    entries = document.get("secrets")
    if not isinstance(entries, list) or not entries:
        raise ArchiveBoundaryError("inventory must contain at least one secret record")

    records: list[InventoryRecord] = []
    names: set[str] = set()
    paths: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ArchiveBoundaryError("inventory contains a malformed secret record")
        name = entry.get("name")
        if not isinstance(name, str) or not name.strip() or name != name.strip():
            raise ArchiveBoundaryError("inventory contains an invalid secret name")
        relative_path = _safe_relative_path(
            entry.get("relative_path"), suffix=".sops.yaml"
        )
        for required in ("target", "owner"):
            value = entry.get(required)
            if not isinstance(value, str) or not value.strip():
                raise ArchiveBoundaryError(f"inventory record is missing {required}")
        if name in names:
            raise ArchiveBoundaryError("inventory contains a duplicate secret name")
        if relative_path in paths:
            raise ArchiveBoundaryError("inventory contains a duplicate secret path")
        names.add(name)
        paths.add(relative_path)
        records.append(InventoryRecord(name=name, relative_path=relative_path))
    return records


def _walk_vault_sops_files(vault: Path) -> set[str]:
    discovered: set[str] = set()

    def traversal_failed(exc: OSError) -> None:
        raise ArchiveBoundaryError("vault traversal failed before inventory coverage was complete") from exc

    for directory, directory_names, file_names in os.walk(
        vault, followlinks=False, onerror=traversal_failed
    ):
        current = Path(directory)
        for name in [*directory_names, *file_names]:
            candidate = current / name
            try:
                candidate_stat = candidate.lstat()
            except OSError as exc:
                raise ArchiveBoundaryError("vault changed while inventory was checked") from exc
            if stat.S_ISLNK(candidate_stat.st_mode):
                raise ArchiveBoundaryError(f"vault contains a symbolic link: {candidate.relative_to(vault)}")
        for name in file_names:
            if name.endswith(".sops.yaml"):
                relative = (current / name).relative_to(vault).as_posix()
                discovered.add(_safe_relative_path(relative, suffix=".sops.yaml"))
    return discovered


def _is_sops_envelope(value: object) -> bool:
    if not isinstance(value, str):
        return False
    match = ENC_ENVELOPE.fullmatch(value)
    if match is None:
        return False
    try:
        decoded = [base64.b64decode(field, validate=True) for field in match.groups()]
    except (binascii.Error, ValueError):
        return False
    return all(decoded)


def _is_openpgp_recipient(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    fingerprint = value.get("fp")
    created_at = value.get("created_at")
    encrypted_key = value.get("enc")
    if not isinstance(fingerprint, str) or FINGERPRINT.fullmatch(fingerprint) is None:
        return False
    if not isinstance(created_at, str) or SOPS_CREATED_AT.fullmatch(created_at) is None:
        return False
    try:
        parsed_time = dt.datetime.fromisoformat(created_at[:-1] + "+00:00")
    except ValueError:
        return False
    if parsed_time.utcoffset() != dt.timedelta(0):
        return False
    if not isinstance(encrypted_key, str):
        return False
    prefix = PGP_MESSAGE_BEGIN + "\n\n"
    suffix = "\n" + PGP_MESSAGE_END
    if not encrypted_key.startswith(prefix) or not encrypted_key.endswith(suffix):
        return False
    body_lines = encrypted_key[len(prefix) : -len(suffix)].splitlines()
    if not body_lines or "\x00" in encrypted_key:
        return False
    if PGP_CRC_LINE.fullmatch(body_lines[-1]):
        body_lines = body_lines[:-1]
    if not body_lines or any(PGP_BODY_LINE.fullmatch(line) is None for line in body_lines):
        return False
    try:
        decoded_body = base64.b64decode("".join(body_lines), validate=True)
    except (binascii.Error, ValueError):
        return False
    return bool(decoded_body)


def _policy_pgp_fingerprints(value: object) -> list[str]:
    if not isinstance(value, str) or not value.strip():
        raise ArchiveBoundaryError("SOPS policy has invalid OpenPGP recipients")
    fingerprints = [item.strip() for item in value.split(",")]
    if any(not item or FINGERPRINT.fullmatch(item) is None for item in fingerprints):
        raise ArchiveBoundaryError("SOPS policy has invalid OpenPGP recipients")
    return fingerprints


def _validate_sops_document(contents: bytes, *, label: str) -> None:
    document = _load_yaml_bytes(contents, label=label)
    if not isinstance(document, dict):
        raise ArchiveBoundaryError(f"encrypted document lacks SOPS metadata: {label}")
    metadata = document.get("sops")
    if not isinstance(metadata, dict):
        raise ArchiveBoundaryError(f"encrypted document lacks SOPS metadata: {label}")
    mac = metadata.get("mac")
    if not _is_sops_envelope(mac):
        raise ArchiveBoundaryError(f"encrypted document has invalid SOPS MAC metadata: {label}")

    pgp = metadata.get("pgp")
    if not isinstance(pgp, list) or not pgp or not all(
        _is_openpgp_recipient(item) for item in pgp
    ):
        raise ArchiveBoundaryError(
            f"encrypted document has invalid OpenPGP recipient metadata: {label}"
        )
    if any(metadata.get(key) not in (None, []) for key in UNSUPPORTED_RECIPIENT_KEYS):
        raise ArchiveBoundaryError(
            f"encrypted document uses unsupported recipient metadata: {label}"
        )

    payload_count = 0
    for field in ("data", "stringData"):
        if field not in document:
            continue
        payload = document[field]
        if not isinstance(payload, dict):
            raise ArchiveBoundaryError(
                f"encrypted document has malformed secret payload metadata: {label}"
            )
        for key, value in payload.items():
            if not isinstance(key, str) or not key or not _is_sops_envelope(value):
                raise ArchiveBoundaryError(
                    f"encrypted document contains a non-ciphertext secret payload: {label}"
                )
            payload_count += 1
    if payload_count == 0:
        raise ArchiveBoundaryError(
            f"encrypted document contains no encrypted secret payload: {label}"
        )


def _validate_policy(contents: bytes) -> None:
    document = _load_yaml_bytes(contents, label="SOPS policy")
    if not isinstance(document, dict):
        raise ArchiveBoundaryError("SOPS policy is not a mapping")
    rules = document.get("creation_rules")
    if not isinstance(rules, list) or not rules:
        raise ArchiveBoundaryError("SOPS policy has no creation rules")
    for rule in rules:
        if not isinstance(rule, dict):
            raise ArchiveBoundaryError("SOPS policy contains a malformed creation rule")
        if any(rule.get(key) not in (None, [], "") for key in UNSUPPORTED_RECIPIENT_KEYS):
            raise ArchiveBoundaryError("SOPS policy uses unsupported recipients")
        _policy_pgp_fingerprints(rule.get("pgp"))


def _sha256(contents: bytes) -> str:
    return hashlib.sha256(contents).hexdigest()


def _checksum_document(files: dict[str, bytes]) -> bytes:
    return "".join(
        f"{_sha256(files[name])}  {name}\n" for name in sorted(files)
    ).encode("ascii")


def _validate_private_vault_root(vault: Path) -> None:
    try:
        vault_stat = vault.lstat()
    except OSError as exc:
        raise ArchiveBoundaryError(f"vault directory is unavailable: {vault}") from exc
    if not stat.S_ISDIR(vault_stat.st_mode) or stat.S_ISLNK(vault_stat.st_mode):
        raise ArchiveBoundaryError(f"vault path is not a real directory: {vault}")
    if stat.S_IMODE(vault_stat.st_mode) & 0o077:
        raise ArchiveBoundaryError("vault directory permissions must not grant group or other access")


def _archive_info(name: str, contents: bytes) -> tarfile.TarInfo:
    member = tarfile.TarInfo(name)
    member.size = len(contents)
    member.mode = 0o600
    member.mtime = int(dt.datetime.now(dt.timezone.utc).timestamp())
    member.uid = 0
    member.gid = 0
    member.uname = ""
    member.gname = ""
    return member


def _write_archive(path: Path, files: dict[str, bytes]) -> None:
    with tarfile.open(path, "w:gz", format=tarfile.PAX_FORMAT) as bundle:
        for name in sorted(files):
            contents = files[name]
            bundle.addfile(_archive_info(name, contents), io.BytesIO(contents))
    path.chmod(0o600)


def _remove_owned_publication(source: Path, destination: Path) -> None:
    """Remove destination only when it is the hard link made from source."""
    try:
        source_stat = source.lstat()
        destination_stat = destination.lstat()
    except FileNotFoundError:
        return
    except OSError:
        return
    if (
        stat.S_ISREG(source_stat.st_mode)
        and stat.S_ISREG(destination_stat.st_mode)
        and source_stat.st_dev == destination_stat.st_dev
        and source_stat.st_ino == destination_stat.st_ino
    ):
        try:
            destination.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


def create_backup(*, vault: Path, policy: Path, output: Path) -> Path:
    _validate_private_vault_root(vault)
    inventory_path = vault / "inventory.yaml"
    inventory_contents = _read_regular_file(inventory_path)
    records = _load_inventory_bytes(inventory_contents)
    inventory_paths = {record.relative_path for record in records}
    discovered_paths = _walk_vault_sops_files(vault)
    if inventory_paths != discovered_paths:
        missing = inventory_paths - discovered_paths
        extra = discovered_paths - inventory_paths
        if missing:
            raise ArchiveBoundaryError("inventory references an unavailable encrypted document")
        if extra:
            raise ArchiveBoundaryError("vault contains an encrypted document missing from inventory")

    policy_contents = _read_regular_file(policy)
    _validate_policy(policy_contents)
    files: dict[str, bytes] = {
        INVENTORY_ARCHIVE_PATH: inventory_contents,
        POLICY_ARCHIVE_PATH: policy_contents,
    }
    for record in records:
        source = vault / Path(*PurePosixPath(record.relative_path).parts)
        contents = _read_regular_file(source)
        _validate_sops_document(contents, label=record.relative_path)
        files[f"vault/{record.relative_path}"] = contents
    files[CHECKSUM_NAME] = _checksum_document(files)

    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    output.parent.chmod(0o700)
    checksum_output = output.with_name(output.name + ".sha256")
    if output.exists() or checksum_output.exists():
        raise ArchiveBoundaryError("backup output already exists")
    with tempfile.TemporaryDirectory(
        prefix=".glasslab-secret-backup-", dir=output.parent
    ) as directory:
        workspace = Path(directory)
        workspace.chmod(0o700)
        temporary_archive = workspace / "archive.tmp"
        _write_archive(temporary_archive, files)
        archive_digest = _sha256(temporary_archive.read_bytes())
        temporary_checksum = workspace / "archive.sha256.tmp"
        temporary_checksum.write_text(
            f"{archive_digest}  {output.name}\n", encoding="ascii"
        )
        temporary_checksum.chmod(0o600)
        try:
            os.link(temporary_archive, output)
            os.link(temporary_checksum, checksum_output)
        except BaseException as exc:
            _remove_owned_publication(temporary_checksum, checksum_output)
            _remove_owned_publication(temporary_archive, output)
            if isinstance(exc, OSError):
                raise ArchiveBoundaryError(
                    "could not publish the completed backup atomically"
                ) from exc
            raise
    return output


def _copy_open_regular_file(source: Path, destination: Path, *, maximum: int) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        source_fd = os.open(source, flags)
    except OSError as exc:
        raise ArchiveBoundaryError(f"required regular file is unavailable: {source}") from exc
    try:
        source_stat = os.fstat(source_fd)
        if not stat.S_ISREG(source_stat.st_mode) or source_stat.st_size > maximum:
            raise ArchiveBoundaryError(f"path is not an acceptable regular file: {source}")
        destination_fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(source_fd, "rb", closefd=False) as input_stream:
                with os.fdopen(destination_fd, "wb", closefd=False) as output_stream:
                    shutil.copyfileobj(input_stream, output_stream, length=1024 * 1024)
                    output_stream.flush()
                    os.fsync(destination_fd)
        finally:
            os.close(destination_fd)
    finally:
        os.close(source_fd)


def _parse_archive_checksum(contents: bytes, *, archive_name: str) -> str:
    try:
        text = contents.decode("ascii")
    except UnicodeDecodeError as exc:
        raise ArchiveBoundaryError("archive checksum is not valid ASCII") from exc
    lines = text.splitlines()
    if len(lines) != 1:
        raise ArchiveBoundaryError("archive checksum must contain exactly one entry")
    match = SHA256_LINE.fullmatch(lines[0])
    if match is None or match.group(2) != archive_name:
        raise ArchiveBoundaryError("archive checksum names an unexpected file")
    return match.group(1)


def verify_archive_checksum(*, archive: Path, checksum: Path) -> None:
    expected = _parse_archive_checksum(
        _read_regular_file(checksum), archive_name=archive.name
    )
    actual = hashlib.sha256()
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(archive, flags)
    except OSError as exc:
        raise ArchiveBoundaryError(f"backup archive is unavailable: {archive}") from exc
    try:
        archive_stat = os.fstat(descriptor)
        if not stat.S_ISREG(archive_stat.st_mode) or archive_stat.st_size > MAX_ARCHIVE_BYTES:
            raise ArchiveBoundaryError("backup archive is not an acceptable regular file")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                actual.update(chunk)
    finally:
        os.close(descriptor)
    if actual.hexdigest() != expected:
        raise ArchiveBoundaryError("backup archive SHA-256 checksum does not match")


def _preflight_archive(path: Path) -> set[str]:
    try:
        with tarfile.open(path, "r:*") as bundle:
            members = bundle.getmembers()
    except (OSError, tarfile.TarError, EOFError) as exc:
        raise ArchiveBoundaryError("backup archive is not a readable tar file") from exc
    if not members or len(members) > MAX_MEMBERS:
        raise ArchiveBoundaryError("backup archive has an invalid member count")
    names: set[str] = set()
    total_size = 0
    for member in members:
        name = member.name
        try:
            normalized = _safe_relative_path(name)
        except ArchiveBoundaryError as exc:
            raise ArchiveBoundaryError("backup archive contains an unsafe path") from exc
        if normalized in names:
            raise ArchiveBoundaryError("backup archive contains a duplicate path")
        if not member.isreg() or member.sparse:
            raise ArchiveBoundaryError("backup archive contains a non-regular member")
        allowed = normalized in {
            CHECKSUM_NAME,
            INVENTORY_ARCHIVE_PATH,
            POLICY_ARCHIVE_PATH,
        } or (
            normalized.startswith("vault/")
            and normalized.endswith(".sops.yaml")
        )
        if not allowed:
            raise ArchiveBoundaryError("backup archive contains an unexpected path")
        names.add(normalized)
        total_size += member.size
        if member.size < 0 or total_size > MAX_ARCHIVE_BYTES:
            raise ArchiveBoundaryError("backup archive exceeds the recovery size limit")
    required = {CHECKSUM_NAME, INVENTORY_ARCHIVE_PATH, POLICY_ARCHIVE_PATH}
    if not required.issubset(names):
        raise ArchiveBoundaryError("backup archive is missing required recovery metadata")
    return names


def _walk_extracted_files(workspace: Path) -> set[str]:
    actual: set[str] = set()

    def traversal_failed(exc: OSError) -> None:
        raise ArchiveBoundaryError("extracted archive traversal failed") from exc

    for directory, directory_names, file_names in os.walk(
        workspace, followlinks=False, onerror=traversal_failed
    ):
        current = Path(directory)
        try:
            current.chmod(0o700)
            for name in directory_names:
                candidate = current / name
                candidate_stat = candidate.lstat()
                if not stat.S_ISDIR(candidate_stat.st_mode) or stat.S_ISLNK(candidate_stat.st_mode):
                    raise ArchiveBoundaryError(
                        "extracted archive contains a non-directory path component"
                    )
            for name in file_names:
                candidate = current / name
                candidate_stat = candidate.lstat()
                if not stat.S_ISREG(candidate_stat.st_mode) or stat.S_ISLNK(candidate_stat.st_mode):
                    raise ArchiveBoundaryError(
                        "extracted archive contains a non-regular file"
                    )
                candidate.chmod(0o600)
                actual.add(candidate.relative_to(workspace).as_posix())
        except OSError as exc:
            raise ArchiveBoundaryError("extracted archive metadata could not be verified") from exc
    return actual


def _parse_internal_checksums(contents: bytes) -> dict[str, str]:
    try:
        text = contents.decode("ascii")
    except UnicodeDecodeError as exc:
        raise ArchiveBoundaryError("internal checksums are not valid ASCII") from exc
    checksums: dict[str, str] = {}
    for line in text.splitlines():
        match = SHA256_LINE.fullmatch(line)
        if match is None:
            raise ArchiveBoundaryError("internal checksums contain a malformed entry")
        name = _safe_relative_path(match.group(2))
        if name == CHECKSUM_NAME or name in checksums:
            raise ArchiveBoundaryError("internal checksums contain an unexpected entry")
        checksums[name] = match.group(1)
    if not checksums:
        raise ArchiveBoundaryError("internal checksums are empty")
    return checksums


def _validate_extracted_archive(workspace: Path, member_names: set[str]) -> Path:
    actual = _walk_extracted_files(workspace)
    if actual != member_names:
        raise ArchiveBoundaryError("extracted archive paths differ from the preflight result")
    inventory_contents = _read_regular_file(workspace / INVENTORY_ARCHIVE_PATH)
    records = _load_inventory_bytes(inventory_contents)
    expected_members = {
        CHECKSUM_NAME,
        INVENTORY_ARCHIVE_PATH,
        POLICY_ARCHIVE_PATH,
        *(f"vault/{record.relative_path}" for record in records),
    }
    if member_names != expected_members:
        raise ArchiveBoundaryError("backup archive does not exactly match its inventory")

    checksums = _parse_internal_checksums(
        _read_regular_file(workspace / CHECKSUM_NAME)
    )
    expected_checksum_paths = expected_members - {CHECKSUM_NAME}
    if set(checksums) != expected_checksum_paths:
        raise ArchiveBoundaryError("internal checksum coverage does not match the inventory")
    for name in sorted(expected_checksum_paths):
        if _sha256(_read_regular_file(workspace / name)) != checksums[name]:
            raise ArchiveBoundaryError("an internal SHA-256 checksum does not match")

    _validate_policy(_read_regular_file(workspace / POLICY_ARCHIVE_PATH))
    for record in records:
        _validate_sops_document(
            _read_regular_file(workspace / "vault" / record.relative_path),
            label=record.relative_path,
        )
    return workspace / "vault"


def _rollback_path(vault: Path) -> Path:
    timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    for _ in range(32):
        candidate = vault.with_name(
            f"{vault.name}.rollback-{timestamp}-{secrets.token_hex(4)}"
        )
        if not candidate.exists() and not candidate.is_symlink():
            return candidate
    raise ArchiveBoundaryError("could not allocate a unique rollback path")


def _rename_exchange(first: Path, second: Path) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise ArchiveBoundaryError("atomic directory exchange is unavailable on this host")
    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        -100,
        os.fsencode(first),
        -100,
        os.fsencode(second),
        2,
    )
    if result != 0:
        error_number = ctypes.get_errno()
        if error_number in {errno.ENOSYS, errno.EINVAL, errno.EXDEV}:
            raise ArchiveBoundaryError("atomic directory exchange is unavailable for the vault")
        raise ArchiveBoundaryError("atomic directory exchange failed")


def _replace_vault_atomically(staged_vault: Path, vault: Path) -> Path | None:
    parent = vault.parent
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if vault.is_symlink():
        raise ArchiveBoundaryError("active vault path must not be a symbolic link")
    if not vault.exists():
        os.rename(staged_vault, vault)
        return None
    if not vault.is_dir():
        raise ArchiveBoundaryError("active vault path is not a directory")

    rollback = _rollback_path(vault)
    _rename_exchange(staged_vault, vault)
    try:
        os.rename(staged_vault, rollback)
    except OSError as exc:
        try:
            _rename_exchange(vault, staged_vault)
        except ArchiveBoundaryError as rollback_exc:
            raise ManualRecoveryRequired(staged_vault) from rollback_exc
        raise ArchiveBoundaryError("vault replacement was reversed because rollback preservation failed") from exc
    return rollback


def _select_tar_binary() -> str:
    test_mode = os.environ.get("GLASSLAB_SECRET_TEST_MODE") == "1"
    candidate = TRUSTED_TAR_PATH
    if test_mode and os.environ.get("TAR_BIN"):
        candidate = Path(os.environ["TAR_BIN"])
    if not candidate.is_absolute():
        raise ArchiveBoundaryError("tar executable path must be absolute")
    try:
        candidate_stat = candidate.lstat()
    except OSError as exc:
        raise ArchiveBoundaryError("trusted tar executable is unavailable") from exc
    if (
        not stat.S_ISREG(candidate_stat.st_mode)
        or stat.S_ISLNK(candidate_stat.st_mode)
        or not os.access(candidate, os.X_OK)
    ):
        raise ArchiveBoundaryError("tar executable is not an executable regular file")
    if not test_mode and (
        candidate_stat.st_uid != 0 or stat.S_IMODE(candidate_stat.st_mode) & 0o022
    ):
        raise ArchiveBoundaryError("trusted tar executable ownership or mode is unsafe")
    return str(candidate)


def restore_backup(
    *,
    archive: Path,
    checksum: Path,
    vault: Path,
    confirmed: bool,
    tar_bin: str,
    signal_state: SignalState | None = None,
) -> Path | None:
    parent = vault.parent
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    workspace = Path(tempfile.mkdtemp(prefix=f".{vault.name}.restore-", dir=parent))
    preserve_workspace = False
    try:
        workspace.chmod(0o700)
        private_archive = workspace / ".archive.tar.gz"
        _copy_open_regular_file(archive, private_archive, maximum=MAX_ARCHIVE_BYTES)
        checksum_contents = _read_regular_file(checksum)
        expected = _parse_archive_checksum(checksum_contents, archive_name=archive.name)
        if _sha256(private_archive.read_bytes()) != expected:
            raise ArchiveBoundaryError("backup archive SHA-256 checksum does not match")
        member_names = _preflight_archive(private_archive)

        command = [
            tar_bin,
            "--extract",
            "--file",
            str(private_archive),
            "--directory",
            str(workspace),
            "--no-same-owner",
            "--no-same-permissions",
            "--no-overwrite-dir",
            "--no-xattrs",
            "--no-acls",
            "--no-selinux",
        ]
        try:
            result = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                check=False,
            )
        except OSError as exc:
            raise ArchiveBoundaryError("could not execute tar for private extraction") from exc
        if result.returncode != 0:
            raise ArchiveBoundaryError("private archive extraction failed")
        private_archive.unlink()
        staged_vault = _validate_extracted_archive(workspace, member_names)
        if not confirmed:
            raise ArchiveBoundaryError(
                "archive validation passed; rerun with --yes to replace the active vault"
            )

        blocked = {signal.SIGINT, signal.SIGTERM}
        previous_mask = None
        if signal_state is not None:
            signal_state.critical_section = True
        try:
            if hasattr(signal, "pthread_sigmask"):
                previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, blocked)
            try:
                rollback = _replace_vault_atomically(staged_vault, vault)
                if signal_state is not None:
                    signal_state.restore_committed = True
            finally:
                if previous_mask is not None:
                    signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
        finally:
            if signal_state is not None:
                signal_state.critical_section = False
        return rollback
    except ManualRecoveryRequired:
        preserve_workspace = True
        raise
    finally:
        if not preserve_workspace:
            shutil.rmtree(workspace, ignore_errors=True)


def _install_signal_handlers(state: SignalState) -> dict[signal.Signals, object]:
    previous: dict[signal.Signals, object] = {}

    def interrupt(signum, _frame):
        if state.critical_section or state.restore_committed:
            state.deferred_signals.append(signum)
            return
        raise OperationInterrupted(f"operation interrupted by signal {signum}")

    for handled in (signal.SIGINT, signal.SIGTERM):
        previous[handled] = signal.getsignal(handled)
        signal.signal(handled, interrupt)
    return previous


def _restore_signal_handlers(previous: dict[signal.Signals, object]) -> None:
    for handled, handler in previous.items():
        signal.signal(handled, handler)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Back up or restore already-encrypted Glasslab SOPS vault documents."
    )
    subparsers = parser.add_subparsers(dest="operation", required=True)

    backup = subparsers.add_parser("backup")
    backup.add_argument("--vault-dir", type=Path, required=True)
    backup.add_argument("--policy", type=Path, required=True)
    backup.add_argument("--output", type=Path, required=True)

    restore = subparsers.add_parser("restore")
    restore.add_argument("--archive", type=Path, required=True)
    restore.add_argument("--checksum", type=Path)
    restore.add_argument("--vault-dir", type=Path, required=True)
    restore.add_argument("--yes", action="store_true")

    verify = subparsers.add_parser("verify-archive")
    verify.add_argument("--archive", type=Path, required=True)
    verify.add_argument("--checksum", type=Path)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    os.umask(0o077)
    arguments = _build_parser().parse_args(argv)
    signal_state = SignalState()
    previous_handlers = _install_signal_handlers(signal_state)
    try:
        if arguments.operation == "backup":
            output = create_backup(
                vault=arguments.vault_dir,
                policy=arguments.policy,
                output=arguments.output,
            )
            print(f"Encrypted-only vault backup written to {output}")
            print(f"Archive checksum written to {output}.sha256")
            return 0
        if arguments.operation == "verify-archive":
            checksum = arguments.checksum or arguments.archive.with_name(
                arguments.archive.name + ".sha256"
            )
            verify_archive_checksum(archive=arguments.archive, checksum=checksum)
            print(f"Archive checksum verified for {arguments.archive}")
            return 0

        checksum = arguments.checksum or arguments.archive.with_name(
            arguments.archive.name + ".sha256"
        )
        rollback = restore_backup(
            archive=arguments.archive,
            checksum=checksum,
            vault=arguments.vault_dir,
            confirmed=arguments.yes,
            tar_bin=_select_tar_binary(),
            signal_state=signal_state,
        )
        print(f"Validated encrypted vault restored to {arguments.vault_dir}")
        if rollback is not None:
            print(f"Previous vault preserved at {rollback}")
        if signal_state.deferred_signals:
            print(
                "Restore commit completed before a termination signal was observed; "
                "the active vault and rollback status above are authoritative.",
                file=sys.stderr,
            )
        return 0
    except OperationInterrupted:
        print("Secret archive operation interrupted; active vault was not partially replaced.", file=sys.stderr)
        return 130
    except ArchiveBoundaryError as exc:
        print(f"Secret archive safety check failed: {exc}", file=sys.stderr)
        return 1
    finally:
        _restore_signal_handlers(previous_handlers)


if __name__ == "__main__":
    raise SystemExit(main())
