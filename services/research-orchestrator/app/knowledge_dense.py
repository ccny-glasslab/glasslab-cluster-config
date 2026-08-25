"""Production dense retrieval over the canonical knowledge chunk namespace.

Vectors attach to existing ``knowledge_chunks`` rows — this module creates no
secondary chunk namespace. Surfaces:

- :class:`NumpyChunkIndex`: brute-force cosine over stored vector bytes;
  works on every store backend.
- :class:`PgVectorChunkIndex`: HNSW-backed search over the guarded halfvec
  column; requires a PostgreSQL DSN where the pgvector extension is available.
- :func:`build_dense_index`: idempotent batch embedding of every existing
  chunk for one embedding-model lineage.

Dimension/model mismatches degrade readiness and are reported rather than
silently mixed: rows whose ``dims`` disagree with the active provider are
ignored and surfaced through ``readiness()``.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Any

import numpy as np

from .corpus_rag.contracts import ChunkVectorMeta
from .corpus_rag.embeddings import decode_vector, encode_vector

DENSE_INDEX_VERSION = 'dense-v1'

_HALFVEC_COLUMN_DIMS = 768


def _halfvec_literal(values: Any) -> str:
    """Format a vector as a halfvec literal, zero-padded to the column dims.

    The physical column is ``halfvec(768)`` and enforces exactly 768
    dimensions. Zero-padding shorter vectors is cosine-invariant, so small
    test/embedding lineages share the same column safely; the canonical
    ``dims``/bytes columns keep the true width.
    """
    arr = np.asarray(values, dtype=np.float32).ravel()
    if arr.shape[0] > _HALFVEC_COLUMN_DIMS:
        raise DenseModelError(
            f'vector has {arr.shape[0]} dims, exceeds halfvec column'
            f' ({_HALFVEC_COLUMN_DIMS})'
        )
    padded = np.zeros(_HALFVEC_COLUMN_DIMS, dtype=np.float32)
    padded[:arr.shape[0]] = arr
    return '[' + ','.join(f'{x:.7g}' for x in padded) + ']'


class DenseModelError(Exception):
    """An operation would mix incompatible embedding lineages."""


@dataclass(frozen=True)
class DenseReadiness:
    available: bool
    reason: str
    backend: str
    model_id: str
    revision: str
    dims: int
    indexed_count: int


def _unit_row(vec: Any, *, dims: int | None = None) -> np.ndarray:
    arr = np.ascontiguousarray(vec, dtype=np.float32)
    if arr.ndim != 1:
        raise DenseModelError(f'vector must be 1-D, got shape {arr.shape}')
    if dims is not None and arr.shape[0] != dims:
        raise DenseModelError(
            f'vector has {arr.shape[0]} dims, expected {dims}'
        )
    norm = float(np.linalg.norm(arr))
    return arr / norm if norm > 0 else arr.copy()


def _provider_revision(provider: Any) -> str:
    return getattr(provider, 'revision', '') or ''


def _provider_dims(provider: Any) -> int:
    return int(getattr(provider, 'dims'))


class NumpyChunkIndex:
    """Brute-force cosine over stored vector bytes (fine to ~100k chunks)."""

    def __init__(
        self,
        store: Any,
        provider: Any,
        model_id: str | None = None,
    ) -> None:
        self._store = store
        self._provider = provider
        self.model_id = model_id or provider.model_id
        self._rows: dict[str, np.ndarray] = {}
        self._mismatched = 0
        self._cache: tuple[list[str], np.ndarray] | None = None
        self.reload()

    def reload(self) -> None:
        self._rows = {}
        self._mismatched = 0
        self._cache = None
        expected = self._provider_dims()
        for meta, blob in self._store.list_knowledge_chunk_vectors(self.model_id):
            vec = decode_vector(blob)
            if meta.dims != expected or vec.shape[0] != expected:
                self._mismatched += 1
                continue
            self._rows[meta.chunk_id] = _unit_row(vec)

    def _provider_dims(self) -> int:
        return _provider_dims(self._provider)

    def embed_query(self, text: str) -> np.ndarray:
        return self._provider.embed_queries([text])[0]

    def readiness(self) -> DenseReadiness:
        reason = ''
        available = bool(self._rows)
        mismatch_note = (
            f'{self._mismatched} row(s) ignored for dimension mismatch'
            if self._mismatched
            else ''
        )
        if not available:
            reason = f'no usable vectors for model {self.model_id!r}'
            if mismatch_note:
                reason += f'; {mismatch_note}'
        elif mismatch_note:
            reason = mismatch_note
        return DenseReadiness(
            available=available,
            reason=reason,
            backend='numpy',
            model_id=self.model_id,
            revision=_provider_revision(self._provider),
            dims=self._provider_dims(),
            indexed_count=len(self._rows),
        )

    def search(
        self,
        query_vec: np.ndarray,
        *,
        allowed_chunk_ids: set[str] | None = None,
        k: int = 10,
    ) -> list[tuple[str, float]]:
        if k <= 0 or not self._rows:
            return []
        cached = self._cache
        if cached is None:
            ids = list(self._rows)
            cached = (ids, np.stack([self._rows[cid] for cid in ids]))
            self._cache = cached
        ids, matrix = cached

        q_unit = _unit_row(query_vec, dims=self._provider_dims())
        scores = matrix @ q_unit

        mask = np.ones(len(ids), dtype=bool)
        if allowed_chunk_ids is not None:
            allowed = set(allowed_chunk_ids)
            mask = np.array([cid in allowed for cid in ids], dtype=bool)
        candidates = np.flatnonzero(mask)
        if candidates.size == 0:
            return []

        k_eff = min(k, candidates.size)
        top = np.argpartition(scores[candidates], -k_eff)[-k_eff:]
        ranked = sorted(
            (
                (ids[candidates[pos]], float(scores[candidates[pos]]))
                for pos in top
            ),
            key=lambda item: (-item[1], item[0]),
        )
        return ranked

    def hydrate(self, chunk_ids: list[str]) -> list[dict[str, Any]]:
        return self._store.get_knowledge_chunks(chunk_ids)


class PgVectorChunkIndex:
    """HNSW-backed dense search over the guarded halfvec column."""

    def __init__(
        self,
        store_or_dsn: Any,
        provider: Any,
        model_id: str | None = None,
    ) -> None:
        if hasattr(store_or_dsn, 'list_knowledge_chunk_vectors'):
            self._store = store_or_dsn
        else:
            from .postgres_store import PostgresStore

            self._store = PostgresStore(str(store_or_dsn))
        self._provider = provider
        self.model_id = model_id or provider.model_id

    def embed_query(self, text: str) -> np.ndarray:
        return self._provider.embed_queries([text])[0]

    def readiness(self) -> DenseReadiness:
        rows = self._store.list_knowledge_chunk_vectors(self.model_id)
        expected = self._provider_dims()
        usable = [
            (meta, blob) for meta, blob in rows
            if meta.dims == expected
            and decode_vector(blob).shape[0] == expected
        ]
        mismatched = len(rows) - len(usable)
        reason = ''
        available = bool(usable)
        if not available:
            reason = f'no usable pgvector rows for model {self.model_id!r}'
        elif mismatched:
            reason = f'{mismatched} row(s) ignored for dimension mismatch'
        return DenseReadiness(
            available=available,
            reason=reason,
            backend='pgvector',
            model_id=self.model_id,
            revision=_provider_revision(self._provider),
            dims=expected,
            indexed_count=len(usable),
        )

    def search(
        self,
        query_vec: np.ndarray,
        *,
        allowed_chunk_ids: set[str] | None = None,
        k: int = 10,
    ) -> list[tuple[str, float]]:
        if k <= 0:
            return []
        literal = _halfvec_literal(query_vec)
        sql = (
            'SELECT v.chunk_id, 1 - (v.embedding <=> %s::halfvec) AS score'
            ' FROM orchestrator_knowledge_chunk_vectors v'
            ' WHERE v.model_id = %s AND v.embedding IS NOT NULL'
        )
        params: list[Any] = [literal, self.model_id]
        if allowed_chunk_ids is not None:
            ids = sorted(allowed_chunk_ids)
            if not ids:
                return []
            sql += ' AND v.chunk_id = ANY(%s)'
            params.append(ids)
        sql += ' ORDER BY v.embedding <=> %s::halfvec LIMIT %s'
        params.extend([literal, k])
        with self._store._connect() as conn:
            with conn.transaction():
                conn.execute('SET LOCAL hnsw.ef_search = 100')
                rows = conn.execute(sql, params).fetchall()
        return [(row['chunk_id'], float(row['score'])) for row in rows]

    def hydrate(self, chunk_ids: list[str]) -> list[dict[str, Any]]:
        return self._store.get_knowledge_chunks(chunk_ids)

    def _provider_dims(self) -> int:
        return _provider_dims(self._provider)


def build_dense_index(
    store: Any,
    provider: Any,
    *,
    model_id: str | None = None,
    batch_size: int = 32,
    force: bool = False,
) -> dict[str, Any]:
    """Embed every existing knowledge chunk for one model lineage.

    Idempotent: chunks that already carry a vector for ``model_id`` are
    skipped unless ``force`` re-embeds them. Only chunks that already passed
    KnowledgeManager ingestion (secret/path checks included) can ever reach
    this function, because it enumerates ``knowledge_chunks``.
    """
    mid = model_id or provider.model_id
    existing = {
        meta.chunk_id for meta, _ in store.list_knowledge_chunk_vectors(mid)
    }
    chunks = store.list_knowledge_chunks()
    todo = [
        chunk for chunk in chunks
        if force or chunk['chunk_id'] not in existing
    ]

    indexed = 0
    skipped = 0
    for start in range(0, len(todo), batch_size):
        batch = todo[start:start + batch_size]
        vectors = provider.embed_passages([row['text'] for row in batch])
        for row, vec in zip(batch, vectors):
            meta = ChunkVectorMeta(
                chunk_id=row['chunk_id'],
                model_id=mid,
                revision=_provider_revision(provider),
                dims=_provider_dims(provider),
                index_version=DENSE_INDEX_VERSION,
            )
            try:
                store.upsert_knowledge_chunk_vectors(meta, encode_vector(vec))
            except Exception as exc:  # noqa: BLE001 - per-row isolation below
                if type(exc).__name__ != 'IntegrityError':
                    raise
                skipped += 1
                print(
                    f'[build_dense_index] skipping unresolvable chunk {meta.chunk_id}',
                    file=sys.stderr,
                )
                continue
            indexed += 1

    return {
        'model_id': mid,
        'revision': _provider_revision(provider),
        'dims': _provider_dims(provider),
        'n_vectors': indexed,
        'skipped': skipped,
        'index_version': DENSE_INDEX_VERSION,
    }
