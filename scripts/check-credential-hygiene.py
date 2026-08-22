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
from yaml.nodes import MappingNode, ScalarNode, SequenceNode

@dataclass(frozen=True)
class Finding:
    """A non-revealing credential hygiene violation."""

    path: Path
    line: int
    rule_id: str


EXCLUDED_DIRECTORY_NAMES = frozenset(
    {
        ".git",
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
# The first digest is a harmless fixture sentinel. The second is the historical
# GPU-runner DSN fingerprint, retained only as an irreversible SHA-256 digest.
KNOWN_EXPOSED_VALUE_SHA256 = frozenset(
    {
        "8dc2193cf9ab7a6c99b0d3ea8a5299c1e8b2a39bf5153cbed18280fc7828c7b7",
        "88e065c256085b6ca6be64261cd4bd361f29b5938b2861e859abe54898662341",
    }
)

CRYPT_SHA512_RE = re.compile(
    r"\$6\$(?:rounds=\d+\$)?[./0-9A-Za-z]{1,}\$[./0-9A-Za-z]{20,}"
)
SSHPASS_RE = re.compile(r"\bsshpass\s+(?:\S+\s+)*-p(?:\s|=)")
DSN_RE = re.compile(
    rb"(?:postgres(?:ql)?|mysql|mariadb|mongodb(?:\+[a-z0-9_-]+)?|redis|amqp)://",
    re.IGNORECASE,
)
def _is_excluded_path(relative_path: Path) -> bool:
    parts = tuple(part.lower() for part in relative_path.parts)
    if any(part in EXCLUDED_DIRECTORY_NAMES for part in parts[:-1]):
        return True
    if any("whatsapp" in part for part in parts):
        return True
    return relative_path.suffix.lower() in EXCLUDED_SUFFIXES


def _decode_base64(value: str) -> bytes | None:
    candidate = value.strip().strip("'\"")
    if not candidate or len(candidate) % 4:
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
            for _, value_node in section_node.value:
                if not isinstance(value_node, ScalarNode):
                    continue
                line_number = value_node.start_mark.line + 1
                value = value_node.value
                if section == "stringData":
                    if "change-me" in value.lower():
                        _append_finding(
                            findings,
                            seen,
                            relative_path,
                            line_number,
                            "deployable-change-me-secret",
                        )
                    continue

                decoded_value = _decode_base64(value)
                if decoded_value is None:
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
            if isinstance(document, MappingNode):
                _scan_kubernetes_object(document, relative_path, findings, seen)
    except yaml.YAMLError:
        return


def _scan_file(path: Path, relative_path: Path) -> list[Finding]:
    try:
        contents = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []

    findings: list[Finding] = []
    seen: set[tuple[Path, int, str]] = set()

    for line_number, line in enumerate(contents.splitlines(), start=1):
        if CRYPT_SHA512_RE.search(line):
            _append_finding(findings, seen, relative_path, line_number, "sha512-crypt-verifier")
        if SSHPASS_RE.search(line):
            _append_finding(findings, seen, relative_path, line_number, "sshpass-password")
        if any(_matches_known_digest(candidate) for candidate in _candidate_values(line)):
            _append_finding(findings, seen, relative_path, line_number, "known-exposed-value")

    _scan_secret_documents(contents, relative_path, findings, seen)

    return findings


def scan_tree(root: Path) -> list[Finding]:
    """Scan a repository tree and return non-revealing credential findings."""
    root = root.resolve()
    findings: list[Finding] = []
    for current_root, directory_names, file_names in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current_root)
        directory_names[:] = [
            directory
            for directory in directory_names
            if not _is_excluded_path((current_path / directory).relative_to(root))
        ]
        for file_name in sorted(file_names):
            path = current_path / file_name
            relative_path = path.relative_to(root)
            if _is_excluded_path(relative_path) or path.is_symlink():
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
