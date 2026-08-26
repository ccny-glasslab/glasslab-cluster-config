"""Embedding-provider layer for the corpus-RAG prototype.

SERIALIZATION CONTRACT
======================

Stored vectors are **float32, little-endian** byte blobs. Downstream waves
(index store, hybrid retrieval) import :func:`encode_vector` and
:func:`decode_vector` instead of re-rolling their own dtype handling:

- ``encode_vector(vec)`` -> ``bytes``: ``np.ascontiguousarray(vec,
  dtype='<f4').tobytes()``
- ``decode_vector(blob)`` -> ``np.ndarray``: ``np.frombuffer(blob,
  dtype='<f4')`` (read-only view; copy before mutating)

PROVIDER CONTRACT
=================

:class:`EmbeddingProvider` is the structural protocol every provider
satisfies: ``model_id``/``revision``/``dims`` attributes plus
``embed_passages``/``embed_queries`` returning float32 arrays shaped
``(n, dims)`` with L2-normalized rows. Query-side prefixing is a provider
responsibility (arctic models need it; the offline provider does not).

Heavy runtimes (torch / sentence-transformers) are imported lazily inside
functions so importing this module stays cheap and dependency-free beyond
numpy.
"""

from __future__ import annotations

import gc
import hashlib
import os
from typing import TYPE_CHECKING, ClassVar, Protocol, runtime_checkable

import numpy as np

if TYPE_CHECKING:
    from sentence_transformers import SentenceTransformer

__all__ = [
    'ArcticEmbedProvider',
    'EmbeddingProvider',
    'OfflineDeterministicEmbedding',
    'decode_vector',
    'encode_vector',
    'get_provider',
]


# --- Serialization contract -------------------------------------------------


def encode_vector(vec: np.ndarray) -> bytes:
    """Serialize one vector to the canonical float32 little-endian blob."""
    return np.ascontiguousarray(vec, dtype='<f4').tobytes()


def decode_vector(blob: bytes) -> np.ndarray:
    """Deserialize a float32 little-endian blob back to a 1-D array."""
    return np.frombuffer(blob, dtype='<f4')


