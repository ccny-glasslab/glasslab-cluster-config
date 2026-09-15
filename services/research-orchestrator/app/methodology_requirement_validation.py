"""Seal-time validation that methodology requirements are matrix-expressible.

A contract may only declare a methodology requirement the matrix base_config
shape can carry. Rejecting an unsatisfiable requirement at contract-candidate
seal time keeps it out of the trusted catalog; otherwise preflight rejects every
Beaker revision with the same error and the run never converges (issue #457).
These rules mirror ``preflight._config_value``, which walks a dotted config_path
segment by segment from the base_config root.
"""

from __future__ import annotations

from pathlib import Path

from .preflight import EXPERIMENT_DIMENSIONS_ROOT, MethodologyRequirement

_ALLOWED_DIMENSION_ROOTS = frozenset({EXPERIMENT_DIMENSIONS_ROOT})


def _looks_like_filesystem_path(config_path: str) -> bool:
    # A value containing a directory separator and a file extension is a
    # filesystem path, not a key path (issue #198).
    return '/' in config_path and Path(config_path).suffix != ''


def _requirement_prefix(requirement: MethodologyRequirement) -> str:
    return (
        f'methodology_requirements requirement_id '
        f'{requirement.requirement_id!r} config_path '
        f'{requirement.config_path!r}'
    )


def _config_path_errors(requirement: MethodologyRequirement) -> list[str]:
    path = requirement.config_path
    prefix = _requirement_prefix(requirement)
    if _looks_like_filesystem_path(path):
        return [
            f'{prefix} must be a dotted key path into the matrix.base_config '
            'YAML (for example "experiment_dimensions.model"), not a file '
            'path; a key path cannot combine a directory separator with a '
            'file extension'
        ]
    if (
        '/' in path
        or '\\' in path
        or path.startswith('.')
        or path.endswith('.')
    ):
        return [
            f'{prefix} must be a dotted key path into the matrix.base_config '
            'YAML with non-empty segments (for example '
            '"experiment_dimensions.model"); it cannot be an absolute or '
            'filesystem path, contain directory separators, or start or end '
            'with a separator'
        ]
    segments = path.split('.')
    if any(not segment.strip() for segment in segments):
        return [
            f'{prefix} must be a dotted key path into the matrix.base_config '
            'YAML with non-empty segments and no ".." traversal (for example '
            '"experiment_dimensions.model")'
        ]
    root = segments[0]
    if root not in _ALLOWED_DIMENSION_ROOTS:
        return [
            f'{prefix} is rooted at {root!r}, which is not a permitted '
            'experiment-dimensions namespace; config_path must be rooted at '
            f'`{EXPERIMENT_DIMENSIONS_ROOT}` (for example '
            '"experiment_dimensions.model")'
        ]
    if len(segments) < 2:
        return [
            f'{prefix} must address a key beneath the '
            f'`{EXPERIMENT_DIMENSIONS_ROOT}` root (for example '
            '"experiment_dimensions.model"), not the root mapping itself'
        ]
    return []


def _requirement_mode_errors(requirement: MethodologyRequirement) -> list[str]:
    prefix = _requirement_prefix(requirement)
    minimum = requirement.minimum_distinct_values
    maximum = requirement.maximum_distinct_values
    errors: list[str] = []
    if maximum is not None and maximum < minimum:
        errors.append(
            f'{prefix} sets maximum_distinct_values={maximum} below '
            f'minimum_distinct_values={minimum}'
        )
    if requirement.mode == 'comparison':
        if minimum < 2:
            errors.append(
                f'{prefix} declares mode `comparison` but sets '
                f'minimum_distinct_values={minimum}; a comparison requirement '
                'must require at least 2 distinct values'
            )
    elif minimum != 1:
        errors.append(
            f'{prefix} declares mode `decision` but sets '
            f'minimum_distinct_values={minimum}; a decision requirement must '
            'pin exactly 1 value'
        )
    return errors


def _requirement_identity_errors(
    requirement: MethodologyRequirement,
    seen_ids: set[str],
) -> list[str]:
    errors: list[str] = []
    requirement_id = requirement.requirement_id
    if not requirement_id.strip():
        errors.append(
            'methodology_requirements require a non-empty requirement_id'
        )
    elif requirement_id in seen_ids:
        errors.append(
            'methodology_requirements contains duplicate requirement_id '
            f'{requirement_id!r}; each requirement_id must be unique per '
            'contract'
        )
    else:
        seen_ids.add(requirement_id)
    if not requirement.description.strip():
        errors.append(
            f'{_requirement_prefix(requirement)} must have a non-empty '
            'description'
        )
    return errors


def validate_methodology_requirements(
    requirements: list[MethodologyRequirement],
) -> list[str]:
    errors: list[str] = []
    seen_ids: set[str] = set()
    for requirement in requirements:
        errors.extend(_requirement_identity_errors(requirement, seen_ids))
        errors.extend(_config_path_errors(requirement))
        errors.extend(_requirement_mode_errors(requirement))
    return errors
