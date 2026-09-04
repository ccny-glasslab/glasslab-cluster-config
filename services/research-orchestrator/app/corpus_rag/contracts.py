"""Frozen schema contract for the corpus-RAG prototype.

These shapes are the shared vocabulary for the corpus-RAG waves (corpus
curation, chunking, embedding, hybrid retrieval, and advisory generation).
They are frozen: later waves depend on these field names and types verbatim,
so a change here is a coordinated contract change, not a local edit.

The module deliberately imports only pydantic. Embedding or indexing runtimes
must stay out of this package so the contract remains importable from any
service, script, or notebook without heavy dependencies.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


# Index/format version stamped on every RAG-derived record so a re-index can
# invalidate superseded chunks and vectors.
RAG_INDEX_VERSION = 'rag-v1'

# Dense embedding width every stored vector must match.
EMBED_DIM = 768

# Token budgets: retrieval context assembly vs. advisory generation prompts.
RETRIEVAL_TOKEN_BUDGET = 3000
ADVISORY_TOKEN_BUDGET = 6000

# Query decomposition and fusion parameters.
MAX_SUBQUERIES = 6
# RRF fusion constant. Benchmark T1 (eval/corpus_rag, run_benchmark_km.py)
# swept k and measured k=60 as the best hybrid config; keep in lockstep with
# the benchmark runner's _RRF_K. Tie-breaks in the fusion sort by chunk_id.
RRF_K = 60

# Per-source cap on chunks promoted into one retrieval answer; keeps a single
# dominant document from crowding out the rest of the context window.
MAX_CHUNKS_PER_SOURCE = 3


class CorpusRecord(BaseModel):
    """A curated research corpus: a named, stable set of knowledge sources."""

    model_config = ConfigDict(extra='forbid')

    corpus_id: str = Field(default_factory=lambda: uuid4().hex)
    slug: str = Field(min_length=1)
    title: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)


class RagDocumentRecord(BaseModel):
    """One extracted document inside a RAG source (1:1 with its source_id)."""

    model_config = ConfigDict(extra='forbid')

    doc_id: str = Field(default_factory=lambda: uuid4().hex)
    source_id: str = Field(min_length=1)
    doc_type: Literal['book', 'paper', 'reference', 'other']
    title: str | None = None
    authors: list[str] = Field(default_factory=list)
    year: int | None = None
    doi_isbn_url: str | None = None
    extraction_version: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class RagSectionRecord(BaseModel):
    """A node of a document's heading tree ('path' is dotted, e.g. '1.2.3')."""

    model_config = ConfigDict(extra='forbid')

    section_id: str = Field(default_factory=lambda: uuid4().hex)
    doc_id: str = Field(min_length=1)
    path: str = Field(min_length=1)
    title: str | None = None
    level: int = Field(ge=1)
    page_start: int | None = None
    page_end: int | None = None
    summary: str | None = None


class RagChunkRecord(BaseModel):
    """A retrievable unit with provenance down to character spans."""

    model_config = ConfigDict(extra='forbid')

    chunk_id: str = Field(default_factory=lambda: uuid4().hex)
    source_id: str = Field(min_length=1)
    doc_id: str | None = None
    section_id: str | None = None
    kind: Literal['evidence_span', 'section_unit']
    chunk_index: int = Field(ge=0)
    text: str = Field(min_length=1)
    digest: str = Field(pattern=r'^[a-f0-9]{64}$')
    token_count: int = Field(ge=1)
    page_start: int | None = None
    page_end: int | None = None
    char_start: int | None = None
    char_end: int | None = None
    section_path: str | None = None
    index_version: str = RAG_INDEX_VERSION


class ChunkVectorMeta(BaseModel):
    """Provenance for one stored embedding vector (no float payload here)."""

    model_config = ConfigDict(extra='forbid')

    chunk_id: str = Field(min_length=1)
    model_id: str = Field(min_length=1)
    revision: str = Field(min_length=1)
    dims: int = Field(ge=1)
    index_version: str = RAG_INDEX_VERSION


class CorpusManifestEntry(BaseModel):
    """One row of a corpus acquisition manifest (what to fetch or skip)."""

    model_config = ConfigDict(extra='forbid')

    id: str = Field(min_length=1)
    title: str
    url: str
    sha256: str | None = None
    license_note: str | None = None
    skip: bool = False
    skip_reason: str | None = None


