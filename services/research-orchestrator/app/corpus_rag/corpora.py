"""Corpus curation service over the knowledge store.

A corpus is a named, stable set of knowledge sources (``CorpusRecord`` plus
``rag_corpus_sources`` membership rows). ``CorpusService`` is the thin,
store-backed facade the rest of the corpus-RAG waves program against.
"""

from __future__ import annotations

from typing import Any

from app.corpus_rag.contracts import CorpusRecord


class CorpusService:
    """Create, resolve, and describe corpora; list their member sources."""

    def __init__(self, store: Any) -> None:
        self._store = store

    def ensure_corpus(self, slug: str, title: str | None = None) -> CorpusRecord:
        """Return the existing corpus for ``slug``, creating it if absent."""
        corpus = self._store.get_corpus(slug)
        if corpus is not None:
            return corpus
        return self._store.create_corpus(CorpusRecord(slug=slug, title=title))

    def member_source_ids(self, slug: str) -> list[str]:
        """Source ids in the corpus; empty list for an unknown slug."""
        corpus = self._store.get_corpus(slug)
        if corpus is None:
            return []
        return self._store.list_corpus_sources(corpus.corpus_id)

    def describe(self, slug: str) -> dict[str, Any]:
        """Compact summary: slug, resolved corpus_id (or None), n_sources."""
        corpus = self._store.get_corpus(slug)
        members = self.member_source_ids(slug)
        return {
            'slug': slug,
            'corpus_id': corpus.corpus_id if corpus is not None else None,
            'n_sources': len(members),
        }
