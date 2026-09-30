"""Grounded corpus-QA chat service tests (offline, deterministic).

The default synthesis path is extractive over lexical retrieval: no network,
no embedding model, no reranker. The optional LLM path is exercised only with
scripted duck-typed providers so the suite never leaves the process. Every
assertion targets observable ``ChatAnswer``/``ChatCitation`` values; the
citation contract is deliberately URI-free (the ``/ui`` page forbids emitting
``knowledge://`` URIs or filesystem paths).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from app.corpus_rag.chat import CorpusChatService
from app.corpus_rag.contracts import RagChunkRecord
from app.corpus_rag.llm_provider import get_llm
from app.schemas import KnowledgeSource, SourceType
from app.storage import SqliteStore
from app.ui_chat import ChatCitation

TITLED_URI = 'repo://docs/resampling.md'
UNTITLED_URI = 'repo://docs/stability.md'
TITLED_TEXT = 'Resampling improves stability of small samples.'
UNTITLED_TEXT = 'Small samples resampling stability diagnostics reveal variance drift.'
ALPHA_TEXT = 'Alpha beta gamma delta epsilon.'


def _source(uri: str, *, title: str | None) -> KnowledgeSource:
    return KnowledgeSource(
        source_type=SourceType.DOCUMENTATION,
        canonical_uri=uri,
        digest=hashlib.sha256(uri.encode()).hexdigest(),
        title=title,
    )


def _chunk(
    source_id: str,
    index: int,
    text: str,
    *,
    page_start: int | None = None,
) -> RagChunkRecord:
    return RagChunkRecord(
        chunk_id=f'{source_id}::c{index}',
        source_id=source_id,
        kind='evidence_span',
        chunk_index=index,
        text=text,
        digest=hashlib.sha256(text.encode()).hexdigest(),
        token_count=max(1, len(text.split())),
        page_start=page_start,
    )


def _seed_store(
    path: Path,
    *,
    titled_text: str = TITLED_TEXT,
    titled_page: int | None = 3,
    include_untitled: bool = True,
    store_type: type[SqliteStore] = SqliteStore,
) -> SqliteStore:
    """Real SQLite store with two synthetic sources and one chunk each."""
    store = store_type(str(path))
    titled = _source(TITLED_URI, title='Resampling Handbook')
    store.save_knowledge_source(titled)
    store.replace_rag_chunks(
        titled.source_id, [_chunk(titled.source_id, 0, titled_text, page_start=titled_page)]
    )
    if include_untitled:
        untitled = _source(UNTITLED_URI, title=None)
        store.save_knowledge_source(untitled)
        store.replace_rag_chunks(
            untitled.source_id, [_chunk(untitled.source_id, 0, UNTITLED_TEXT)]
        )
    return store


class _CapturingStore(SqliteStore):
    """SqliteStore that records every lexical FTS query it receives."""

    def __init__(self, database_path: str) -> None:
        super().__init__(database_path)
        self.fts_queries: list[str] = []

    def search_rag_chunks_fts(
        self,
        query: str,
        *,
        source_ids: list[str] | None = None,
        limit: int = 10,
    ) -> list[dict]:
        self.fts_queries.append(query)
        return super().search_rag_chunks_fts(
            query, source_ids=source_ids, limit=limit
        )


class _ScriptedLlm:
    """Duck-typed ``complete`` provider returning a fixed JSON string."""

    def __init__(self, response: str) -> None:
        self._response = response
        self.calls: list[tuple[str, str]] = []

    def complete(self, *, system: str, user: str) -> str:
        self.calls.append((system, user))
        return self._response

    def complete_json(self, system: str, user: str) -> dict:  # pragma: no cover
        raise AssertionError('complete_json must not be used by the chat path')


class _RaisingLlm:
    def complete(self, *, system: str, user: str) -> str:
        raise RuntimeError('provider exploded')


# --- behavior 1: grounded extractive citations, resolved titles -------------


def test_answer_returns_grounded_citations_with_titles_and_exact_verdict(
    tmp_path: Path,
) -> None:
    store = _seed_store(tmp_path / 'chat.db')
    service = CorpusChatService(store, top_k=5)

    result = service.answer('resampling stability small samples')

    assert result.insufficient is False
    assert result.citations, 'expected grounded citations'
    by_source = {c.source_id: c for c in result.citations}
    titled = next(
        source for source in store.list_knowledge_sources()
        if source.canonical_uri == TITLED_URI
    )
    assert titled.source_id in by_source
    citation = by_source[titled.source_id]
    assert citation.title == 'Resampling Handbook'
    assert citation.verdict == 'exact'
    assert citation.page == 3
    assert citation.excerpt in TITLED_TEXT
    # The untitled source falls back to a safe ordinal, never a URI or path.
    untitled = next(
        source for source in store.list_knowledge_sources()
        if source.canonical_uri == UNTITLED_URI
    )
    fallback = by_source[untitled.source_id]
    ordinal = [c.source_id for c in result.citations].index(untitled.source_id) + 1
    assert fallback.title == f'Source {ordinal}'


# --- behavior 2: empty corpus refuses without leaking URIs/paths ------------


def test_answer_empty_corpus_is_insufficient_without_uris(tmp_path: Path) -> None:
    store = SqliteStore(str(tmp_path / 'empty.db'))
    service = CorpusChatService(store)

    result = service.answer('resampling stability')

    assert result.insufficient is True
    assert result.citations == []
    serialized = result.model_dump_json()
    assert 'knowledge://' not in serialized
    assert '"uri"' not in serialized
    assert '"path"' not in serialized
    assert set(ChatCitation.model_fields) == {
        'source_id',
        'title',
        'excerpt',
        'verdict',
        'page',
    }


# --- behavior 3: LLM citations validated, silent extractive fallback --------


def test_answer_llm_citations_validated_then_silent_extractive_fallback(
    tmp_path: Path,
) -> None:
    store = _seed_store(tmp_path / 'llm.db', include_untitled=False)
    titled = store.list_knowledge_sources()[0]

    validated_llm = _ScriptedLlm(
        '{"answer": "LLM synthesis.", "citations": ['
        '{"evidence_id": "E1", "excerpt": "Resampling improves stability of small samples."},'
        '{"evidence_id": "E99", "excerpt": "fabricated"}]}'
    )
    validated = CorpusChatService(store, llm=validated_llm)
    result = validated.answer('resampling stability')

    assert result.answer == 'LLM synthesis.'
    assert [c.source_id for c in result.citations] == [titled.source_id]
    assert all(c.verdict == 'exact' for c in result.citations)

    # Any provider failure silently falls back to the extractive answer.
    falling_back = CorpusChatService(store, llm=_RaisingLlm())
    fallback = falling_back.answer('resampling stability')
    assert 'Resampling improves stability' in fallback.answer
    assert fallback.insufficient is False
    assert all(c.verdict == 'exact' for c in fallback.citations)

    # The shipped providers expose only complete_json (no complete): the
    # dormant path must not crash and must use the extractive answer.
    dormant = CorpusChatService(store, llm=get_llm())
    dormant_result = dormant.answer('resampling stability')
    assert 'Resampling improves stability' in dormant_result.answer


# --- behavior 4: verdict is exact / fuzzy / none ----------------------------


def test_answer_verdict_exact_fuzzy_none(tmp_path: Path) -> None:
    store = _seed_store(
        tmp_path / 'verdict.db',
        titled_text=ALPHA_TEXT,
        titled_page=None,
        include_untitled=False,
    )
    llm = _ScriptedLlm(
        '{"answer": "Grounded.", "citations": ['
        '{"evidence_id": "E1", "excerpt": "Alpha beta gamma delta epsilon."},'
        '{"evidence_id": "E1", "excerpt": "Alpha, beta gamma delta epsilon."},'
        '{"evidence_id": "E1", "excerpt": "Totally unrelated statement."}]}'
    )
    service = CorpusChatService(store, llm=llm)

    result = service.answer('alpha beta gamma delta epsilon')

    assert [c.verdict for c in result.citations] == ['exact', 'fuzzy', 'none']
    assert [c.page for c in result.citations] == [None, None, None]


# --- behavior 5: question is redacted and length-capped ---------------------


def test_answer_redacts_secret_question_and_caps_length(tmp_path: Path) -> None:
    secret = 'ghp_' + 'a' * 36
    store = _CapturingStore(str(tmp_path / 'redact.db'))
    service = CorpusChatService(store, max_question_chars=50)

    service.answer(f'{secret} ' + 'x' * 3000)

    assert store.fts_queries, 'retrieval must consult the lexical channel'
    joined = ' '.join(store.fts_queries)
    assert secret not in joined
    assert 'a' * 36 not in joined
    assert any('redacted' in query for query in store.fts_queries)
    assert all(len(query) <= 50 for query in store.fts_queries)
    assert 'x' * 100 not in joined


if __name__ == '__main__':  # pragma: no cover
    raise SystemExit(pytest.main([__file__, '-q']))
