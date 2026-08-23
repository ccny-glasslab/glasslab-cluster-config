#!/usr/bin/env python3
"""Reject credential material that must not return to the tracked repository.

Findings intentionally contain locations and symbolic rule identifiers only. The
scanner never includes a matched line, decoded Secret value, or digest input in
its output.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path

import yaml
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

@dataclass(frozen=True)
class Finding:
    """A non-revealing credential hygiene violation."""

    path: Path
    line: int
    rule_id: str


EXCLUDED_DIRECTORY_NAMES = frozenset(
    {
        ".git",
        ".lab-agents",
        ".cache",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".superpowers",
        "__pycache__",
        "node_modules",
        "scan-artifacts",
    }
)
EXCLUDED_SUFFIXES = frozenset(
    {
        ".7z",
        ".avi",
        ".bmp",
        ".gif",
        ".gz",
        ".ico",
        ".jpeg",
        ".jpg",
        ".mkv",
        ".mov",
        ".mp3",
        ".mp4",
        ".pdf",
        ".png",
        ".svg",
        ".tar",
        ".tgz",
        ".tif",
        ".tiff",
        ".webm",
        ".webp",
        ".zip",
    }
)
# The first digest is a harmless fixture sentinel. The remaining fingerprints
# are historical exposed values, retained only as irreversible SHA-256 digests.
KNOWN_EXPOSED_VALUE_SHA256 = frozenset(
    {
        "8dc2193cf9ab7a6c99b0d3ea8a5299c1e8b2a39bf5153cbed18280fc7828c7b7",
        "88e065c256085b6ca6be64261cd4bd361f29b5938b2861e859abe54898662341",
        "cf70a192a840ad93e149a8897417a27cd2698dcc1f12d6108d0f4c2b53798d97",
    }
)
# Fixed-length windows catch a known value even when documentation embeds it in
# punctuation or a user/value example. Length is non-secret policy metadata.
KNOWN_EXPOSED_FIXED_LENGTH_SHA256 = {
    13: frozenset(
        {"cf70a192a840ad93e149a8897417a27cd2698dcc1f12d6108d0f4c2b53798d97"}
    )
}
YAML_SUFFIXES = frozenset({".yaml", ".yml"})
SAFE_REDACTED_VALUES = frozenset({"redacted", "<redacted>", "replace-me"})
CREDENTIAL_KEY_RE = re.compile(
    r"(?:^|_)(?:PASSWORD|PASSWD|TOKEN|SECRET|API_KEY|ACCESS_KEY|DSN|DATABASE_URL)$",
    re.IGNORECASE,
)

CRYPT_SHA512_RE = re.compile(
    r"\$6\$(?:rounds=\d+\$)?[./0-9A-Za-z]{1,}\$[./0-9A-Za-z]{20,}"
)
SSHPASS_RE = re.compile(r"\bsshpass\s+(?:\S+\s+)*-p(?:\s|=)")
DSN_RE = re.compile(
    rb"(?:postgres(?:ql)?|mysql|mariadb|mongodb(?:\+[a-z0-9_-]+)?|redis|amqp)://",
    re.IGNORECASE,
)


class DuplicateYamlKeyError(yaml.YAMLError):
    """A YAML mapping repeats a key and is unsafe to interpret."""

    def __init__(self, line: int):
        self.line = line
        super().__init__("duplicate YAML mapping key")


def _is_excluded_path(relative_path: Path) -> bool:
    parts = tuple(part.lower() for part in relative_path.parts)
    if any(part in EXCLUDED_DIRECTORY_NAMES for part in parts[:-1]):
        return True
    if any("whatsapp" in part for part in parts):
        return True
    return relative_path.suffix.lower() in EXCLUDED_SUFFIXES


def _decode_base64(value: str) -> bytes | None:
    candidate = value.strip().strip("'\"")
    if not candidate:
        return b""
    if len(candidate) % 4:
        return None
    try:
        return base64.b64decode(candidate, validate=True)
    except (binascii.Error, ValueError):
        return None


def _candidate_values(line: str) -> set[str]:
    """Return possible literal values without retaining them beyond this check."""
    candidates = {line.strip().strip("'\"[]{}(),;")}
    for token in re.split(r"\s+", line.strip()):
        token = token.strip("'\"[]{}(),;")
        if not token:
            continue
        candidates.add(token)
        for separator in ("=", ":"):
            if separator in token:
                candidates.add(token.rsplit(separator, 1)[1].strip("'\"[]{}(),;"))
    return {candidate for candidate in candidates if candidate}


def _matches_known_digest(value: str | bytes) -> bool:
    raw_value = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(raw_value).hexdigest() in KNOWN_EXPOSED_VALUE_SHA256


def _contains_fixed_length_known_digest(line: str) -> bool:
    for length, digests in KNOWN_EXPOSED_FIXED_LENGTH_SHA256.items():
        if length <= 0 or len(line) < length:
            continue
        for offset in range(len(line) - length + 1):
            candidate = line[offset : offset + length].encode("utf-8")
            if hashlib.sha256(candidate).hexdigest() in digests:
                return True
    return False


def _append_finding(
    findings: list[Finding], seen: set[tuple[Path, int, str]], path: Path, line: int, rule_id: str
) -> None:
    key = (path, line, rule_id)
    if key not in seen:
        seen.add(key)
        findings.append(Finding(path=path, line=line, rule_id=rule_id))


def _mapping_values(node: MappingNode, name: str):
    for key_node, value_node in node.value:
        if isinstance(key_node, ScalarNode) and key_node.value == name:
            yield value_node


def _mapping_scalar_value(node: MappingNode, name: str) -> str | None:
    for value_node in _mapping_values(node, name):
        if isinstance(value_node, ScalarNode):
            return value_node.value
    return None


def _node_signature(node: Node, active: set[int] | None = None) -> object:
    """Build a structural signature for YAML keys without constructing values."""
    active = active or set()
    identity = id(node)
    if identity in active:
        return ("alias-cycle", identity)
    active.add(identity)
    try:
        if isinstance(node, ScalarNode):
            return ("scalar", node.tag, node.value)
        if isinstance(node, SequenceNode):
            return (
                "sequence",
                node.tag,
                tuple(_node_signature(item, active) for item in node.value),
            )
        if isinstance(node, MappingNode):
            return (
                "mapping",
                node.tag,
                tuple(
                    (_node_signature(key, active), _node_signature(value, active))
                    for key, value in node.value
                ),
            )
        return (type(node).__name__, node.tag)
    finally:
        active.remove(identity)


def _validate_unique_yaml_keys(node: Node, visited: set[int] | None = None) -> None:
    visited = visited or set()
    if id(node) in visited:
        return
    visited.add(id(node))
    if isinstance(node, MappingNode):
        keys: set[object] = set()
        for key_node, value_node in node.value:
            signature = _node_signature(key_node)
            if signature in keys:
                raise DuplicateYamlKeyError(key_node.start_mark.line + 1)
            keys.add(signature)
            _validate_unique_yaml_keys(key_node, visited)
            _validate_unique_yaml_keys(value_node, visited)
    elif isinstance(node, SequenceNode):
        for item in node.value:
            _validate_unique_yaml_keys(item, visited)


def _scan_secret_mapping(
    secret_node: MappingNode,
    relative_path: Path,
    findings: list[Finding],
    seen: set[tuple[Path, int, str]],
) -> None:
    for section in ("data", "stringData"):
        for section_node in _mapping_values(secret_node, section):
            if not isinstance(section_node, MappingNode):
                continue
            for key_node, value_node in section_node.value:
                if not isinstance(key_node, ScalarNode) or not isinstance(value_node, ScalarNode):
                    continue
                line_number = value_node.start_mark.line + 1
                value = value_node.value
                if section == "stringData":
                    normalized = value.strip().lower()
                    if "change-me" in normalized:
                        _append_finding(
                            findings,
                            seen,
                            relative_path,
                            line_number,
                            "deployable-change-me-secret",
                        )
                    elif DSN_RE.search(value.encode("utf-8")):
                        _append_finding(
                            findings,
                            seen,
                            relative_path,
                            line_number,
                            "secret-stringdata-dsn",
                        )
                    elif _matches_known_digest(value):
                        _append_finding(
                            findings,
                            seen,
                            relative_path,
                            line_number,
                            "known-exposed-value",
                        )
                    elif (
                        normalized not in SAFE_REDACTED_VALUES
                        and not value.startswith("ENC[")
                        and CREDENTIAL_KEY_RE.search(key_node.value)
                    ):
                        _append_finding(
                            findings,
                            seen,
                            relative_path,
                            line_number,
                            "secret-stringdata-credential",
                        )
                    continue

                decoded_value = _decode_base64(value)
                if decoded_value is None:
                    _append_finding(
                        findings,
                        seen,
                        relative_path,
                        line_number,
                        "secret-data-invalid-base64",
                    )
                    continue
                if b"change-me" in decoded_value.lower():
                    _append_finding(
                        findings,
                        seen,
                        relative_path,
                        line_number,
                        "deployable-change-me-secret",
                    )
                if DSN_RE.search(decoded_value):
                    _append_finding(
                        findings, seen, relative_path, line_number, "secret-data-dsn"
                    )
                if _matches_known_digest(decoded_value):
                    _append_finding(
                        findings, seen, relative_path, line_number, "known-exposed-value"
                    )


def _scan_kubernetes_object(
    object_node: MappingNode,
    relative_path: Path,
    findings: list[Finding],
    seen: set[tuple[Path, int, str]],
    inherited_kind: str | None = None,
) -> None:
    """Scan a Kubernetes object and nested List items, not arbitrary mappings."""
    kind = _mapping_scalar_value(object_node, "kind") or inherited_kind
    if kind == "Secret":
        _scan_secret_mapping(object_node, relative_path, findings, seen)
        return
    if kind not in {"List", "SecretList"}:
        return

    item_kind = "Secret" if kind == "SecretList" else None
    for items_node in _mapping_values(object_node, "items"):
        if not isinstance(items_node, SequenceNode):
            continue
        for item_node in items_node.value:
            if isinstance(item_node, MappingNode):
                _scan_kubernetes_object(
                    item_node,
                    relative_path,
                    findings,
                    seen,
                    inherited_kind=item_kind,
                )


def _scan_secret_documents(
    contents: str,
    relative_path: Path,
    findings: list[Finding],
    seen: set[tuple[Path, int, str]],
) -> None:
    """Find Secret data in root objects and Kubernetes List items."""
    try:
        for document in yaml.compose_all(contents):
            if document is not None:
                _validate_unique_yaml_keys(document)
            if isinstance(document, MappingNode):
                _scan_kubernetes_object(document, relative_path, findings, seen)
    except DuplicateYamlKeyError as exc:
        _append_finding(
            findings,
            seen,
            relative_path,
            exc.line,
            "scan-error-duplicate-yaml-key",
        )
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        line = mark.line + 1 if mark is not None else 0
        _append_finding(findings, seen, relative_path, line, "scan-error-yaml")


def _scan_file(path: Path, relative_path: Path) -> list[Finding]:
    try:
        contents = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return [Finding(path=relative_path, line=0, rule_id="scan-error-file-read")]

    findings: list[Finding] = []
    seen: set[tuple[Path, int, str]] = set()

    for line_number, line in enumerate(contents.splitlines(), start=1):
        if CRYPT_SHA512_RE.search(line):
            _append_finding(findings, seen, relative_path, line_number, "sha512-crypt-verifier")
        if SSHPASS_RE.search(line):
            _append_finding(findings, seen, relative_path, line_number, "sshpass-password")
        if _contains_fixed_length_known_digest(line) or any(
            _matches_known_digest(candidate) for candidate in _candidate_values(line)
        ):
            _append_finding(findings, seen, relative_path, line_number, "known-exposed-value")

    if relative_path.suffix.lower() in YAML_SUFFIXES or relative_path.name.endswith(
        "-secret.example"
    ):
        _scan_secret_documents(contents, relative_path, findings, seen)

    return findings


def scan_tree(root: Path) -> list[Finding]:
    """Scan a repository tree and return non-revealing credential findings."""
    root = root.resolve()
    findings: list[Finding] = []

    def traversal_failed(exc: OSError) -> None:
        relative_path = Path(".")
        if exc.filename:
            try:
                relative_path = Path(exc.filename).resolve().relative_to(root)
            except (OSError, ValueError):
                relative_path = Path(".")
        findings.append(
            Finding(path=relative_path, line=0, rule_id="scan-error-traversal")
        )

    for current_root, directory_names, file_names in os.walk(
        root,
        topdown=True,
        followlinks=False,
        onerror=traversal_failed,
    ):
        current_path = Path(current_root)
        directory_names[:] = [
            directory
            for directory in directory_names
            if not _is_excluded_path((current_path / directory).relative_to(root))
        ]
        for file_name in sorted(file_names):
            path = current_path / file_name
            relative_path = path.relative_to(root)
            if _is_excluded_path(relative_path):
                continue
            try:
                if path.is_symlink():
                    continue
            except OSError:
                findings.append(
                    Finding(path=relative_path, line=0, rule_id="scan-error-file-stat")
                )
                continue
            findings.extend(_scan_file(path, relative_path))
    return sorted(findings, key=lambda finding: (str(finding.path), finding.line, finding.rule_id))


def main() -> int:
    parser = argparse.ArgumentParser(description="Scan a tree for tracked credential regressions.")
    parser.add_argument("root", nargs="?", default=".", type=Path)
    args = parser.parse_args()
    findings = scan_tree(args.root)
    for finding in findings:
        print(f"{finding.path}:{finding.line}:{finding.rule_id}")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
