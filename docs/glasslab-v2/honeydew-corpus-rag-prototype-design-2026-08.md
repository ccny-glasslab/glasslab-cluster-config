# Honeydew Curated-Corpus RAG Prototype Design

Date: 2026-08-23 · Branch: `feat/honeydew-corpus-rag-20260823-225251` (from `testing`)
Status: PROTOTYPE ONLY. Not deployed. Runs #98/#100 untouched. No active-model change,
no cluster change, no production schema/data change (dev Postgres = throwaway container).

## 1. Current architecture (what we build on)

The research-orchestrator already owns a working knowledge subsystem
(`services/research-orchestrator/app/knowledge_manager.py`, 839 lines):

- **Models** (`app/schemas.py:633-766`): `KnowledgeSource` (content-addressed:
  `(digest=sha256(bytes), canonical_uri)` unique identity; `source_type` enum;
  `run_scope` NULL=globally approved; `access_policy`; `index_version='v1'`;
  `metadata` dict; `parent_source_id`), `KnowledgeChunk` (chunk_id, chunk_index,
  text, per-chunk digest, token_count), `ContextPacket` (per-turn durable record:
  run/agent/turn_kind/query/ranked_sources/exact_text_supplied/token_budget).
- **Evidence URIs**: `knowledge://<source_id>`, `knowledge://context:<packet_id>`
  are validated citation schemes (`schemas.py:95-106`).
- **Storage**: `ResearchStore` protocol (`research_store.py:76-91`) with two
  interchangeable backends — SQLite (`storage.py`: FTS5 `knowledge_chunks_fts`,
  BM25 search `search_knowledge_chunks` :1325-1384, OR-of-quoted-terms ≤24 terms)
  and PostgreSQL (`postgres_store.py`: GIN `to_tsvector('simple', …)`,
  `websearch_to_tsquery` + `ts_rank_cd` :359-373). Additive `CREATE TABLE IF NOT
  EXISTS` startup DDL; no migration framework.
- **Scoping**: `_default_source_types(agent, turn_kind)` computed server-side
  pre-query (:445-481); callers cannot widen scope; turn-kind narrowing
  (verification/final_report → evidence-only types; protocol_draft drops
  implementation_file).
- **Safety**: fail-closed secret rejection (`SECRET_PATTERNS` :54-72,
  `SECRET_PATH_PATTERNS` :74-84; reject-only; non-UTF8 refused); ingestion path
  allowlist (:743-756); retrieved text framed as untrusted data with sanitized
  delimiters (:700-737); operator-only HTTP ingestion.
- **Budgets**: rank → diversify (max 3 chunks/source) → token budget (whole-entry
  admission, default 4000 tokens); over-fetch 4×.
- **Engine wiring**: per-turn retrieval fail-open (`engine.py:656-687`), packet
  persisted + `agent.context_retrieved` event, prompt injection with
  `agent.context_attached` event (:822-844).
- **Quality harness**: `app/knowledge_fixture.py` + `tests/test_knowledge_quality.py`
  rank-regression fixture without live models.
- **Existing-but-unused pgvector**: `workflow-api/app/persistence.py:1051-1079`
  creates `vector_index_items` (`CREATE EXTENSION vector`) — provisioned, zero
  consumers; platform docs commit to "semantic/vector indexes live in Postgres
  through pgvector".

## 2. Deficiencies for textbook/paper-scale expert retrieval

| # | Deficiency | Consequence |
|---|---|---|
| D1 | Fixed char-window chunking (1500c/150c) destroys document structure | No page/section provenance → citations cannot locate passages in a book |
| D2 | Lexical-only retrieval | Vocabulary mismatch: "unknown cluster geometry" never matches "clusterability" prose |
| D3 | No corpus/notebook abstraction | Honeydew cannot be scoped to an operator-curated library ("statistical-learning" vs "clustering"); all approved sources compete |
| D4 | Single granularity | Broad methodological questions retrieve formula fragments; no section/chapter conceptual units or summaries |
| D5 | No dense channel, no reranking, no neighborhood expansion | A hit that states a formula returns without its assumptions/failure-mode paragraphs |
| D6 | No query analysis | Multi-faceted Honeydew questions collapse into one bag-of-terms OR query |
| D7 | `index_version` carries no model identity | No reproducible embedding lineage |
| D8 | No advisory artifact | Output is raw evidence lines, not a structured method recommendation |

## 3. Proposed prototype

New package `services/research-orchestrator/app/corpus_rag/` (library within the
service; no new microservice). Parallel additive tables keyed to existing
`knowledge_sources.source_id` — the live knowledge subsystem stays behaviorally
untouched; `knowledge://` remains the citation currency.

```
corpus_rag/
  corpora.py        # Corpus + membership (operator-selected source sets; durable, inspectable)
  documents.py      # Document/Section/provenance models (page, bbox, span, section path)
  pdf_backend.py    # PdfExtractor protocol; PyMuPdfBackend (born-digital); TextBackend
  chunking.py       # two-tier: evidence spans (~300 tok) + section units (~1200 tok), shared parents
  embeddings.py     # EmbeddingProvider protocol; ArcticEmbed provider (sentence-transformers);
                    # records {model_id, revision, dims, index_version} per vector row
  vector_index.py   # VectorIndex protocol: NumpyVectorIndex (SQLite-local) + PgVectorIndex (pgvector HNSW)
  retrieval.py      # hybrid: lexical(existing FTS/BM25) + dense -> RRF -> source diversity
                    #       -> optional cross-encoder rerank -> optional section/neighborhood expansion
  planner.py        # bounded query decomposition (<=6 subqueries, heuristic default, LLM optional,
                    #                   decomposition recorded in debug output)
  advisory.py       # MethodAdvisory schema + grounded synthesis (LLM provider interface +
                    # extractive fallback); contradictions surfaced, uncertainty explicit
  llm_provider.py   # OpenAI-compatible client + OfflineDeterministic stub (no network required)
scripts/corpus_rag/ # CLI: fetch_corpus, ingest_corpus, build_index, run_benchmark, ask
eval assets         # manifest (sha256 per source), questions.jsonl, qrels.tsv, rubric
```

