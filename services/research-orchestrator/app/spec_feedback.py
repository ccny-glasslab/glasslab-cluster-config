"""User-facing feedback for task-spec submissions that cannot start.

Turns preflight blocking issues into actionable guidance: what the spec is
missing, how to fix it, and a pointer to the authoring guide. This is the
surface Mike asked for — a rejected spec explains itself instead of failing
out with a bare error.
"""

from __future__ import annotations

_GUIDE_URL = (
    'https://github.com/ccny-glasslab/glasslab-cluster-config/blob/main/'
    'docs/research-orchestrator-task-bundle-guide.md'
)

_GUIDANCE: list[tuple[str, str]] = [
    (
        'exact evaluation rubric with metric thresholds',
        'Add an `## Evaluation rubric` section to `problem.md` with exact '
        'pass/fail thresholds and stopping conditions (e.g. "pass iff '
        '`accuracy >= 0.78`; stop after the approved matrix completes").',
    ),
    (
        'defined hyperparameter search space',
        'Add an exact `## Hyperparameter search space` section with concrete '
        'values or ranges (e.g. "learning rate in {1e-4, 3e-4}; batch size '
        'in {64, 128}").',
    ),
    (
        'specific backbone architecture requirements',
        'Name the exact model/backbone in the method section (e.g. '
        '"ResNet-18, embedding dim 128").',
    ),
    (
        'triplet loss parameters',
        'State the exact triplet-loss parameters (margin value, sampling '
        'strategy).',
    ),
    (
        'contrastive loss temperature',
        'Specify the supervised-contrastive temperature value (e.g. '
        '`SupCon temperature in {0.07, 0.1}`).',
    ),
    (
        'early stopping criteria',
        'State the early-stopping condition explicitly (e.g. "no '
        'validation-loss improvement for 10 epochs").',
    ),
    (
        'confidence level for effect sizes',
        'State the confidence level (e.g. 0.95) and method for any '
        'effect-size claims.',
    ),
    (
        'paired test methodology',
        'Describe the paired-test method used for comparisons (e.g. paired '
        'bootstrap with 1000 resamples).',
    ),
    (
        'task asset download failed for',
        'The dataset asset could not be downloaded. Upload it via '
        '`/dataset-upload` and reference `glasslab-dataset://<sha256>` in '
        '`problem.md` instead of a remote URL.',
    ),
    (
        'compiled runtime image is not permitted',
        'The compiled runtime image is not in the approved allowlist; the '
        'compiler must select an approved CPU/GPU profile.',
    ),
    (
        'compiled evaluation contract is not installed',
        'The compiled evaluation contract is not installed; this is an '
        'orchestrator configuration issue.',
    ),
    (
        'task archive is unavailable or failed checksum verification',
        'The task archive failed checksum verification; re-upload the '
        'original archive.',
    ),
    (
        'asset URI is not approved',
        'A dataset asset references a URI outside the approved mounts; bind '
        'datasets through `glasslab-dataset://<sha256>`.',
    ),
    (
        'asset is unavailable or failed checksum verification',
        'A bound dataset asset is missing or failed its checksum; re-upload '
        'the dataset.',
    ),
]


def _guidance_for(issue: str) -> str:
    for marker, guidance in _GUIDANCE:
        if marker in issue:
            return guidance
    return (
        'Add the missing requirement to `problem.md` with exact, '
        'unambiguous values (see the authoring guide for the expected '
        'sections).'
    )


def format_spec_feedback(blocking_issues: list[str]) -> str:
    """Format blocking issues into an actionable, user-facing explanation."""
    if not blocking_issues:
        return ''
    lines = [
        'Your task spec compiled but cannot start yet. It is missing:',
        '',
    ]
    for index, issue in enumerate(blocking_issues, start=1):
        lines.append(f'{index}. {issue}')
        lines.append(f'   -> {_guidance_for(issue)}')
        lines.append('')
    lines.extend(
        [
            'No run was started. Fix the spec and resubmit.',
            f'See the task-bundle authoring guide: {_GUIDE_URL}',
        ]
    )
    return '\n'.join(lines)