"""Deterministic structural contract for problem.md (issue #496).

The compiler prompt treats the guide's sections as conventions; these tests
pin the deterministic replacement: the importer rejects a problem.md whose
required sections are missing or empty, naming the offending section, while
accepting reordered headings and formatting variations.
"""

from __future__ import annotations

from app.problem_schema import PROBLEM_REQUIRED_SECTIONS, problem_section_errors


def _valid_problem() -> str:
    return (
        '# Adult Income Classification\n\n'
        '## Objective\n'
        'Predict whether an adult earns more than 50K a year.\n\n'
        '## Inputs\n'
        '- Dataset: glasslab-dataset://' + 'a' * 64 + '\n\n'
        '## Method and architecture\n'
        '- Logistic regression over the tabular features.\n\n'
        '## Hyperparameter search space (exact)\n'
        '- C: {0.1, 1.0}; solver: lbfgs.\n\n'
        '## Evaluation rubric (exact)\n'
        '- accuracy >= 0.78; stop after the approved matrix completes.\n\n'
        '## Evidence artifacts (required)\n'
        '- metrics.json, report.md, tables/\n'
    )


def test_required_section_list_is_exposed() -> None:
    assert [section.name for section in PROBLEM_REQUIRED_SECTIONS] == [
        'Objective',
        'Inputs',
        'Method and architecture',
        'Hyperparameter search space',
        'Evaluation rubric',
        'Evidence artifacts',
    ]


def test_valid_problem_has_no_section_errors() -> None:
    assert problem_section_errors(_valid_problem()) == []


def test_missing_section_is_rejected_by_name() -> None:
    problem = _valid_problem().replace(
        '## Evaluation rubric (exact)',
        '## Scoring notes',
    )
    errors = problem_section_errors(problem)
    assert len(errors) == 1
    assert 'Evaluation rubric' in errors[0]
    assert 'missing' in errors[0]


def test_empty_section_is_rejected_by_name() -> None:
    problem = _valid_problem().replace(
        'Predict whether an adult earns more than 50K a year.',
        '',
    )
    errors = problem_section_errors(problem)
    assert len(errors) == 1
    assert 'Objective' in errors[0]
    assert 'empty' in errors[0]


def test_subheadings_do_not_leave_a_section_empty() -> None:
    problem = _valid_problem().replace(
        '- accuracy >= 0.78; stop after the approved matrix completes.',
        '### Metric keys\n- accuracy\n\n### Thresholds\n- accuracy >= 0.78',
    )
    assert problem_section_errors(problem) == []


def test_heading_match_is_order_insensitive_and_format_tolerant() -> None:
    problem = (
        '# Adult task\n\n'
        '### evidence artifacts (REQUIRED):\n\n'
        '- metrics.json\n\n'
        '## method and architecture:\n'
        'Logistic regression.\n\n'
        '# objective:\n'
        'Classify adult income.\n\n'
        '## INPUTS\n'
        '- glasslab-dataset://' + 'a' * 64 + '\n\n'
        '###### Hyperparameter search space\n'
        '- C: {0.1, 1.0}\n\n'
        '## Evaluation Rubric\n'
        '- accuracy >= 0.78\n\n'
        'Just an extra prose paragraph.\n'
    )
    assert problem_section_errors(problem) == []
