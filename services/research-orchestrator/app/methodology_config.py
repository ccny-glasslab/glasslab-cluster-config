"""Deterministic repair of contract-required methodology settings.

Issue #457: a small, deterministic model can reject every auto-revision with
the same preflight error because it keeps omitting a contract-required nested
YAML key. The engine closes that gap by guaranteeing the *shape* of the file
named by ``matrix.base_config`` — the dotted keys exist and each holds at least
``minimum_distinct_values`` distinct values — while the agent remains
responsible for the *values*.

The repair is deliberately bounded and idempotent:

* it only touches the required dotted paths and their ancestors;
* existing valid values are preserved and only a shortfall is topped up;
* when the file already conforms it is not rewritten (no reformatting);
* generated values are deterministic ``<leaf>-candidate-N`` placeholders, each
  annotated with a YAML comment so the agent knows to replace it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .preflight import MethodologyRequirement


@dataclass(frozen=True, slots=True)
class MethodologyConfigRepair:
    """Outcome of a bounded methodology-shape repair."""

    created: bool
    changed: bool
    # config_path -> placeholder values inserted for that requirement.
    placeholders: dict[str, tuple[str, ...]]


def repair_methodology_settings(
    *,
    base_config_path: Path,
    requirements: list[MethodologyRequirement],
) -> MethodologyConfigRepair:
    """Ensure every required dotted path exists with enough distinct values.

    Returns a :class:`MethodologyConfigRepair` describing whether the file was
    created or modified and which placeholder values were inserted. The file is
    written only when something actually changed.
    """
    created = not base_config_path.is_file()
    config = {} if created else _load_mapping(base_config_path)
    changed = created
    comments: dict[str, set[str]] = {}
    placeholders: dict[str, tuple[str, ...]] = {}

    for requirement in requirements:
        before = _snapshot(config)
        inserted = _ensure_requirement(config, requirement, comments)
        if inserted or _snapshot(config) != before:
            changed = True
        if inserted:
            placeholders[requirement.config_path] = tuple(inserted)

    if changed:
        base_config_path.parent.mkdir(parents=True, exist_ok=True)
        base_config_path.write_text(
            _render_yaml(config, comments),
            encoding='utf-8',
        )

    return MethodologyConfigRepair(
        created=created,
        changed=changed,
        placeholders=placeholders,
    )


def _load_mapping(path: Path) -> dict[str, Any]:
    # An unreadable or non-object config cannot pass preflight anyway, so the
    # repair starts from an empty mapping rather than failing the revision.
    try:
        parsed = yaml.safe_load(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeError, yaml.YAMLError):
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def _snapshot(config: dict[str, Any]) -> str:
    return yaml.safe_dump(config, sort_keys=False)


def _ensure_requirement(
    config: dict[str, Any],
    requirement: MethodologyRequirement,
    comments: dict[str, set[str]],
) -> list[str]:
    keys = requirement.config_path.split('.')
    node: dict[str, Any] = config
    for key in keys[:-1]:
        child = node.get(key)
        if not isinstance(child, dict):
            child = {}
            node[key] = child
        node = child

    leaf = keys[-1]
    if (
        requirement.mode == 'comparison'
        and requirement.comparison_scope == 'across_jobs'
    ):
        # An across_jobs comparison puts the distinct methods in the variant
        # overrides, so base_config carries exactly one placeholder scalar
        # instead of the within-job value list.
        return _ensure_single_scalar(
            node,
            leaf,
            requirement.config_path,
            comments,
        )
    current = node.get(leaf)
    if isinstance(current, dict):
        # Preflight rejects metadata wrappers outright, so a wrapped object is
        # replaced rather than treated as an existing value.
        existing: list[Any] = []
    elif isinstance(current, list):
        existing = list(current)
    elif current is None:
        existing = []
    else:
        existing = [current]

    distinct = list(dict.fromkeys(str(value) for value in existing))
    shortfall = max(0, requirement.minimum_distinct_values - len(distinct))
    if shortfall == 0 and not isinstance(current, dict):
        return []

    inserted = _placeholder_values(leaf, distinct, shortfall)
    node[leaf] = [*existing, *inserted]
    for value in inserted:
        comments.setdefault(value, set()).add(requirement.config_path)
    return inserted


def _ensure_single_scalar(
    node: dict[str, Any],
    leaf: str,
    config_path: str,
    comments: dict[str, set[str]],
) -> list[str]:
    current = node.get(leaf)
    if current is None or isinstance(current, dict):
        placeholder = f'{leaf}-candidate-1'
        node[leaf] = placeholder
        comments.setdefault(placeholder, set()).add(config_path)
        return [placeholder]
    if isinstance(current, list):
        distinct = [str(value) for value in dict.fromkeys(current)]
        if len(distinct) > 1:
            node[leaf] = current[0]
    return []


def _placeholder_values(
    leaf: str,
    existing: list[str],
    needed: int,
) -> list[str]:
    # Deterministic and collision-free: the same input always yields the same
    # placeholders, and values already present in the list are skipped.
    taken = set(existing)
    generated: list[str] = []
    index = 1
    while len(generated) < needed:
        candidate = f'{leaf}-candidate-{index}'
        index += 1
        if candidate in taken:
            continue
        generated.append(candidate)
    return generated


def _render_yaml(
    config: dict[str, Any],
    comments: dict[str, set[str]],
) -> str:
    dumped = yaml.safe_dump(
        config,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
    )
    lines: list[str] = []
    for line in dumped.split('\n'):
        lines.append(line)
        stripped = line.strip()
        if not stripped.startswith('- '):
            continue
        value = stripped[2:].strip().strip('\'"')
        targets = comments.get(value)
        if not targets:
            continue
        indent = line[: len(line) - len(line.lstrip())]
        paths = ', '.join(sorted(targets))
        lines.append(
            f'{indent}  # placeholder for {paths}: '
            'replace with a meaningful, distinct value'
        )
    return '\n'.join(lines)
