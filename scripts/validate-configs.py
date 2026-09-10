#!/usr/bin/env python3
"""Read-only repo hygiene check: parse every YAML and JSON file and report
failures without modifying anything.

Beyond the parse check, this script enforces supply-chain pinning rules:

- kubeadm/ manifests: every ``image:`` value must be pinned to a full
  40-hex git SHA tag or a ``@sha256:<64 hex>`` digest. Short SHAs and
  floating tags (``:latest``, ``:v1``, ``:2.10-alpine``, ...) fail.
- services/*/Dockerfile*: every ``FROM`` base must be digest-pinned.
- .github/workflows/: every ``uses:`` action must be a local path or a
  full 40-hex SHA pin.
- ansible/: image references (keys containing ``image``) must be pinned.

Intentional exceptions: Python ``>=`` comparison operators are not tags,
and test fixtures that deliberately assert rejection (e.g. ``:latest`` in
services/*/tests) are outside the scanned paths. Legacy image tags that
cannot be resolved to a git SHA or registry digest are listed explicitly
in LEGACY_IMAGE_ALLOWLIST.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import sys

import yaml


EXCLUDED_PARTS = {
    '.git',
    '.mypy_cache',
    '.pytest_cache',
    '__pycache__',
    'node_modules',
}

EXCLUDED_SUFFIXES = {
    '.bak',
    '.bak2',
    '.bak3',
}

FULL_SHA_RE = re.compile(r'^[0-9a-f]{40}$')
DIGEST_RE = re.compile(r'^sha256:[0-9a-f]{64}$')

# Legacy/manual image tags that cannot be resolved to a git SHA or a
# registry digest: the images are built and pushed by hand (no CI build
# record) into private GHCR packages, so neither a full-SHA tag nor a
# pullable digest is available from this repo. Kept as explicit exceptions
# so the pinning gate stays enforceable on everything else.
LEGACY_IMAGE_ALLOWLIST = {
    'ghcr.io/ccny-glasslab/glasslab-gpu-experiment-runner:0.1.7-local',
    'ghcr.io/ccny-glasslab/glasslab-assessment-agent:0.1.0',
    'ghcr.io/ccny-glasslab/glasslab-intake-agent:0.1.0',
    'ghcr.io/ccny-glasslab/glasslab-interpretation-agent:0.1.1',
    'ghcr.io/ccny-glasslab/glasslab-research-orchestrator:0.1.0',
    'ghcr.io/ccny-glasslab/glasslab-design-agent:0.1.0',
    'ghcr.io/ccny-glasslab/glasslab-schedule-worker:0.1.0',
    'ghcr.io/ccny-glasslab/glasslab-agent-api:0.1.0',
}


def should_skip(path: Path) -> bool:
    # VCS internals, caches, and vendored dependency trees are never valid
    # repo configs and are excluded from the walk.
    if any(part in EXCLUDED_PARTS for part in path.parts):
        return True
    return any(path.name.endswith(suffix) for suffix in EXCLUDED_SUFFIXES)


def is_pinned_image_ref(ref: str) -> bool:
    """True when an image reference is pinned to a digest or full git SHA.

    A pinned reference is either ``name@sha256:<64 hex>``, a tag that is
    exactly 40 hex characters, or a tag that embeds a 40-hex SHA as a
    dash-separated component (e.g. ``smoke-test-<40hex>`` or
    ``sha-<40hex>-benchmark-gpu``).
    """
    if ref in LEGACY_IMAGE_ALLOWLIST:
        return True
    if '@' in ref:
        return bool(DIGEST_RE.match(ref.split('@', 1)[1]))
    if ':' in ref:
        tag = ref.rsplit(':', 1)[1]
        if FULL_SHA_RE.match(tag):
            return True
        return any(FULL_SHA_RE.match(part) for part in tag.split('-'))
    return False


def is_pinned_action(uses: str) -> bool:
    """True when a workflow ``uses:`` value is a local path or SHA-pinned."""
    if uses.startswith('./'):
        return True
    if uses.startswith('docker://'):
        return is_pinned_image_ref(uses[len('docker://'):])
    if '@' in uses:
        return bool(FULL_SHA_RE.match(uses.rsplit('@', 1)[1]))
    return False


def looks_like_image_ref(value: str) -> bool:
    """Heuristic for ansible values that reference a container image."""
    if value.startswith(('http://', 'https://', '/', './', '../', '{{', '$', 'unix://')):
        return False
    return '/' in value or ':' in value


def walk_key_values(node, key_name: str):
    """Yield every string value stored under ``key_name`` in a YAML tree."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == key_name and isinstance(value, str):
                yield value
            else:
                yield from walk_key_values(value, key_name)
    elif isinstance(node, list):
        for item in node:
            yield from walk_key_values(item, key_name)


