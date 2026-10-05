"""Pin the deliberately narrow scope of ``mypy-research-store.ini``.

That run exists to prove one structural conformance assertion, not to
type-check the orchestrator codebase. ``app.knowledge_manager`` is imported
through ``app.corpus_rag`` and is now type-checked in full, so the config no
longer carries any ``follow_imports = silent`` override. These guards fail if an
override is reintroduced or if the run starts checking more than the protocol
conformance test.
"""

from __future__ import annotations

import configparser
from pathlib import Path

SERVICE_ROOT = Path(__file__).resolve().parents[1]
INI_PATH = SERVICE_ROOT / "mypy-research-store.ini"
CONFORMANCE_TEST = "tests/test_research_store_protocol.py"
SILENCED_SECTION = "mypy-app.knowledge_manager"


def _parser() -> configparser.ConfigParser:
    parser = configparser.ConfigParser()
    assert parser.read(INI_PATH, encoding="utf-8"), f"could not read {INI_PATH}"
    return parser


def test_no_module_silences_followed_imports() -> None:
    parser = _parser()
    silenced = {
        section
        for section in parser.sections()
        if parser[section].get("follow_imports", "").strip().lower() == "silent"
    }

    assert silenced == set()


def test_files_targets_only_the_protocol_conformance_test() -> None:
    parser = _parser()

    assert parser["mypy"].get("files", "").strip() == CONFORMANCE_TEST


def test_knowledge_manager_is_not_silenced() -> None:
    parser = _parser()

    assert SILENCED_SECTION not in parser.sections()
