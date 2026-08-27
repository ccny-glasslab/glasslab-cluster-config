"""Frozen corpus-RAG schema contract.

Re-exports the shared shapes from :mod:`.contracts` so callers can import the
whole surface from one package root: ``from app.corpus_rag import CorpusRecord``.
"""

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
    'ChunkVectorMeta',
    'Citation',
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
]
