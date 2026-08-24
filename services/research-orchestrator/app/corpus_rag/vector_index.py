"""Dense vector-index backends for corpus-RAG retrieval.

Two interchangeable backends behind the :class:`VectorIndex` protocol:

- :class:`NumpyVectorIndex`: brute-force cosine over an in-process float32
  matrix. Deterministic and dependency-free; the reference implementation.
- :class:`PgVectorIndex`: pgvector HNSW over
  ``orchestrator_rag_chunk_vectors.embedding`` (``halfvec_cosine_ops``),
  sharing the canonical opaque-bytes column through ``PostgresStore``.

Both return ``(chunk_id, cosine_similarity)`` pairs sorted by descending
score with ascending ``chunk_id`` as the deterministic tie-break. Vector
serialization reuses :func:`encode_vector`/:func:`decode_vector` from
``app.corpus_rag.embeddings``; psycopg is imported lazily so numpy-only
callers never touch it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

import numpy as np

from .contracts import EMBED_DIM, ChunkVectorMeta
from .embeddings import decode_vector, encode_vector

if TYPE_CHECKING:
    from ..postgres_store import PostgresStore

__all__ = [
    'NumpyVectorIndex',
    'PgVectorIndex',
    'VectorIndex',
    'open_vector_index',
]


@runtime_checkable
class VectorIndex(Protocol):
    """Structural interface for corpus-RAG dense indexes."""

    def add(self, meta: ChunkVectorMeta, vec: np.ndarray) -> None:
        """Insert or replace the row for ``meta.chunk_id``."""
        ...

    def search(
        self,
        query: np.ndarray,
        k: int,
        source_ids: list[str] | None = None,
    ) -> list[tuple[str, float]]:
        """Return up to k ``(chunk_id, cosine_similarity)`` hits, descending."""
        ...


def _as_unit_row(vec: np.ndarray, meta_dims: int | None = None) -> np.ndarray:
    """Validate one vector and L2-normalize it to float32 (zero stays zero)."""
    arr = np.ascontiguousarray(vec, dtype=np.float32)
    if arr.ndim != 1:
        raise ValueError(f'vector must be 1-D, got shape {arr.shape}')
    if meta_dims is not None and arr.shape[0] != meta_dims:
        raise ValueError(f'vector has {arr.shape[0]} dims, meta declares {meta_dims}')
    if not bool(np.isfinite(arr).all()):
        raise ValueError('vector contains non-finite components')
    norm = float(np.linalg.norm(arr))
    return arr / norm if norm > 0.0 else arr.copy()


def _format_halfvec_literal(vec: np.ndarray) -> str:
    """Render one vector as pgvector text input ``'[v1,v2,...]'``.

    The physical column is typed ``halfvec(EMBED_DIM)``, so vectors are
    zero-padded to EMBED_DIM components (trailing zeros do not change cosine
    geometry); the canonical ``vec`` byte column keeps the true width.
    Components use ``%.7g`` formatting and the whole literal is always bound
    as a SQL parameter — numbers are never interpolated into SQL text.
    """
    arr = np.ascontiguousarray(vec, dtype=np.float32).ravel()
    if arr.size > EMBED_DIM:
        raise ValueError(f'vector has {arr.size} dims; column is halfvec({EMBED_DIM})')
    padded = np.zeros(EMBED_DIM, dtype=np.float32)
    padded[: arr.size] = arr
    return '[' + ','.join('%.7g' % x for x in padded) + ']'


class NumpyVectorIndex:
    """Brute-force cosine index over an in-process float32 matrix.

    O(n*d) per query is fine up to roughly 100k chunks; beyond that prefer
    :class:`PgVectorIndex` (HNSW).

    Source filtering derives each row's ``source_id`` as the chunk_id
    substring before the first ``'::'`` delimiter (e.g.
    ``'<source_id>::c3'``); rows without the delimiter are invisible to
    filtered searches. This mirrors :class:`PgVectorIndex`, which resolves
    source membership through the ``orchestrator_rag_chunks`` table.
    """

    def __init__(
        self,
        entries: Iterable[tuple[ChunkVectorMeta, bytes]] | None = None,
        *,
        source_of: Mapping[str, str] | None = None,
    ) -> None:
        self._rows: dict[str, np.ndarray] = {}
        self._metas: dict[str, ChunkVectorMeta] = {}
        self._cache: tuple[list[str], np.ndarray] | None = None
        # Explicit mapping wins: production chunk ids are bare hex with no
        # delimiter, so callers must hydrate membership from the store.
        self._source_map = dict(source_of) if source_of else None
        for meta, blob in entries or ():
            self.add(meta, decode_vector(blob))

    def _source_for(self, chunk_id: str) -> str | None:
        if self._source_map is not None:
            return self._source_map.get(chunk_id)
        return chunk_id.split('::', 1)[0] if '::' in chunk_id else None

    def add(self, meta: ChunkVectorMeta, vec: np.ndarray) -> None:
        unit = _as_unit_row(vec, meta_dims=meta.dims)
        self._rows[meta.chunk_id] = unit
        self._metas[meta.chunk_id] = meta
        self._cache = None

    def search(
        self,
        query: np.ndarray,
        k: int,
        source_ids: list[str] | None = None,
    ) -> list[tuple[str, float]]:
        if k <= 0 or not self._rows:
            return []
        cached = self._cache
        if cached is None:
            ids = list(self._rows)
            cached = (ids, np.stack([self._rows[cid] for cid in ids]))
            self._cache = cached
        ids, matrix = cached

        q_unit = _as_unit_row(query)
        scores = matrix @ q_unit

        mask = np.ones(len(ids), dtype=bool)
        if source_ids:
            allowed = set(source_ids)
            mask = np.array(
                [self._source_for(cid) in allowed for cid in ids], dtype=bool
            )
        candidates = np.flatnonzero(mask)
        if candidates.size == 0:
            return []

        k_eff = min(k, candidates.size)
        top = np.argpartition(scores[candidates], -k_eff)[-k_eff:]
        ranked = sorted(
            top.tolist(),
            key=lambda pos: (-float(scores[candidates[pos]]), ids[candidates[pos]]),
        )
        return [(ids[candidates[pos]], float(scores[candidates[pos]])) for pos in ranked]


class PgVectorIndex:
    """pgvector HNSW index over ``orchestrator_rag_chunk_vectors.embedding``.

    ``add()`` writes the canonical opaque-bytes column plus provenance via
    ``PostgresStore.upsert_rag_chunk_vectors``, then populates the halfvec
    embedding column that the HNSW index serves. ``search()`` is a single
    roundtrip inside a transaction with ``hnsw.ef_search = 100``; scores are
    cosine similarity (``1 - halfvec`` cosine distance).

    The DSN is stored for connections only and is never logged.
    """

    EF_SEARCH = 100

    def __init__(self, dsn: str, model_id: str) -> None:
        from ..postgres_store import PostgresStore

        self._dsn = str(dsn)
        self.model_id = str(model_id)
        # Instantiating the store once bootstraps the schema (tables and the
        # HNSW halfvec_cosine_ops index) via its startup DDL.
        self._store: PostgresStore = PostgresStore(self._dsn)

    def add(self, meta: ChunkVectorMeta, vec: np.ndarray) -> None:
        arr = _as_unit_row(vec, meta_dims=meta.dims)
        self._store.upsert_rag_chunk_vectors(meta, encode_vector(arr))
        literal = _format_halfvec_literal(arr)
        with self._store._connect() as conn:
            conn.execute(
                'UPDATE orchestrator_rag_chunk_vectors'
                ' SET embedding = %s::halfvec WHERE chunk_id = %s',
                (literal, meta.chunk_id),
            )
            conn.commit()

    def search(
        self,
        query: np.ndarray,
        k: int,
        source_ids: list[str] | None = None,
    ) -> list[tuple[str, float]]:
        literal = _format_halfvec_literal(_as_unit_row(query))
        sql = (
            'SELECT v.chunk_id, 1 - (v.embedding <=> %s::halfvec) AS score'
            ' FROM orchestrator_rag_chunk_vectors v'
        )
        params: list[Any] = [literal]
        if source_ids:
            sql += (
                ' JOIN orchestrator_rag_chunks c'
                ' ON c.chunk_id = v.chunk_id AND c.source_id = ANY(%s)'
            )
            params.append(list(source_ids))
        sql += ' WHERE v.model_id = %s AND v.embedding IS NOT NULL'
        params.append(self.model_id)
        # chunk_id ascending after the distance keeps tie order deterministic.
        sql += ' ORDER BY v.embedding <=> %s::halfvec, v.chunk_id LIMIT %s'
        params.extend([literal, int(k)])
        with self._store._connect() as conn:
            with conn.transaction():
                conn.execute(f'SET LOCAL hnsw.ef_search = {int(self.EF_SEARCH)}')
                rows = conn.execute(sql, params).fetchall()
        return [(row['chunk_id'], float(row['score'])) for row in rows]


def open_vector_index(
    backend: Literal['numpy', 'pgvector'], **kwargs: Any
) -> VectorIndex:
    """Build a :class:`VectorIndex` backend by name.

    ``'numpy'`` accepts :class:`NumpyVectorIndex` kwargs (``entries``);
    ``'pgvector'`` requires ``dsn`` and ``model_id``.
    """
    if backend == 'numpy':
        return NumpyVectorIndex(**kwargs)
    if backend == 'pgvector':
        return PgVectorIndex(**kwargs)
    raise ValueError(
        f"unknown vector-index backend {backend!r}; choices: 'numpy', 'pgvector'"
    )