Storage additions (both backends, additive):
- `rag_corpora(corpus_id, slug, title, created_at, metadata)`
- `rag_corpus_sources(corpus_id, source_id, added_at, UNIQUE(corpus_id, source_id))`
- `rag_documents(doc_id, source_id, doc_type, title, authors, year, doi_isbn_url, extraction_version)`
- `rag_sections(section_id, doc_id, path (h1.h2.h3…), title, level, page_start, page_end, summary?)`
- `rag_chunks(chunk_id, source_id, doc_id?, section_id?, kind ∈ {evidence_span, section_unit},
   text, digest, token_count, page_start, page_end, char_start, char_end, section_path, index_version)`
- vectors: SQLite `rag_chunk_vectors(chunk_id PK, vec BLOB(f16))` +
  in-memory numpy ann; PG `rag_chunk_vectors(chunk_id, embedding halfvec(dim))` + HNSW cosine.

Model choices (evidence-based, see librarian report):
- Embeddings: `snowflake-arctic-embed-m-v1.5` (Apache-2.0, 109M, 768d MRL-trimmable,
  MTEB-Retrieval 55.14 — best ≤137M class), fallback `snowflake-arctic-embed-s`.
  Chunk targets ≤512 tokens. CPU-only friendly.
- Reranker: `bge-reranker-base` (MIT, 278M) scoring (query, chunk[:~320 tok]),
  batch 16; `cross-encoder/ms-marco-MiniLM-L-6-v2` as latency escape hatch.
- pgvector v0.8.6 confirmed sufficient: HNSW (m=16, ef_construction=64,
  ef_search≥40), halfvec indexable ≤4000 dims, iterative scans, documented
  FTS+RRF hybrid pattern. No Qdrant/Chroma.

Ingestion preserves: original bytes (never prompted), sha256 digest (canonical),
title/authors/year/DOI-or-ISBN-or-URL when supplied, doc type, page numbers,
section hierarchy, exact char spans per chunk, extraction version. Structure-aware
extraction via PyMuPDF font-size/numbering heuristics; OCR explicitly out of scope.

## 4. Hypotheses to test

- H1 Hybrid (lexical+dense, RRF) beats lexical alone on recall@10 for
  conceptual/methodological questions (vocabulary-mismatch cases).
- H2 Cross-encoder reranking improves precision@5/nDCG@10 over fused ranking.
- H3 Coarse section-unit candidates refined into evidence spans beat
  small-chunk-only retrieval for broad questions (citation usefulness ↑).
- H4 pgvector HNSW gives sub-50 ms dense queries at prototype scale and scales
  to ~10⁶ chunks; numpy brute force is competitive below ~10⁵ chunks.
- H5 Structured provenance yields correct, resolvable citations (spot-audit).

## 5. Benchmark design

- **Corpus**: 15–20 legitimately open sources (ISLR2, ESL print12, von Luxburg
  clustering stability arXiv:1007.1075, Monti 2003 [DOI; publisher-paywalled flag],
  Şenbabaoğlu SciRep 4:6207, Meinshausen-Bühlmann arXiv:0809.2932, Shah-Samworth
  arXiv:1105.5578, glmnet JSS v33i01, Cawley-Talbot JMLR 11(70), Guo et al.
  arXiv:1706.04599, SMOTE JAIR, He-Garcia [DOI; paywalled flag], Saito-Rehmsmeier
  PLOS ONE, Strobl BMC Bioinformatics 9:307, Gregorutti arXiv:1310.5726,
  Benjamini-Yekutieli 2001, Efron 1979, Koenker-Hallock JEP 2001, gap statistic
  TR). Manifest pins URL + sha256 + license note per source.
- **Questions**: ≥8 Honeydew-style questions (clustering geometry, stability
  assessment, n≪p, severe imbalance evaluation, nested-CV necessity,
  linear-model assumption alternatives, calibration checking, multiple-testing).
- **Qrels**: per-question relevant sections/passages judged 0/1/2 (graded),
  derived from known TOC/headings then manually verified.
- **Retrieval metrics**: recall@{5,10}, MRR@10, nDCG@10, distinct_sources@10,
  duplicate-rate@10, stage latencies, packet token cost. Modes: lexical | dense |
  hybrid | hybrid+rerank (+expansion ablation).
- **Advisory rubric** (scored 0–2 each): groundedness, citation validity,
  methodological relevance, candidate diversity, assumptions surfaced, failure
  modes surfaced, overreach penalty, experiment-matrix usefulness. Compare
  no-context vs lexical-context vs prototype-RAG-context when an LLM endpoint is
  available; otherwise extractive fallback demonstrates grounding mechanics.

## 6. Integration point (future, not wired today)

Honeydew would eventually call
`corpus_rag.advisory.build_method_advisory(run_id, objective, dataset_profile, corpora=[slug])`
which internally reuses `KnowledgeManager.retrieve()` semantics (scoping, budgets,
packets) and returns the advisory + durable packet. Engine wiring is deliberately
NOT changed in this prototype.

## 7. Non-goals

Live deployment · OCR · new infrastructure services · changes to evaluator/workflow
authority · autonomous approval flows. RAG proposes evidence; Honeydew reasons;
the deterministic control plane retains authority.
