"""Seal-time validation that methodology requirements are matrix-expressible.

A contract may only declare a methodology requirement the matrix base_config
shape can carry. Rejecting an unsatisfiable requirement at contract-candidate
seal time keeps it out of the trusted catalog; otherwise preflight rejects every
Beaker revision with the same error and the run never converges (issue #457).
These rules mirror ``preflight._config_value``, which walks a dotted config_path
segment by segment from the base_config root.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

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


def _requirement_mode_errors(
    requirement: MethodologyRequirement,
    *,
    require_explicit_scope: bool = True,
) -> list[str]:
    prefix = _requirement_prefix(requirement)
    minimum = requirement.minimum_distinct_values
    maximum = requirement.maximum_distinct_values
    errors: list[str] = []
    if maximum is not None and maximum < minimum:
        errors.append(
            f'{prefix} sets maximum_distinct_values={maximum} below '
            f'minimum_distinct_values={minimum}'
        )
    has_explicit_scope = 'comparison_scope' in requirement.model_fields_set
    if requirement.mode == 'comparison':
        # Agent-proposed candidates must state the scope explicitly so no new
        # ambiguous contract can be sealed. Curated repository-baked contracts
        # are human-reviewed and keep the within_job default, so repository
        # installs relax only this check.
        if require_explicit_scope and not has_explicit_scope:
            errors.append(
                f'{prefix} declares mode `comparison` but omits '
                '`comparison_scope`; a comparison requirement must declare an '
                'explicit scope of `within_job` or `across_jobs`'
            )
        if minimum < 2:
            errors.append(
                f'{prefix} declares mode `comparison` but sets '
                f'minimum_distinct_values={minimum}; a comparison requirement '
                'must require at least 2 distinct values'
            )
    else:
        if has_explicit_scope:
            errors.append(
                f'{prefix} declares mode `decision` but sets '
                '`comparison_scope`; a decision requirement must not carry a '
                'comparison scope'
            )
        if minimum != 1:
            errors.append(
                f'{prefix} declares mode `decision` but sets '
                f'minimum_distinct_values={minimum}; a decision requirement '
                'must pin exactly 1 value'
            )
    return errors


def _across_jobs_count_errors(
    requirements: list[MethodologyRequirement],
) -> list[str]:
    # Multi-axis across-jobs comparison is out of scope, so a contract may make
    # at most one method comparison span separate jobs. The check needs the full
    # requirement list, not one requirement at a time.
    across_jobs = [
        requirement
        for requirement in requirements
        if (
            requirement.mode == 'comparison'
            and requirement.comparison_scope == 'across_jobs'
        )
    ]
    if len(across_jobs) > 1:
        return [
            'methodology_requirements declares more than one across_jobs '
            'comparison requirement; at most one across_jobs comparison is '
            'supported per contract'
        ]
    return []


def _dotted_paths_overlap(first: str, second: str) -> bool:
    # Segment-wise prefix overlap: `a.b` overlaps `a.b` and `a.b.c`, but not
    # `a.bc`.
    first_segments = first.split('.')
    second_segments = second.split('.')
    if len(first_segments) > len(second_segments):
        first_segments, second_segments = second_segments, first_segments
    return second_segments[: len(first_segments)] == first_segments


def _comparison_path_overlap_errors(
    requirements: list[MethodologyRequirement],
) -> list[str]:
    # An across_jobs comparison requires base_config to hold exactly one scalar
    # at its path, while any other comparison using an equal, ancestor, or
    # descendant path requires a full list (or a mapping under that ancestor).
    # The deterministic base_config repair then oscillates and preflight rejects
    # every revision, which is the issue-#457 non-convergence this validator
    # exists to prevent. The guard is limited to overlaps that involve an
    # across_jobs requirement; two within_job comparisons sharing a list path
    # are coherent.
    comparisons = [
        (index, requirement)
        for index, requirement in enumerate(requirements)
        if requirement.mode == 'comparison'
    ]
    errors: list[str] = []
    for index, requirement in comparisons:
        if requirement.comparison_scope != 'across_jobs':
            continue
        for other_index, other in comparisons:
            if other_index == index:
                continue
            if not _dotted_paths_overlap(
                requirement.config_path,
                other.config_path,
            ):
                continue
            errors.append(
                'methodology_requirements across_jobs comparison '
                f'{requirement.requirement_id!r} at '
                f'{requirement.config_path!r} overlaps comparison '
                f'{other.requirement_id!r} at {other.config_path!r}; an '
                'across_jobs comparison path must not be equal to, an ancestor '
                'of, or a descendant of another comparison path'
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
    *,
    require_explicit_scope: bool = True,
) -> list[str]:
    errors: list[str] = []
    seen_ids: set[str] = set()
    for requirement in requirements:
        errors.extend(_requirement_identity_errors(requirement, seen_ids))
        errors.extend(_config_path_errors(requirement))
        errors.extend(
            _requirement_mode_errors(
                requirement,
                require_explicit_scope=require_explicit_scope,
            )
        )
    errors.extend(_across_jobs_count_errors(requirements))
    errors.extend(_comparison_path_overlap_errors(requirements))
    return errors


def validate_across_jobs_comparison_key_schema(
    requirements: list[MethodologyRequirement],
    output_schema: Mapping[str, Any],
) -> list[str]:
    """Require a string ``comparison_key`` property for across_jobs contracts.

    A per-job evaluator cannot see sibling jobs, so it cannot attest that the
    compared jobs share folds, data split, or metric definitions. It emits a
    deterministic ``comparison_key`` digest instead, and the deterministic
    comparison builder requires all jobs to agree. For that to be possible the
    bound contract's expected output schema must declare the property.
    """
    has_across_jobs = any(
        requirement.mode == 'comparison'
        and requirement.comparison_scope == 'across_jobs'
        for requirement in requirements
    )
    if not has_across_jobs:
        return []
    properties = output_schema.get('properties')
    comparison_key = (
        properties.get('comparison_key')
        if isinstance(properties, Mapping)
        else None
    )
    if (
        isinstance(comparison_key, Mapping)
        and comparison_key.get('type') == 'string'
    ):
        return []
    return [
        'expected_output_schema must declare a string `comparison_key` '
        'property for an across_jobs comparison requirement; the evaluator '
        'must emit a deterministic digest over the protocol constants that '
        'must match across the compared jobs'
    ]
