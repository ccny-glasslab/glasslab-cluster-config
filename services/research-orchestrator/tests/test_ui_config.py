"""Corpus-RAG storage and read-only UI settings (issues #618/#619).

These lock the code defaults for the new corpus-RAG raw root, the optional
explicit store path, and the UI/LLM feature flags, plus the deployed container
path. A silently flipped default or a relocated deployment path fails review.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
import yaml

from app.config import Settings
from app.main import create_app

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
    assert settings.ui_chat_retrieval_mode == 'lexical'
    assert settings.ui_pdf_enabled is True
    assert settings.ui_upload_enabled is True
    assert settings.rag_llm_enabled is False


def test_configmap_sets_corpus_raw_root_container_path() -> None:
    assert _configmap_data()[CORPUS_RAG_RAW_ROOT_KEY] == CONTAINER_RAW_ROOT


def _flag_client(settings, engine, **overrides) -> TestClient:
    configured = settings.model_copy(
        update={'require_operator_auth': False, **overrides}
    )
    return TestClient(create_app(configured, engine=engine, start_watcher=False))


def test_ui_pdf_routes_absent_when_disabled(orchestrator_bundle) -> None:
    """ui_pdf_enabled=false removes the whole /ui/pdf/** route group."""
    settings, _, _, _, engine = orchestrator_bundle

    with _flag_client(settings, engine, ui_pdf_enabled=False) as client:
        assert client.get('/ui/').status_code == 200
        assert client.get('/ui/pdf/assets/web/viewer.css').status_code == 404
        assert (
            client.get(
                '/ui/pdf/document.pdf', params={'source': 'missing'}
            ).status_code
            == 404
        )
        assert (
            client.get(
                '/ui/pdf/boxes', params={'source': 'missing', 'page': 1}
            ).status_code
            == 404
        )


def test_ui_pdf_routes_present_when_enabled(orchestrator_bundle) -> None:
    """The disabled-route assertion is only meaningful if enabled serves."""
    settings, _, _, _, engine = orchestrator_bundle

    with _flag_client(settings, engine, ui_pdf_enabled=True) as client:
        assert client.get('/ui/pdf/assets/web/viewer.css').status_code == 200


def test_ui_chat_form_absent_when_disabled(orchestrator_bundle) -> None:
    """ui_chat_enabled=false keeps the center column but not the chat form."""
    settings, _, _, _, engine = orchestrator_bundle

    with _flag_client(settings, engine, ui_chat_enabled=False) as client:
        response = client.get('/ui/')

    assert response.status_code == 200
    assert '<h2>Ask the corpus</h2>' in response.text
    assert 'id="ask"' in response.text
    assert 'Corpus chat is not enabled on this deployment.' in response.text
    # The Sources-column upload form is independent of the chat flag, so the
    # assertion is scoped to the ask form rather than the whole document.
    assert 'class="ask-form"' not in response.text
    assert 'name="q"' not in response.text


def test_ui_chat_section_present_when_enabled(orchestrator_bundle) -> None:
    """The disabled assertion is only meaningful if enabled renders the pane."""
    settings, _, _, _, engine = orchestrator_bundle

    with _flag_client(settings, engine, ui_chat_enabled=True) as client:
        response = client.get('/ui/')

    assert response.status_code == 200
    assert '<h2>Ask the corpus</h2>' in response.text