# --- Provider protocol ------------------------------------------------------


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Structural interface for corpus-RAG embedding providers."""

    model_id: str
    revision: str
    dims: int

    def embed_passages(self, texts: list[str]) -> np.ndarray:
        """Embed documents; returns float32 (n, dims), L2-normalized rows."""
        ...

    def embed_queries(self, texts: list[str]) -> np.ndarray:
        """Embed queries; returns float32 (n, dims), L2-normalized rows."""
        ...


class OfflineDeterministicEmbedding:
    """Hash-seeded random embeddings: deterministic across processes.

    Each text maps to a PCG64 stream seeded from its SHA-256 digest, so the
    same text always yields the same vector on any machine with the same
    numpy — useful for tests and pipelines that must not download weights.
    Queries and passages use the same mapping by design.
    """

    def __init__(self, dims: int = 64) -> None:
        self.model_id = 'offline-deterministic'
        self.revision = 'v0'
        self.dims = dims

    def _embed(self, texts: list[str]) -> np.ndarray:
        out = np.empty((len(texts), self.dims), dtype=np.float32)
        for row, text in enumerate(texts):
            seed = int.from_bytes(hashlib.sha256(text.encode()).digest()[:8])
            generator = np.random.Generator(np.random.PCG64(seed))
            vec = generator.standard_normal(self.dims)
            out[row] = vec / np.linalg.norm(vec)
        return out

    def embed_passages(self, texts: list[str]) -> np.ndarray:
        return self._embed(texts)

    def embed_queries(self, texts: list[str]) -> np.ndarray:
        return self._embed(texts)


class ArcticEmbedProvider:
    """Snowflake arctic-embed provider backed by sentence-transformers.

    The model loads lazily on the first embed call (never at construction or
    module import). ``dims`` and ``revision`` are declared eagerly from the
    static model table and the configured pin so index reloads after a
    process restart see the true lineage before any embedding happens; the
    first load verifies the probed output width against the declaration.
    Instances sharing a ``model_name`` reuse one loaded model via a
    class-level cache. Callers must :meth:`unload` before loading rerankers
    to keep peak RAM bounded.
    """

    DEFAULT_MODEL_NAME = 'Snowflake/snowflake-arctic-embed-m-v1.5'
    QUERY_PREFIX = 'Represent this sentence for searching relevant passages: '
    BATCH_SIZE = 32

    # Published output widths per supported model, so lineage checks work
    # before weights are loaded. A model absent from this table reports
    # dims=0 until first load, which readiness surfaces as unavailable
    # rather than silently mismatching stored vectors.
    _KNOWN_DIMS: ClassVar[dict[str, int]] = {
        'Snowflake/snowflake-arctic-embed-m-v1.5': 768,
        DEFAULT_MODEL_NAME: 768,
        'Snowflake/snowflake-arctic-embed-s': 384,
    }

    _cache: ClassVar[dict[tuple[str, str], 'SentenceTransformer']] = {}

    def __init__(
        self,
        model_name: str = DEFAULT_MODEL_NAME,
        revision: str = '',
    ) -> None:
        self.model_id = model_name
        self.revision = revision
        self.dims = type(self)._KNOWN_DIMS.get(model_name, 0)
        self._model_name = model_name
        self._loaded: SentenceTransformer | None = None

    @classmethod
    def _load_shared(cls, model_name: str, revision: str) -> 'SentenceTransformer':
        cache_key = (model_name, revision)
        model = cls._cache.get(cache_key)
        if model is None:
            import torch
            from sentence_transformers import SentenceTransformer

            # torch's default can leave cores idle on CPU-only hosts; batch
            # indexing wants every core available.
            torch.set_num_threads(max(1, os.cpu_count() or 1))
            # A pinned revision MUST reach the loader — otherwise the
            # reported provenance would describe weights that were never
            # loaded. Empty string means unpinned: defer to hub default.
            model = SentenceTransformer(model_name, revision=revision or None)
            cls._cache[cache_key] = model
        return model

    def _ensure_loaded(self) -> 'SentenceTransformer':
        if self._loaded is not None:
            return self._loaded
        model = type(self)._load_shared(self._model_name, self.revision)
        probe = model.encode(['dims probe'], normalize_embeddings=True)
        probed_dims = int(probe.shape[1])
        if self.dims and probed_dims != self.dims:
            raise RuntimeError(
                f'embedding model {self._model_name!r} produces '
                f'{probed_dims} dims but {self.dims} were declared; '
                'refusing to mix vector lineages'
            )
        self.dims = probed_dims
        if not self.revision:
            commit_hash = getattr(getattr(model, 'config', None), '_commit_hash', None)
            self.revision = str(commit_hash) if commit_hash else 'main'
        self._loaded = model
        return model

    def embed_passages(self, texts: list[str]) -> np.ndarray:
        model = self._ensure_loaded()
        encoded = model.encode(
            list(texts),
            batch_size=self.BATCH_SIZE,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        return np.asarray(encoded, dtype=np.float32)

    def embed_queries(self, texts: list[str]) -> np.ndarray:
        model = self._ensure_loaded()
        prefixed = [f'{self.QUERY_PREFIX}{text}' for text in texts]
        encoded = model.encode(
            prefixed,
            batch_size=self.BATCH_SIZE,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        return np.asarray(encoded, dtype=np.float32)

    def lineage_resolved(self) -> bool:
        """True once the served revision is known (pinned or loaded)."""
        return bool(self._loaded) or bool(self.revision)

    @classmethod
    def unload(cls) -> None:
        """Drop every cached model and reclaim RAM (call before rerankers)."""
        cls._cache.clear()
        gc.collect()


# --- Factory ----------------------------------------------------------------

_ARCTIC_MODEL_NAMES = {
    'arctic-s': 'Snowflake/snowflake-arctic-embed-s',
    'arctic-m': 'Snowflake/snowflake-arctic-embed-m-v1.5',
}


def get_provider(name: str = 'arctic-m', dims: int = 64) -> EmbeddingProvider:
    """Build an embedding provider by name ('arctic-m', 'arctic-s', 'offline')."""
    if name == 'offline':
        return OfflineDeterministicEmbedding(dims=dims)
    model_name = _ARCTIC_MODEL_NAMES.get(name)
    if model_name is None:
        choices = ', '.join(sorted((*_ARCTIC_MODEL_NAMES, 'offline')))
        raise ValueError(f'unknown embedding provider {name!r}; choices: {choices}')
    return ArcticEmbedProvider(model_name)
