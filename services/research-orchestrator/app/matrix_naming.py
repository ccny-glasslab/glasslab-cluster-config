"""Single source for experiment-matrix variant naming (issue #501).

The schema validator, the agent-facing prompt guidance, and the template
sanitizer all derive from :data:`VARIANT_NAME_PATTERN`. The pattern literal
exists in exactly one place: :func:`render_variant_rules_guidance` renders the
prompt prose from the constant and raises at import if an edit removes the
interpolation, so prompt drift fails loudly instead of surfacing as a live
matrix rejection (#474).
"""

from __future__ import annotations

import re

VARIANT_NAME_PATTERN = r'^[a-z0-9][a-z0-9_-]{0,62}$'
VARIANT_NAME_MAX_LENGTH = 63
VARIANT_NAME_FALLBACK = 'candidate'
VARIANT_NAME_RE = re.compile(VARIANT_NAME_PATTERN)

_VARIANT_RULES_TEMPLATE = (
    '\nVariant naming and comparison rules: every variant `name` must match '
    'the pattern `{variant_name_pattern}` (lowercase letters, digits, '
    'hyphens, or underscores). When the evaluation contract declares a '
    '`within_job` `comparison` methodology requirement, every compared method '
    'runs inside ONE job: emit exactly one variant named `candidate` with an '
    'EMPTY `overrides` object, and write the complete distinct list of methods '
    "into `base_config` at the requirement's `config_path` (at least "
    '`minimum_distinct_values` distinct values). Replicate the within-job '
    'comparison with at least three matrix seeds.\n'
)

_ACROSS_JOBS_VARIANT_RULES_TEMPLATE = (
    '\nVariant naming and comparison rules: every variant `name` must match '
    'the pattern `{variant_name_pattern}` (lowercase letters, digits, '
    'hyphens, or underscores). When the evaluation contract declares an '
    '`across_jobs` `comparison` methodology requirement, each compared '
    'methodology runs in its OWN Kubernetes job scored by a per-job evaluator: '
    'emit exactly one variant per required distinct method (at least '
    '`minimum_distinct_values` variants), each with a distinct NON-EMPTY '
    "`overrides` object that sets that requirement's `config_path` to one "
    'distinct scalar value. Write a single placeholder scalar into '
    '`base_config` at the same `config_path`; the distinct methods live only '
    'in the variant overrides, so never propose a single variant with empty '
    '`overrides` when an across_jobs comparison is required.\n'
)


def render_variant_rules_guidance(
    variant_name_pattern: str,
    comparison_scope: str = 'within_job',
) -> str:
    templates = {
        'within_job': _VARIANT_RULES_TEMPLATE,
        'across_jobs': _ACROSS_JOBS_VARIANT_RULES_TEMPLATE,
    }
    template = templates.get(comparison_scope)
    if template is None:
        raise ValueError(
            f'unknown comparison_scope {comparison_scope!r}; expected '
            "'within_job' or 'across_jobs'"
        )
    guidance = template.format(variant_name_pattern=variant_name_pattern)
    if variant_name_pattern not in guidance:
        raise RuntimeError(
            'matrix variant guidance no longer interpolates '
            'variant_name_pattern; restore the {variant_name_pattern} '
            'placeholder so the prompt and ExperimentVariant.name cannot '
            'diverge silently'
        )
    return guidance


MATRIX_VARIANT_RULES_GUIDANCE = render_variant_rules_guidance(
    VARIANT_NAME_PATTERN
)


def variant_name_from_value(value: str) -> str:
    """Derive a pattern-conforming variant name from a demonstrated value.

    The sanitized slug is accepted only when it matches the single-source
    pattern; otherwise the fallback (itself pattern-conforming) is returned,
    so a template can never propose an ExperimentVariant the schema rejects.
    """
    slug = re.sub(r'[^a-z0-9_-]+', '-', value.lower()).strip('-_')
    slug = slug[:VARIANT_NAME_MAX_LENGTH]
    if VARIANT_NAME_RE.fullmatch(slug) is None:
        return VARIANT_NAME_FALLBACK
    return slug
