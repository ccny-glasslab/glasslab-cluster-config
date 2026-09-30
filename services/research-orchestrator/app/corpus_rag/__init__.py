"""Frozen corpus-RAG schema contract.

Re-exports the shared shapes from :mod:`.contracts` so callers can import the
whole surface from one package root: ``from app.corpus_rag import CorpusRecord``.
"""

from app.ui_chat import ChatAnswer, ChatCitation

from .chat import CorpusChatService, build_corpus_chat_service
from .contracts import (
    ADVISORY_TOKEN_BUDGET,
    EMBED_DIM,
    MAX_CHUNKS_PER_SOURCE,
    MAX_SUBQUERIES,
    RETRIEVAL_TOKEN_BUDGET,
    RAG_INDEX_VERSION,
    RRF_K,
    AdvisoryResult,
    BenchmarkQuestion,
    ChunkVectorMeta,
    Citation,
    CorpusManifestEntry,
    CorpusRecord,
    InsufficientCorpusAdvisory,
    MethodAdvisory,
    MethodCandidate,
    QueryPlan,
    RagChunkRecord,
    RagDocumentRecord,
    RagSectionRecord,
    RetrievedHit,
)
from .llm_provider import build_rag_llm_provider

__all__ = [
    'ADVISORY_TOKEN_BUDGET',
    'EMBED_DIM',
    'MAX_CHUNKS_PER_SOURCE',
    'MAX_SUBQUERIES',
    'RETRIEVAL_TOKEN_BUDGET',
    'RAG_INDEX_VERSION',
    'RRF_K',
    'AdvisoryResult',
    'BenchmarkQuestion',
    'ChatAnswer',
    'ChatCitation',
    'ChunkVectorMeta',
    'Citation',
    'CorpusChatService',
    'CorpusManifestEntry',
    'CorpusRecord',
    'InsufficientCorpusAdvisory',
    'MethodAdvisory',
    'MethodCandidate',
    'QueryPlan',
    'RagChunkRecord',
    'RagDocumentRecord',
    'RagSectionRecord',
    'RetrievedHit',
    'build_corpus_chat_service',
    'build_rag_llm_provider',
]
