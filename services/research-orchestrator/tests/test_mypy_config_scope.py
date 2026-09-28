"""Pin the deliberately narrow scope of ``mypy-research-store.ini``.

That run exists to prove one structural conformance assertion, not to
type-check the orchestrator codebase. Its single ``follow_imports = silent``
override suppresses a different, pre-existing module so the conformance check
can run at all. These guards fail if the override is widened, if the run starts
checking more than the protocol conformance test, or if the override loses the
comment that marks the pre-existing errors as a tracked follow-up.
"""

from __future__ import annotations

import configparser
import re
from pathlib import Path

SERVICE_ROOT = Path(__file__).resolve().parents[1]
INI_PATH = SERVICE_ROOT / "mypy-research-store.ini"
CONFORMANCE_TEST = "tests/test_research_store_protocol.py"
SILENCED_SECTION = "mypy-app.knowledge_manager"
FOLLOW_UP_MARKER_RE = re.compile(r"follow[- ]?up", re.IGNORECASE)


def _parser() -> configparser.ConfigParser:
    parser = configparser.ConfigParser()
    assert parser.read(INI_PATH, encoding="utf-8"), f"could not read {INI_PATH}"
    return parser


def test_only_knowledge_manager_silences_followed_imports() -> None:
    parser = _parser()
    silenced = {
        section
        for section in parser.sections()
        if parser[section].get("follow_imports", "").strip().lower() == "silent"
    }

    assert silenced == {SILENCED_SECTION}


def test_files_targets_only_the_protocol_conformance_test() -> None:
    parser = _parser()

    assert parser["mypy"].get("files", "").strip() == CONFORMANCE_TEST


def test_silent_override_documents_the_tracked_follow_up() -> None:
    lines = INI_PATH.read_text(encoding="utf-8").splitlines()
    section_index = next(
        index
        for index, line in enumerate(lines)
        if line.strip() == f"[{SILENCED_SECTION}]"
    )
    comment_block: list[str] = []
    for line in reversed(lines[:section_index]):
        stripped = line.strip()
        if not stripped:
            continue
        if not stripped.startswith("#"):
            break
        comment_block.append(stripped)
    context = " ".join(reversed(comment_block))

    assert comment_block, f"[{SILENCED_SECTION}] has no explanatory comment"
    assert FOLLOW_UP_MARKER_RE.search(context), (
        f"[{SILENCED_SECTION}] comment must name the tracked follow-up: {context!r}"
    )
