"""Tests for the shared corpus store factory.

Every corpus CLI used to construct ``SqliteStore`` directly, so a production
run against the configured Postgres store was impossible without editing each
script. ``build_store(settings)`` is the single seam they (and this test)
construct their store through. ``PostgresStore`` must be imported lazily: the
sqlite/local path must never pull in psycopg, and ``app.storage`` (which the
factory imports) already imports ``corpus_rag``, so a top-level Postgres
import would also risk a cycle.
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.storage import SqliteStore
from app.store_factory import build_store

SERVICE_DIR = Path(__file__).resolve().parents[1]
FACTORY_PATH = SERVICE_DIR / 'app' / 'store_factory.py'

# The corpus CLIs this lane owns; each must route through the factory instead
# of hard-coding a backend.
CORPUS_CLI_PATHS = (
    SERVICE_DIR / 'scripts' / 'corpus_rag' / 'ingest_corpus.py',
    SERVICE_DIR / 'scripts' / 'corpus_rag' / 'ingest_arxiv.py',
    SERVICE_DIR / 'scripts' / 'corpus_rag' / 'fetch_corpus.py',
    SERVICE_DIR / 'scripts' / 'corpus_rag' / 'build_index.py',
    SERVICE_DIR / 'scripts' / 'build_knowledge_corpus.py',
)


def test_sqlite_backend_uses_corpus_rag_store_path(tmp_path: Path) -> None:
    store_path = tmp_path / 'corpus-rag.db'
    settings = Settings(
        store_backend='sqlite',
        corpus_rag_store_path=str(store_path),
        database_path=str(tmp_path / 'ignored.db'),
    )

    store = build_store(settings)

    assert isinstance(store, SqliteStore)
    assert store.database_path == str(store_path)


def test_sqlite_backend_falls_back_to_database_path(tmp_path: Path) -> None:
    database_path = tmp_path / 'orchestrator.db'
    settings = Settings(
        store_backend='sqlite',
        corpus_rag_store_path=None,
        database_path=str(database_path),
    )

    store = build_store(settings)

    assert isinstance(store, SqliteStore)
    assert store.database_path == str(database_path)


def test_postgres_backend_requires_dsn() -> None:
    # A settings-like object without the pydantic validator (as the tests and
    # scripts sometimes pass) must still fail closed rather than build a
    # half-configured store.
    settings = SimpleNamespace(
        store_backend='postgres',
        store_postgres_dsn=None,
        database_path='/tmp/ignored.db',
        corpus_rag_store_path=None,
    )
    with pytest.raises(ValueError, match='dsn'):
        build_store(settings)


def test_postgres_backend_builds_store_via_lazy_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, str] = {}

    class _FakePostgresStore:
        def __init__(self, dsn: str) -> None:
            captured['dsn'] = dsn

    # Patched before build_store imports it: proves the factory resolves the
    # class at call time (lazy), never at module import.
    monkeypatch.setattr('app.postgres_store.PostgresStore', _FakePostgresStore)
    settings = SimpleNamespace(
        store_backend='postgres',
        store_postgres_dsn='postgresql://glasslab/db',
        database_path='/tmp/ignored.db',
        corpus_rag_store_path=None,
    )

    store = build_store(settings)

    assert isinstance(store, _FakePostgresStore)
    assert captured['dsn'] == 'postgresql://glasslab/db'


def test_postgres_import_stays_lazy_and_out_of_module_toplevel() -> None:
    tree = ast.parse(FACTORY_PATH.read_text(encoding='utf-8'))
    toplevel_modules = {
        node.module
        for node in tree.body
        if isinstance(node, ast.ImportFrom)
    }
    assert 'app.postgres_store' not in toplevel_modules
    # The class is still referenced, just inside the function body.
    assert 'PostgresStore' in FACTORY_PATH.read_text(encoding='utf-8')


def test_corpus_clis_route_through_the_factory() -> None:
    for path in CORPUS_CLI_PATHS:
        assert path.exists(), f'missing corpus CLI {path}'
        source = path.read_text(encoding='utf-8')
        assert 'SqliteStore(' not in source, (
            f'{path} still constructs SqliteStore directly; route it through '
            'app.store_factory.build_store'
        )
        assert 'build_store' in source, f'{path} does not use build_store'
