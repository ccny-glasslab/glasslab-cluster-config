"""Environment-variable parsing for list-valued operator settings.

Also locks the ``cluster_execution_mode`` fail-closed contract: it was an
untyped ``str``, so a deployment typo (``workflo-api``) silently selected the
workflow-api executor path and bypassed the ``fake`` branch entirely. It is a
``Literal`` of the values the composition root actually branches on
(``app/main.py``).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import Settings


def test_knowledge_allowlist_roots_parse_from_comma_separated_env(
    monkeypatch,
) -> None:
    monkeypatch.setenv(
        'GLASSLAB_ORCHESTRATOR_KNOWLEDGE_ALLOWLIST_ROOTS',
        '/mnt/artifacts/research-orchestrator/approved-repo/docs,'
        '/mnt/artifacts/research-orchestrator/approved-repo/services/research-orchestrator/evaluation-contracts',
    )
    settings = Settings()
    assert settings.knowledge_allowlist_roots == [
        '/mnt/artifacts/research-orchestrator/approved-repo/docs',
        '/mnt/artifacts/research-orchestrator/approved-repo/services/research-orchestrator/evaluation-contracts',
    ]


def test_knowledge_allowlist_roots_default_targets_containerized_repo() -> None:
    monkeypatch_free = Settings()
    assert isinstance(monkeypatch_free.knowledge_allowlist_roots, list)
    assert all(isinstance(root, str) for root in monkeypatch_free.knowledge_allowlist_roots)


def test_cluster_execution_mode_accepts_workflow_api() -> None:
    settings = Settings(cluster_execution_mode='workflow-api')
    assert settings.cluster_execution_mode == 'workflow-api'


def test_cluster_execution_mode_accepts_fake() -> None:
    settings = Settings(cluster_execution_mode='fake')
    assert settings.cluster_execution_mode == 'fake'


def test_cluster_execution_mode_rejects_typo() -> None:
    with pytest.raises(ValidationError):
        Settings(cluster_execution_mode='workflo-api')


def test_cluster_execution_mode_rejects_unknown_value() -> None:
    with pytest.raises(ValidationError):
        Settings(cluster_execution_mode='kubernetes')


def test_cluster_execution_mode_default_is_workflow_api() -> None:
    assert Settings().cluster_execution_mode == 'workflow-api'
