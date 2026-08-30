"""Tests for the user-facing task-spec feedback formatter."""

from __future__ import annotations

from app.spec_feedback import format_spec_feedback


def test_missing_rubric_gets_actionable_guidance() -> None:
    feedback = format_spec_feedback(
        ["exact evaluation rubric with metric thresholds and stopping conditions"]
    )
    assert "## Evaluation rubric" in feedback
    assert "thresholds" in feedback
    assert "No run was started" in feedback
    assert "task-bundle-guide" in feedback


def test_multiple_issues_are_all_listed() -> None:
    issues = [
        "exact evaluation rubric with metric thresholds and stopping conditions",
        "defined hyperparameter search space",
        "specific backbone architecture requirements",
    ]
    feedback = format_spec_feedback(issues)
    for issue in issues:
        assert issue in feedback


def test_asset_download_issue_advises_dataset_upload() -> None:
    feedback = format_spec_feedback(
        ["task asset download failed for cifar100_dataset: The read operation timed out"]
    )
    assert "/dataset-upload" in feedback
    assert "glasslab-dataset://" in feedback


def test_unknown_issue_gets_generic_guidance() -> None:
    feedback = format_spec_feedback(["some novel requirement not yet known"])
    assert "some novel requirement not yet known" in feedback
    assert "problem.md" in feedback
    assert "No run was started" in feedback


def test_empty_issues_produces_ready_message() -> None:
    feedback = format_spec_feedback([])
    assert "can't start" not in feedback
    assert feedback.strip() == ""