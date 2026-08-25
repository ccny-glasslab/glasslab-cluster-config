# Honeydew Method Advisor (corpus-RAG productionization)

Date: 2026-08-24 · Branch: `feat/honeydew-method-advisor`
Companion to [`honeydew-corpus-rag-prototype-design-2026-08.md`](honeydew-corpus-rag-prototype-design-2026-08.md)
and [`honeydew-corpus-rag-prototype-results-2026-08.md`](honeydew-corpus-rag-prototype-results-2026-08.md).

## What this adds

During a normal run, Honeydew's `protocol_draft` and `methodology_review`
turns receive a bounded **method advisory**: structured candidate methods
(assumptions / diagnostics / failure modes / baselines / comparisons) each
grounded in mechanically resolvable `knowledge://` citations from an
operator-curated corpus. The advisory is injected into the turn context as
clearly-marked untrusted evidence — never instructions — and its full
provenance is persisted for later audit:

- a durable ContextPacket holding the exact ranked chunks Honeydew saw
- an `agent.method_advisory_built` event carrying the advisory digest
  (sha256 of the canonical payload) + packet id
- an `agent.method_advisory_attached` event recording delivery to the turn

Beaker never receives advisory content: the hook is gated to
Honeydew × {protocol_draft, methodology_review}, and source-type scoping
still applies underneath (implementation files are invisible to Honeydew).

## Architecture

```
objective + problem head
  -> MethodAdvisor.build_and_render()        app/method_advisor.py
     -> bounded query plan (<=6 subqueries)  corpus_rag/planner.py
     -> KnowledgeManager.retrieve(dense)     knowledge_manager.py
        (same scoping / budgets / packet+event path as lexical;
         cosine scores substitute into rank -> diversify -> budget)
     -> family matching over retrieved text  corpus_rag/advisory_families.py
     -> typed result | InsufficientCorpusAdvisory
     -> digest + event persistence
```

Dense retrieval lives in `app/knowledge_dense.py`: vectors attach to the
EXISTING `knowledge_chunks` identity (`knowledge_chunk_vectors` table,
additive DDL on both store backends; PG adds a guarded halfvec(768) column +
HNSW index). Embeddings default to `snowflake-arctic-embed-m-v1.5`
(revision recorded per vector row); a deterministic offline provider backs
tests. Model/dimension mismatch degrades readiness instead of mixing
lineages, and ANY dense failure falls back to lexical retrieval — recorded
in the `agent.context_retrieved` event as `retrieval_mode_actual`.

## Configuration (Settings)

| knob | default | meaning |
|---|---|---|
| `knowledge_advisory_enabled` | true | master switch for advisor wiring |
| `knowledge_dense_mode` | 'dense' | preferred mode for advisory turns |
| `knowledge_embedding_model` | snowflake-arctic-embed-m-v1.5 | provider lineage |
| `knowledge_embedding_revision` | '' | optional pin |
| `knowledge_dense_pg_dsn` | '' | use pgvector backend instead of in-process numpy |

Dependency boundary: numpy and PyMuPDF are import-time dependencies of the
dense surface and are pinned in `requirements.txt` (installed by the image
and by every CI lane that validates the production import surface). The model
runtime — torch/sentence-transformers — is installed by the image via
`requirements-dense.txt` but imported lazily: without model weights (or in a
slim environment without torch) the service starts, serves lexical retrieval,
and `/health` reports dense as unavailable with a reason.

## Operator commands

```bash
# Build/rebuild the corpus store (fetch -> ingest -> embed; resumable):
python services/research-orchestrator/scripts/build_knowledge_corpus.py \
    --store <dir>/knowledge-corpus.db --embedding arctic-m

# Retrieval benchmark on the production surface (lexical/dense/hybrid):
python services/research-orchestrator/scripts/corpus_rag/run_benchmark_km.py \
    --store <dir>/knowledge-corpus.db \
    --questions services/research-orchestrator/eval/corpus_rag/questions.jsonl \
    --qrels    services/research-orchestrator/eval/corpus_rag/qrels.tsv \
    --out      eval/corpus_rag/results/km-benchmark.json

# Diagnostics:
curl -s http://<orchestrator>/health | jq .knowledge_dense
```

`/health.knowledge_dense` reports availability, reason (when degraded),
backend, active model id/revision/dims, and indexed chunk count.

## Current benchmark note

On the 8-question graded benchmark (PR #220 artifact set): dense recall@10
0.906 vs lexical 0.781, hybrid 0.865, hybrid+rerank 0.865. Dense is therefore
the preferred method-advisory channel; fusion/reranking tuning is deferred.
Reranking remains an interface extension point only.

## Deliberately deferred

OCR for image-only scans · corpora beyond the curated manifest · fusion-weight
tuning · cross-encoder reranking · external web search · automatic literature
acquisition · LLM query synthesis · Discord UI surfacing.
