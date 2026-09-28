"""Corpus-RAG storage and read-only UI settings (issues #618/#619).

These lock the code defaults for the new corpus-RAG raw root, the optional
explicit store path, and the UI/LLM feature flags, plus the deployed container
path. A silently flipped default or a relocated deployment path fails review.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from app.config import Settings

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
CONFIGMAP_PATH = (
    REPOSITORY_ROOT
    / 'kubeadm'
    / 'glasslab-v2'
    / 'research-orchestrator'
    / '10-configmap.yaml'
)

CORPUS_RAG_RAW_ROOT_KEY = 'GLASSLAB_ORCHESTRATOR_CORPUS_RAG_RAW_ROOT'
CONTAINER_RAW_ROOT = '/mnt/artifacts/research-orchestrator/rag/raw'


def _configmap_data() -> dict[str, str]:
    document = next(
        item
        for item in yaml.safe_load_all(
            CONFIGMAP_PATH.read_text(encoding='utf-8')
        )
        if item
    )
    return {str(key): str(value) for key, value in document['data'].items()}


def test_corpus_raw_root_defaults_to_tmp() -> None:
    assert Settings().corpus_rag_raw_root == (
        '/tmp/glasslab-research-orchestrator/rag/raw'
    )


def test_corpus_rag_store_path_defaults_none() -> None:
    assert Settings().corpus_rag_store_path is None


def test_ui_and_rag_flags_default() -> None:
    settings = Settings()
    assert settings.ui_chat_enabled is True
    assert settings.ui_chat_dense is False
    assert settings.ui_pdf_enabled is True
    assert settings.rag_llm_enabled is False


def test_configmap_sets_corpus_raw_root_container_path() -> None:
    assert _configmap_data()[CORPUS_RAG_RAW_ROOT_KEY] == CONTAINER_RAW_ROOT