def walk_image_key_values(node):
    """Yield (key, value) pairs whose key contains ``image``."""
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(key, str) and 'image' in key.lower():
                yield key, value
            else:
                yield from walk_image_key_values(value)
    elif isinstance(node, list):
        for item in node:
            yield from walk_image_key_values(item)


def check_yaml_pinning(path: Path, docs: list) -> list[str]:
    """Apply the pinning rules that live inside YAML documents."""
    errors: list[str] = []
    rel = path.as_posix()

    if rel.startswith('kubeadm/'):
        for doc in docs:
            for ref in walk_key_values(doc, 'image'):
                if not is_pinned_image_ref(ref):
                    errors.append(f'{rel}: unpinned image: {ref}')
    elif rel.startswith('ansible/'):
        for doc in docs:
            for key, value in walk_image_key_values(doc):
                if isinstance(value, str) and looks_like_image_ref(value):
                    if not is_pinned_image_ref(value):
                        errors.append(f'{rel}: unpinned image reference {key}: {value}')
    elif rel.startswith('.github/workflows/'):
        for doc in docs:
            for uses in walk_key_values(doc, 'uses'):
                if not is_pinned_action(uses):
                    errors.append(f'{rel}: unpinned action: {uses}')

    return errors


def check_dockerfile(path: Path) -> list[str]:
    """Every ``FROM`` base in a service Dockerfile must be digest-pinned.

    The base must carry a well-formed ``@sha256:<64 hex>`` digest; a bare
    tag, a short digest, or a non-hex digest all fail. ``scratch`` and
    ``--platform=`` options are handled.
    """
    errors: list[str] = []
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped.startswith('FROM '):
            continue
        parts = stripped.split()
        base = next((p for p in parts[1:] if not p.startswith('--')), None)
        if base is None or base == 'scratch':
            continue
        digest = base.split('@', 1)[1] if '@' in base else ''
        if not DIGEST_RE.match(digest):
            errors.append(f'{path}: unpinned FROM base: {base}')
    return errors


def main() -> int:
    yaml_errors: list[tuple[str, str]] = []
    json_errors: list[tuple[str, str]] = []
    pin_errors: list[str] = []

    for path in sorted(Path('.').rglob('*')):
        if should_skip(path) or path.is_dir():
            continue

        suffix = path.suffix.lower()
        if suffix in {'.yaml', '.yml'}:
            try:
                with path.open() as fh:
                    # safe_load_all handles multi-document YAML files, not
                    # just single-document ones.
                    docs = list(yaml.safe_load_all(fh))
            except Exception as exc:
                yaml_errors.append((str(path), str(exc)))
                continue
            pin_errors.extend(check_yaml_pinning(path, docs))
        elif suffix == '.json':
            try:
                with path.open() as fh:
                    json.load(fh)
            except Exception as exc:
                json_errors.append((str(path), str(exc)))

    for path in sorted(Path('services').rglob('Dockerfile*')):
        if not should_skip(path):
            pin_errors.extend(check_dockerfile(path))

    if yaml_errors:
        print('YAML errors:')
        for path, exc in yaml_errors:
            print(f'  - {path}: {exc}')
    if json_errors:
        print('JSON errors:')
        for path, exc in json_errors:
            print(f'  - {path}: {exc}')
    if pin_errors:
        print('Pinning errors:')
        for message in pin_errors:
            print(f'  - {message}')

    if yaml_errors or json_errors or pin_errors:
        return 1

    print('All current YAML and JSON files parsed successfully and image references are pinned.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())