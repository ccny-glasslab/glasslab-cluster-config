from __future__ import annotations

import ast
from pathlib import Path
import sys

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "task_fabric"


def _imported_root_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                roots.add(node.module.split(".")[0])
    return roots


def test_task_fabric_imports_only_the_standard_library() -> None:
    sources = sorted(PACKAGE_ROOT.glob("*.py"))
    assert sources, "task_fabric package sources must exist"
    for path in sources:
        for root in _imported_root_modules(path):
            assert root in sys.stdlib_module_names, (
                f"{path.name} imports non-stdlib module {root!r}; the shared "
                "protocol package must stay stdlib-only and must never import "
                "across service trees"
            )
