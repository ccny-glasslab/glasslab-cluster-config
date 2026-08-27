"""Environment-variable parsing for list-valued operator settings."""

from __future__ import annotations

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
