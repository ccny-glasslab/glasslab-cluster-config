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
    '`comparison` methodology requirement, emit exactly one variant per '
    'required distinct method (at least `minimum_distinct_values` variants), '
    'each with a distinct NON-EMPTY `overrides` object that sets that '
    "requirement's `config_path` to one distinct value; never propose a "
    'single variant with empty `overrides` when a comparison is required. '
    'Write the same distinct values into `base_config` at the same '
    '`config_path`, so the deterministic preflight (which reads base_config) '
    'and the methodology review (which reads the variants) agree.\n'
)


def render_variant_rules_guidance(variant_name_pattern: str) -> str:
    guidance = _VARIANT_RULES_TEMPLATE.format(
        variant_name_pattern=variant_name_pattern
    )
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
