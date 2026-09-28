"""Grounded, offline-first corpus QA for the operator ``/ui`` chat.

:class:`CorpusChatService` answers a natural-language question strictly from
retrieved corpus chunks. The default path is deterministic and network-free:
lexical hybrid retrieval (the dense channel is never engaged here) followed
by extractive synthesis that quotes grounded sentences from the top hits. An
optional duck-typed LLM provider may replace synthesis, but every citation it
returns must resolve to a retrieved hit; any failure -- the shipped providers
expose ``complete_json`` but no ``complete``, malformed JSON, unresolvable
evidence ids, or an empty result -- silently falls back to the extractive
answer.

The emitted :class:`~app.ui_chat.ChatCitation` contract has no URI or path
field: the operator page forbids emitting ``knowledge://`` URIs or filesystem
paths, so only the opaque ``source_id``, the store-resolved ``title``, the
quoted ``excerpt``, its ``verdict``, and the 0-based ``page`` are exposed.

:func:`app.citation_locator.classify_citation` is imported lazily inside the
citation builder: importing it eagerly would pull ``app.knowledge_manager`` ->
``app.research_store`` -> ``app.corpus_rag`` and deadlock package import when
``app.storage`` (which imports ``app.corpus_rag``) is loaded first.
"""

from __future__ import annotations

import json
import re
from typing import Any

from app.corpus_rag.contracts import RetrievedHit
from app.corpus_rag.retrieval import HybridRetriever, RetrievalOptions
from app.redaction import redact_free_text
from app.ui_chat import ChatAnswer, ChatCitation

DEFAULT_TOP_K = 5
DEFAULT_MAX_QUESTION_CHARS = 2000
_EXCERPT_CHARS = 240
_INSUFFICIENT_ANSWER = (
    'No corpus evidence is available to answer that question.'
)
_SENTENCE_END_RE = re.compile(r'[.!?](?:\s|$)')
_LLM_SYSTEM_PROMPT = (
    'You answer strictly from the numbered evidence blocks [E1..En]. Return '
    'STRICT JSON: {"answer": str, "citations": [{"evidence_id": "E<i>", '
    '"excerpt": str}]}. Every excerpt MUST be quoted from its evidence block. '
    'No prose outside JSON.'
)


def _first_sentence(text: str) -> str:
    """Return the whitespace-collapsed first sentence, capped for display."""
    collapsed = ' '.join(text.split())
    match = _SENTENCE_END_RE.search(collapsed)
    sentence = collapsed[: match.end()].strip() if match else collapsed
    return sentence[:_EXCERPT_CHARS]


class CorpusChatService:
    """Answer a question from indexed corpus chunks (offline by default)."""

    def __init__(
        self,
        store: Any,
        *,
        top_k: int = DEFAULT_TOP_K,
        max_question_chars: int = DEFAULT_MAX_QUESTION_CHARS,
        llm: Any = None,
    ) -> None:
        self._store = store
        self._top_k = top_k
        self._max_question_chars = max_question_chars
        self._llm = llm
        self._retriever = HybridRetriever(store)

    def answer(self, question: str) -> ChatAnswer:
        prepared = self._prepare_question(question)
        if not prepared.strip():
            return ChatAnswer(answer=_INSUFFICIENT_ANSWER, insufficient=True)
        result = self._retriever.retrieve(
            prepared,
            options=RetrievalOptions(mode='lexical', k_final=self._top_k),
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
            citations: list[ChatCitation] = []
            for ref in payload.get('citations', []):
                if not isinstance(ref, dict):
                    continue
                hit = by_evidence.get(str(ref.get('evidence_id')))
                if hit is None:
                    continue
                excerpt = str(ref.get('excerpt') or '')[:_EXCERPT_CHARS]
                citations.append(
                    self._citation(hit, excerpt, len(citations) + 1)
                )
            if not citations:
                return None
            return ChatAnswer(
                answer=answer, citations=citations, insufficient=False
            )
        except Exception:  # noqa: BLE001 - any provider failure must fall back
            return None
