"""Persist dense vectors for the corpus-RAG store (the embed step).

The ``/ui`` corpus chat retrieves from ``orchestrator_rag_chunks``. Lexical
retrieval needs no vectors, but ``dense``/``hybrid`` retrieval needs the rows in
``orchestrator_rag_chunk_vectors`` that :mod:`app.corpus_rag.vector_index`
serves. This module bridges the two: it embeds every evidence-span chunk that
lacks a vector for the active model lineage and persists the result.

Two write paths are supported, both keyed to the canonical
``orchestrator_rag_chunk_vectors`` schema:

* pass a :class:`~app.corpus_rag.vector_index.VectorIndex` (``PgVectorIndex``)
  and each vector is written through its ``add`` (canonical bytes plus the
  halfvec column HNSW reads);
* pass ``vector_index=None`` and the canonical bytes are written directly via
  ``store.upsert_rag_chunk_vectors`` (works on SQLite and Postgres).

The step is additive and idempotent: a chunk whose stored vector already
matches the active ``model_id``/``revision``/``dims`` is skipped, so re-running
it is a no-op. It never invents chunks; it only enumerates rows that already
exist in the store. This mirrors :func:`app.knowledge_dense.build_dense_index`
for the canonical RAG chunk namespace instead of the knowledge namespace.
"""

from __future__ import annotations

from typing import Any

from .contracts import RAG_INDEX_VERSION, ChunkVectorMeta
from .embeddings import encode_vector
from .vector_index import NumpyVectorIndex, PgVectorIndex, VectorIndex

__all__ = [
    'build_rag_dense_index',
    'open_rag_vector_index',
    'plan_rag_dense_index',
    'rag_dense_ready',
]

_EMBED_BATCH = 64
# Match app.corpus_rag.pipeline.build_index: only evidence spans are embedded.
_INDEX_KINDS = ['evidence_span']


def _provider_dims(provider: Any) -> int:
    return int(getattr(provider, 'dims', 0) or 0)


def _provider_revision(provider: Any) -> str:
    return str(getattr(provider, 'revision', '') or '')


def _current_lineage_ids(
    store: Any,
    model_id: str,
    dims: int,
    revision: str,
) -> set[str]:
    """Chunk ids whose stored vector matches the active lineage.

    An empty declared dimension/revision cannot be verified, so it is not
    filtered on (a lazy provider that has not resolved its lineage must not
    force a full re-embed on every run).
    """
    existing: set[str] = set()
    for meta, _blob in store.list_rag_chunk_vectors(model_id):
        if dims and meta.dims != dims:
            continue
        if revision and meta.revision != revision:
            continue
        existing.add(meta.chunk_id)
    return existing


def _pending_rows(
    store: Any,
    provider: Any,
    *,
    model_id: str | None,
    force: bool,
) -> tuple[str, list[dict[str, Any]], int]:
    mid = model_id or provider.model_id
    rows = store.list_rag_chunks(kinds=_INDEX_KINDS, limit=None)
    if force:
        return mid, rows, 0
    existing = _current_lineage_ids(
        store, mid, _provider_dims(provider), _provider_revision(provider)
    )
    todo = [row for row in rows if row['chunk_id'] not in existing]
    return mid, todo, len(rows) - len(todo)


def plan_rag_dense_index(
    store: Any,
    provider: Any,
    *,
    model_id: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Report how many evidence spans still need a current-lineage vector."""
    mid, todo, existing = _pending_rows(
        store, provider, model_id=model_id, force=force
    )
    return {
        'model_id': mid,
        'revision': _provider_revision(provider),
        'dims': _provider_dims(provider),
        'total': existing + len(todo),
        'existing': existing,
        'pending': len(todo),
        'index_version': RAG_INDEX_VERSION,
    }


def build_rag_dense_index(
    store: Any,
    provider: Any,
    *,
    model_id: str | None = None,
    vector_index: VectorIndex | None = None,
    batch_size: int = _EMBED_BATCH,
    force: bool = False,
) -> dict[str, Any]:
    """Embed evidence spans missing a current-lineage vector (idempotent).

    Returns a summary dict. ``vector_index`` (a ``PgVectorIndex``) writes the
    canonical bytes plus the halfvec column; when omitted the canonical bytes
    are written through the store so the numpy index can serve them.
    """
    if batch_size < 1:
        raise ValueError('batch_size must be >= 1')
    mid, todo, existing = _pending_rows(
        store, provider, model_id=model_id, force=force
    )
    indexed = 0
    skipped = 0
    for start in range(0, len(todo), batch_size):
        batch = todo[start : start + batch_size]
        vectors = provider.embed_passages([row['text'] for row in batch])
        # The provider may resolve its revision/dims lazily on first use;
        # read them after embedding so the stored lineage is the true one.
        revision = _provider_revision(provider) or 'unknown'
        declared_dims = _provider_dims(provider)
        for row, vector in zip(batch, vectors):
            meta = ChunkVectorMeta(
                chunk_id=row['chunk_id'],
                model_id=mid,
                revision=revision,
                dims=declared_dims or int(vector.shape[0]),
                index_version=RAG_INDEX_VERSION,
            )
            try:
                if vector_index is not None:
                    vector_index.add(meta, vector)
                else:
                    store.upsert_rag_chunk_vectors(meta, encode_vector(vector))
            except Exception as exc:  # noqa: BLE001 - per-row isolation
                # A row whose parent chunk vanished between listing and write
                # must not kill a long unattended run; skipping stays safe
                # because citation resolution re-verifies every chunk.
                if type(exc).__name__ not in ('IntegrityError', 'ForeignKeyViolation'):
                    raise
                skipped += 1
                continue
            indexed += 1
    return {
        'model_id': mid,
        'revision': _provider_revision(provider),
        'dims': _provider_dims(provider),
        'n_vectors': indexed,
        'existing': existing,
        'skipped': skipped,
        'index_version': RAG_INDEX_VERSION,
    }


def open_rag_vector_index(
    store: Any,
    provider: Any,
    *,
    pg_dsn: str = '',
) -> VectorIndex:
    """Open the dense index the chat reads for the provider's lineage.

    With a Postgres DSN, HNSW-backed :class:`PgVectorIndex` is returned.
    Otherwise an in-process :class:`NumpyVectorIndex` is built from the
    canonical stored bytes (fine to ~100k chunks) with an explicit
    chunk->source map so source-scoped search works with bare hex chunk ids.
    """
    if pg_dsn:
        return PgVectorIndex(str(pg_dsn), provider.model_id)

    dims = _provider_dims(provider)
    revision = _provider_revision(provider)
    entries = [
        (meta, blob)
        for meta, blob in store.list_rag_chunk_vectors(provider.model_id)
        if (not dims or meta.dims == dims)
        and (not revision or meta.revision == revision)
    ]
    source_of = {
        row['chunk_id']: row['source_id']
        for row in store.list_rag_chunks(limit=None)
    }
    return NumpyVectorIndex(entries=entries, source_of=source_of)


def rag_dense_ready(store: Any, provider: Any) -> bool:
    """True when the store holds at least one vector for the provider lineage.

    The chat factory uses this before selecting a non-lexical mode: a built
    provider object with an empty index would make ``dense`` return zero hits,
    so readiness is a data question, not just an object-construction question.
    """
    dims = _provider_dims(provider)
    revision = _provider_revision(provider)
    return any(
        (not dims or meta.dims == dims)
        and (not revision or meta.revision == revision)
        for meta, _blob in store.list_rag_chunk_vectors(provider.model_id)
    )
