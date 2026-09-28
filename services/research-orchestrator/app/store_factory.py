"""Single construction seam for the corpus/knowledge store backends.

The corpus CLIs historically constructed ``SqliteStore`` directly, so the
configured Postgres store was unreachable from the ingestion jobs. Routing
every CLI through :func:`build_store` keeps the backend decision in one place
and mirrors the composition root in ``app/main.py``: ``store_backend`` selects
``PostgresStore`` with ``store_postgres_dsn``, otherwise ``SqliteStore``.

``PostgresStore`` is imported lazily (inside the postgres branch) so a
sqlite-only run never pulls psycopg into memory, and so this module can import
``app.storage`` -- which itself imports ``corpus_rag`` -- without a cycle.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.storage import SqliteStore

if TYPE_CHECKING:  # pragma: no cover - typing only, keeps the import lazy
    from app.config import Settings
    from app.postgres_store import PostgresStore


def build_store(settings: Settings) -> SqliteStore | PostgresStore:
    """Return the store selected by ``settings.store_backend``.

    ``sqlite`` uses ``corpus_rag_store_path`` when set (the corpus-specific
    location) and otherwise falls back to the shared ``database_path``. The
    ``postgres`` branch reads ``store_postgres_dsn`` exactly as ``app/main.py``
    does; a missing DSN fails closed here so a settings-like object that never
    ran the pydantic validator cannot build a half-configured store.
    """
    if settings.store_backend == 'postgres':
        dsn = (settings.store_postgres_dsn or '').strip()
        if not dsn:
            raise ValueError(
                'postgres store backend requires a non-empty store_postgres_dsn'
            )
        from app.postgres_store import PostgresStore

        return PostgresStore(dsn)
    path = settings.corpus_rag_store_path or settings.database_path
    return SqliteStore(path)
