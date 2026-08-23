"""Structural conformance checks for durable research stores."""

from app.postgres_store import PostgresStore
from app.research_store import ResearchStore
from app.storage import SqliteStore


def _accepts_research_store(store: ResearchStore) -> ResearchStore:
    """Static checker assertion: both concrete stores satisfy the protocol."""

    return store


# These assignments are intentionally module-level so a static checker checks
# the complete concrete class surfaces without constructing either database.
SQLITE_STORE_CONFORMS: ResearchStore = SqliteStore.__new__(SqliteStore)
POSTGRES_STORE_CONFORMS: ResearchStore = PostgresStore.__new__(PostgresStore)


def test_store_classes_implement_research_store_protocol() -> None:
    assert isinstance(SQLITE_STORE_CONFORMS, ResearchStore)
    assert isinstance(POSTGRES_STORE_CONFORMS, ResearchStore)
    assert _accepts_research_store(SQLITE_STORE_CONFORMS) is SQLITE_STORE_CONFORMS
    assert _accepts_research_store(POSTGRES_STORE_CONFORMS) is POSTGRES_STORE_CONFORMS