class BenchmarkQuestion(BaseModel):
    """A retrieval benchmark question graded against known-good sources.

    ``graded_relevance`` maps a chunk-or-section key to 0 (irrelevant),
    1 (supporting), or 2 (directly answers the question).
    """

    model_config = ConfigDict(extra='forbid')

    qid: str = Field(min_length=1)
    text: str = Field(min_length=1)
    expected_source_ids: list[str] = Field(default_factory=list)
    graded_relevance: dict[str, int] = Field(default_factory=dict)
    notes: str | None = None


class QueryPlan(BaseModel):
    """A decomposed retrieval plan: the original query plus subqueries."""

    model_config = ConfigDict(extra='forbid')

    original_query: str = Field(min_length=1)
    subqueries: list[str] = Field(max_length=MAX_SUBQUERIES)
    planner_mode: Literal['heuristic', 'llm']


class RetrievedHit(BaseModel):
    """One scored retrieval result with per-stage scores kept for debugging."""

    model_config = ConfigDict(extra='forbid')

    chunk: RagChunkRecord
    score: float
    stage_scores: dict[str, float] = Field(default_factory=dict)
    dense_rank: int | None = None
    lexical_rank: int | None = None
    rerank_score: float | None = None


class Citation(BaseModel):
    """A claim-to-evidence link resolving to ``knowledge://<source_id>``.

    ``char_span`` is a two-element ``[start, end]`` list (not a tuple) so it
    serializes to JSON without tuple-to-list conversion at every boundary.
    """

    model_config = ConfigDict(extra='forbid')

    chunk_id: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    evidence_uri: str = Field(min_length=1)
    section_path: str | None = None
    page_start: int | None = None
    page_end: int | None = None
    char_span: list[int] | None = None
    quote: str


class MethodCandidate(BaseModel):
    """One candidate method proposed by the advisory, with full rationale.

    Provenance is explicit: ``citations``/``why`` are anchored in retrieved
    corpus spans, while the guidance fields listed in ``catalog_fields`` come
    from the advisor's fixed family catalog — they are templates to consider,
    not claims extracted from the cited text.
    """

    model_config = ConfigDict(extra='forbid')

    method_name: str = Field(min_length=1)
    why: str
    assumptions: list[str] = Field(default_factory=list)
    preprocessing: list[str] = Field(default_factory=list)
    diagnostics: list[str] = Field(default_factory=list)
    metrics: list[str] = Field(default_factory=list)
    failure_modes: list[str] = Field(default_factory=list)
    baselines: list[str] = Field(default_factory=list)
    comparisons: list[str] = Field(default_factory=list)
    citations: list[Citation] = Field(min_length=1)
    confidence: Literal['high', 'medium', 'low'] = 'low'
    # Fields above populated from the family catalog rather than retrieved
    # text (defaulted so PR #220 prototype payloads still validate).
    catalog_fields: list[str] = Field(default_factory=list)
    grounding_note: str = ''


class MethodAdvisory(BaseModel):
    """A grounded methodology advisory derived from an indexed corpus."""

    model_config = ConfigDict(extra='forbid')

    kind: Literal['method_advisory'] = 'method_advisory'
    objective: str = Field(min_length=1)
    corpus_slug: str = Field(min_length=1)
    candidates: list[MethodCandidate]
    # Pairs of candidates whose assumptions conflict; keys are 'a', 'b',
    # and 'topic' so Honeydew can surface contradictions explicitly.
    contradiction_pairs: list[dict[str, str]] = Field(default_factory=list)
    uncertainty_statement: str
    citations_all: list[Citation]
    generated_by: str
    # Production integration fields (all defaulted so prototype payloads
    # from PR #220 artifacts still validate unchanged).
    research_question: str = ''
    subqueries: list[str] = Field(default_factory=list)
    experiment_matrix: list[dict[str, Any]] = Field(default_factory=list)
    insufficient_evidence: bool = False
    insufficiency_reason: str = ''
    retrieval_metadata: dict[str, Any] = Field(default_factory=dict)
    index_version: str = RAG_INDEX_VERSION


class InsufficientCorpusAdvisory(BaseModel):
    """The refusal result when the corpus cannot ground a method advisory."""

    model_config = ConfigDict(extra='forbid')

    kind: Literal['insufficient_corpus'] = 'insufficient_corpus'
    reason: str = Field(min_length=1)
    details: str
    # Production integration fields (defaulted; see MethodAdvisory note).
    research_question: str = ''
    subqueries: list[str] = Field(default_factory=list)
    retrieval_metadata: dict[str, Any] = Field(default_factory=dict)


AdvisoryResult = MethodAdvisory | InsufficientCorpusAdvisory
