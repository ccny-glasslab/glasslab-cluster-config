"""Grounded, offline-first corpus QA for the operator ``/ui`` chat.

:class:`CorpusChatService` answers a natural-language question strictly from
retrieved corpus chunks. The default path is deterministic and network-free:
lexical hybrid retrieval (the dense channel is never engaged here) followed
by extractive synthesis that quotes grounded sentences from the top hits. An
optional duck-typed LLM provider may replace synthesis, but every citation it
returns must resolve to a retrieved hit; any failure -- the shipped providers
expose ``complete_json`` but no ``complete``, malformed JSON, or unresolvable
evidence ids -- silently falls back to the extractive answer. A well-formed
payload that names no citations is treated as the model's own refusal and is
returned as-is rather than replaced by extractive quoting.

The emitted :class:`~app.ui_chat.ChatCitation` contract has no URI or path
field: the operator page forbids emitting ``knowledge://`` URIs or filesystem
paths, so only the opaque ``source_id``, the store-resolved ``title``, the
quoted ``excerpt``, its ``verdict``, and the 0-based ``page`` are exposed.
The ``/ui`` boundary converts that ``page`` to the 1-based human page number
the PDF viewer and boxes route use.

:func:`app.citation_locator.classify_citation` is imported lazily inside the
citation builder: importing it eagerly would pull ``app.knowledge_manager`` ->
``app.research_store`` -> ``app.corpus_rag`` and deadlock package import when
``app.storage`` (which imports ``app.corpus_rag``) is loaded first.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

from app.corpus_rag.contracts import RetrievedHit
from app.corpus_rag.retrieval import (
    CrossEncoderReranker,
    HybridRetriever,
    Mode,
    RetrievalOptions,
)
from app.redaction import redact_free_text
from app.ui_chat import ChatAnswer, ChatCitation

if TYPE_CHECKING:
    from app.corpus_rag.vector_index import VectorIndex

DEFAULT_TOP_K = 5
DEFAULT_MAX_QUESTION_CHARS = 2000
DEFAULT_RETRIEVAL_MODE: Mode = 'lexical'
_DENSE_MODES = ('dense', 'hybrid', 'hybrid+rerank')
_EXCERPT_CHARS = 240
_INSUFFICIENT_ANSWER = (
    'No corpus evidence is available to answer that question.'
)
_SENTENCE_END_RE = re.compile(r'[.!?](?:\s|$)')
_CITATION_MARKER_RE = re.compile(r'\[(\d{1,3})\]')
_VERDICT_RANK = {'exact': 2, 'fuzzy': 1, 'none': 0}
_LLM_SYSTEM_PROMPT = (
    'You answer strictly from the numbered evidence blocks [E1..En], which '
    'are excerpts from corpus documents. Return STRICT JSON: {"answer": str, '
    '"citations": [{"evidence_id": "E<i>"}]}. Place an inline [n] marker in '
    'the answer immediately after every claim the evidence supports, where n '
    'is the 1-based position of that evidence in your citations array; cite '
    'every block you rely on. No prose outside JSON. Incomplete evidence is '
    'normal: when the blocks concern the question but do not fully answer it, '
    'answer with what they DO support, note briefly what is missing, and cite '
    'the blocks you used. Only when the blocks are unrelated to the question, '
    'return exactly '
    '{"answer": "I could not find anything in the corpus about that.", '
    '"citations": []}; never describe or cite unrelated evidence, and never '
    'invent citations.'
)


def _first_sentence(text: str) -> str:
    """Return the whitespace-collapsed first sentence, capped for display."""
    collapsed = ' '.join(text.split())
    match = _SENTENCE_END_RE.search(collapsed)
    sentence = collapsed[: match.end()].strip() if match else collapsed
    return sentence[:_EXCERPT_CHARS]


def _claim_sentence(text: str, marker_start: int) -> str:
    """The text from the previous sentence boundary up to the marker."""
    prefix = text[:marker_start]
    boundary = 0
    for match in _SENTENCE_END_RE.finditer(prefix):
        boundary = match.end()
    return prefix[boundary:].strip()


class CorpusChatService:
    """Answer a question from indexed corpus chunks (offline by default)."""

    def __init__(
        self,
        store: Any,
        *,
        top_k: int = DEFAULT_TOP_K,
        max_question_chars: int = DEFAULT_MAX_QUESTION_CHARS,
        llm: Any = None,
        retrieval_mode: Mode = DEFAULT_RETRIEVAL_MODE,
        vector_index: VectorIndex | None = None,
        embedding_provider: Any = None,
        reranker: Any = None,
    ) -> None:
        self._store = store
        self._top_k = top_k
        self._max_question_chars = max_question_chars
        self._llm = llm
        # Dense/hybrid retrieval requires both a vector index and a query
        # embedder. When the mode asks for dense but either is missing the
        # service degrades to lexical rather than returning zero hits (the
        # dense channel is fused by rank, so an absent channel is empty).
        requested = retrieval_mode if retrieval_mode in (
            'lexical', 'dense', 'hybrid', 'hybrid+rerank'
        ) else DEFAULT_RETRIEVAL_MODE
        self._retrieval_mode: Mode = (
            'lexical'
            if requested in _DENSE_MODES
            and (vector_index is None or embedding_provider is None)
            else requested
        )
        self._retriever = HybridRetriever(
            store,
            vector_index=vector_index,
            embedding_provider=embedding_provider,
            reranker=reranker,
            model_id=getattr(embedding_provider, 'model_id', 'offline-deterministic'),
        )

    @property
    def retrieval_mode(self) -> Mode:
        """The effective mode after the dense-availability fallback."""
        return self._retrieval_mode

    def answer(self, question: str) -> ChatAnswer:
        prepared = self._prepare_question(question)
        if not prepared.strip():
            return ChatAnswer(answer=_INSUFFICIENT_ANSWER, insufficient=True)
        result = self._retriever.retrieve(
            prepared,
            options=RetrievalOptions(
                mode=self._retrieval_mode, k_final=self._top_k
            ),
        )
        hits = list(result.hits)
        if not hits:
            return ChatAnswer(answer=_INSUFFICIENT_ANSWER, insufficient=True)
        if self._llm is not None:
            llm_answer = self._llm_answer(prepared, hits)
            if llm_answer is not None:
                return llm_answer
        return self._extractive(hits)

    def _prepare_question(self, question: str) -> str:
        # Redact concrete credential formats BEFORE capping so a token split by
        # the cap cannot dodge the format matcher and leak a partial secret.
        return redact_free_text(question)[: self._max_question_chars]

    def _title_for(self, source_id: str, ordinal: int) -> str:
        getter = getattr(self._store, 'get_knowledge_source', None)
        if getter is not None:
            try:
                title = getattr(getter(source_id), 'title', None)
            except Exception:  # noqa: BLE001 - title lookup is best-effort
                title = None
            if title:
                return str(title)
        return f'Source {ordinal}'

    def _citation(
        self, hit: RetrievedHit, excerpt: str, ordinal: int
    ) -> ChatCitation:
        from app.citation_locator import classify_citation

        chunk = hit.chunk
        return ChatCitation(
            source_id=chunk.source_id,
            title=self._title_for(chunk.source_id, ordinal),
            excerpt=excerpt,
            verdict=classify_citation(excerpt, chunk.text),
            page=chunk.page_start,
        )

    def _extractive(self, hits: list[RetrievedHit]) -> ChatAnswer:
        citations = [
            self._citation(hit, _first_sentence(hit.chunk.text), ordinal)
            for ordinal, hit in enumerate(hits, start=1)
        ]
        answer = ' '.join(
            f'[{ordinal}] "{citation.excerpt}"'
            for ordinal, citation in enumerate(citations, start=1)
        )
        return ChatAnswer(answer=answer, citations=citations, insufficient=False)

    def _llm_answer(
        self, question: str, hits: list[RetrievedHit]
    ) -> ChatAnswer | None:
        try:
            by_evidence = {
                f'E{index}': hit for index, hit in enumerate(hits, start=1)
            }
            blocks = '\n\n'.join(
                f'[E{index}]\nsource_id={hit.chunk.source_id}\n'
                f'{hit.chunk.text[:_EXCERPT_CHARS]}'
                for index, hit in enumerate(hits, start=1)
            )
            raw = self._llm.complete(
                system=_LLM_SYSTEM_PROMPT,
                user=f'Question: {question}\n\nEvidence blocks:\n{blocks}',
            )
            payload = json.loads(
                str(raw).strip().removeprefix('```json').removesuffix('```').strip()
            )
            if not isinstance(payload, dict):
                return None
            answer = str(payload.get('answer') or '').strip()
            if not answer:
                return None
            refs = payload.get('citations', [])
            if not refs:
                # No declared citations is the model's deliberate refusal.
                return ChatAnswer(
                    answer=answer, citations=[], insufficient=True
                )
            resolved = self._resolve_citations(answer, refs, by_evidence)
            if resolved is None:
                # A substantive answer with no marker that resolves to a
                # retrieved hit must never be shown uncited: fall back.
                return None
            answer, citations = resolved
            return ChatAnswer(
                answer=answer, citations=citations, insufficient=False
            )
        except Exception:  # noqa: BLE001 - any provider failure must fall back
            return None

    def _resolve_citations(
        self,
        answer: str,
        refs: Any,
        by_evidence: dict[str, RetrievedHit],
    ) -> tuple[str, list[ChatCitation]] | None:
        """Derive citations and renumber markers from the answer's markers.

        The model's ``citations`` array is only an ordinal address book: a
        marker ``[n]`` selects ``refs[n-1]``, whose ``evidence_id`` resolves to
        a retrieved hit. Evidence never referenced by a marker is dropped; the
        cited evidence is sorted ascending and the markers are rewritten to
        those ordinals. The excerpt and verdict are server-owned. Returns
        ``None`` when no marker resolves.
        """
        from app.citation_locator import classify_citation

        occurrences: list[tuple[int, int, int | None]] = []
        for match in _CITATION_MARKER_RE.finditer(answer):
            position = int(match.group(1))
            index: int | None = None
            if 1 <= position <= len(refs) and isinstance(
                refs[position - 1], dict
            ):
                evidence_id = str(refs[position - 1].get('evidence_id') or '')
                if evidence_id in by_evidence:
                    index = int(evidence_id[1:])
            occurrences.append((match.start(), match.end(), index))

        cited_indices = sorted({
            index for _, _, index in occurrences if index is not None
        })
        if not cited_indices:
            return None
        ordinals = {
            index: ordinal
            for ordinal, index in enumerate(cited_indices, start=1)
        }

        verdicts = {index: 'none' for index in cited_indices}
        for start, _, index in occurrences:
            if index is None:
                continue
            hit = by_evidence[f'E{index}']
            claim = _claim_sentence(answer, start) or _first_sentence(
                hit.chunk.text
            )
            verdict = classify_citation(claim, hit.chunk.text)
            if _VERDICT_RANK[verdict] > _VERDICT_RANK[verdicts[index]]:
                verdicts[index] = verdict

        citations = [
            self._citation(
                by_evidence[f'E{index}'],
                _first_sentence(by_evidence[f'E{index}'].chunk.text),
                ordinals[index],
            ).model_copy(update={'verdict': verdicts[index]})
            for index in cited_indices
        ]

        pieces: list[str] = []
        cursor = 0
        for start, end, index in occurrences:
            pieces.append(answer[cursor:start])
            if index is not None:
                pieces.append(f'[{ordinals[index]}]')
            cursor = end
        pieces.append(answer[cursor:])
        return ''.join(pieces), citations


def _effective_retrieval_mode(mode: Mode, rerank_enabled: bool) -> Mode:
    """Resolve the configured mode plus the rerank flag.

    The flag only acts in a hybrid mode: it upgrades ``hybrid`` to
    ``hybrid+rerank``, and a configured ``hybrid+rerank`` degrades back to
    ``hybrid`` when the flag is off. ``lexical``/``dense`` are never upgraded.
    """
    if mode in ('hybrid', 'hybrid+rerank'):
        return 'hybrid+rerank' if rerank_enabled else 'hybrid'
    return mode


def build_corpus_chat_service(
    store: Any,
    settings: Any,
    *,
    top_k: int = DEFAULT_TOP_K,
    llm: Any = None,
) -> CorpusChatService:
    """Build the ``/ui`` chat service under the configured retrieval mode.

    ``lexical`` (the default) needs no embedding backend. ``dense``/``hybrid``
    build the provider and vector index; any failure degrades to lexical, so
    app startup never depends on the dense lane being ready. The reranker is
    built only for an effective ``hybrid+rerank`` mode and only after the
    dense index is ready, so enabling it never adds a startup dependency.
    """
    mode: Mode = getattr(
        settings, 'ui_chat_retrieval_mode', DEFAULT_RETRIEVAL_MODE
    )
    effective = _effective_retrieval_mode(
        mode, bool(getattr(settings, 'ui_chat_rerank_enabled', False))
    )
    vector_index: VectorIndex | None = None
    embedding_provider: Any = None
    reranker: Any = None
    if effective in _DENSE_MODES:
        try:
            from app.knowledge_dense import create_embedding_provider

            from app.corpus_rag.dense import (
                open_rag_vector_index,
                rag_dense_ready,
            )

            provider = create_embedding_provider(
                settings.knowledge_embedding_model,
                revision=settings.knowledge_embedding_revision,
            )
            if rag_dense_ready(store, provider):
                vector_index = open_rag_vector_index(
                    store, provider, pg_dsn=settings.knowledge_dense_pg_dsn
                )
                embedding_provider = provider
                if effective == 'hybrid+rerank':
                    reranker = CrossEncoderReranker()
        except Exception:  # noqa: BLE001 - dense is additive
            vector_index = None
            embedding_provider = None
            reranker = None
    return CorpusChatService(
        store,
        top_k=top_k,
        llm=llm,
        retrieval_mode=effective,
        vector_index=vector_index,
        embedding_provider=embedding_provider,
        reranker=reranker,
    )
