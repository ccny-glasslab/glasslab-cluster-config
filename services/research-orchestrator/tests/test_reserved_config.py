"""Reserved-flag guard for the corpus config.

``corpus_rag_store_path`` must be consumed (the shared store factory reads it
so a deployment can relocate the corpus store independently of the main
orchestrator database). ``rag_llm_enabled`` is RESERVED: it is declared for a
future remote synthesis lane and deliberately has no reader, so flipping it
must have no effect. These are source-level guards over the app package; they
fail loudly if a later change consumes a reserved flag without landing the
lane that owns it.
"""

from __future__ import annotations

from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1] / 'app'
CONFIG_PATH = APP_DIR / 'config.py'

RESERVED_FLAGS = ('rag_llm_enabled',)


def _app_sources() -> dict[Path, str]:
    return {
        path: path.read_text(encoding='utf-8')
        for path in sorted(APP_DIR.rglob('*.py'))
    }


def test_corpus_rag_store_path_is_consumed() -> None:
    consumers = [
        path.name
        for path, source in _app_sources().items()
        if path != CONFIG_PATH and 'corpus_rag_store_path' in source
    ]
    assert consumers, (
        'corpus_rag_store_path is declared in config.py but no app module '
        'reads it; the shared store factory must consume it'
    )
    assert 'store_factory.py' in consumers


def test_ui_chat_retrieval_mode_is_consumed() -> None:
    consumers = [
        path.name
        for path, source in _app_sources().items()
        if path != CONFIG_PATH and 'ui_chat_retrieval_mode' in source
    ]
    assert consumers, (
        'ui_chat_retrieval_mode is declared in config.py but no app module '
        'reads it; the UI chat factory must consume it'
    )


def test_rag_llm_enabled_has_no_consumer() -> None:
    offenders: dict[str, list[str]] = {}
    for path, source in _app_sources().items():
        if path == CONFIG_PATH:
            continue
        for flag in RESERVED_FLAGS:
            if flag in source:
                offenders.setdefault(flag, []).append(path.name)
    assert offenders == {}, (
        f'reserved flags gained a consumer before their lane landed: {offenders}'
    )
