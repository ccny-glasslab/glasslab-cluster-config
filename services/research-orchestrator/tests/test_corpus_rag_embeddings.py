"""Corpus-RAG embedding-provider layer tests.

Pins the serialization contract (float32 little-endian blobs), the
``EmbeddingProvider`` protocol, the offline deterministic provider, the
factory choices, and (when the weights are already in the local HF cache)
the arctic-m provider metadata. Every test except the cached-model one runs
networkless with no downloads.
"""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pytest

from app.corpus_rag.contracts import EMBED_DIM
from app.corpus_rag.embeddings import (
    ArcticEmbedProvider,
    EmbeddingProvider,
    OfflineDeterministicEmbedding,
    decode_vector,
    encode_vector,
    get_provider,
)

ARCTIC_M = 'Snowflake/snowflake-arctic-embed-m-v1.5'


def _arctic_m_cached() -> bool:
    hub = Path(os.environ.get('HF_HOME', '/home/gr66ss/.cache/huggingface')) / 'hub'
    return hub.is_dir() and any(
        entry.name.startswith('models--Snowflake--snowflake-arctic-embed-m-v1.5')
        for entry in hub.iterdir()
        if entry.is_dir()
    )


def test_offline_provider_deterministic_and_normalized():
    provider = OfflineDeterministicEmbedding(dims=64)
    assert isinstance(provider, EmbeddingProvider)

    texts = ['alpha passage about clustering', 'beta passage about embeddings']
    first = provider.embed_passages(texts)
    second = provider.embed_passages(texts)

    assert first.shape == (2, 64)
    assert first.dtype == np.float32
    np.testing.assert_array_equal(first, second)

    other = provider.embed_passages(['a completely different text'])
    assert not np.array_equal(first[0], other[0])

    queries = provider.embed_queries(['gamma query about methods'])
    assert queries.shape == (1, 64)
    assert queries.dtype == np.float32

    np.testing.assert_allclose(np.linalg.norm(first, axis=1), 1.0, rtol=1e-5)
    np.testing.assert_allclose(np.linalg.norm(queries, axis=1), 1.0, rtol=1e-5)


def test_encode_decode_roundtrip():
    rng = np.random.default_rng(7)
    vec = rng.standard_normal(EMBED_DIM).astype(np.float32)

    blob = encode_vector(vec)
    assert len(blob) == EMBED_DIM * 4

    decoded = decode_vector(blob)
    assert decoded.dtype == np.float32
    np.testing.assert_array_equal(decoded, vec)


def test_factory_choices_and_errors():
    offline = get_provider('offline', dims=32)
    assert isinstance(offline, OfflineDeterministicEmbedding)
    assert offline.dims == 32
    assert offline.model_id == 'offline-deterministic'
    assert offline.revision == 'v0'

    with pytest.raises(ValueError, match='choices'):
        get_provider('does-not-exist')


@pytest.mark.skipif(
    not _arctic_m_cached(),
    reason='snowflake-arctic-embed-m-v1.5 weights not present in local HF cache',
)
def test_arctic_provider_metadata():
    provider = ArcticEmbedProvider(ARCTIC_M)
    try:
        passages = [
            'Glasslab runs bounded Kubernetes jobs for research workloads.',
            'Honeydew drafts the protocol and verifies the evidence.',
        ]
        vectors = provider.embed_passages(passages)

        assert provider.dims == EMBED_DIM
        assert vectors.shape == (2, EMBED_DIM)
        assert vectors.dtype == np.float32
        np.testing.assert_allclose(np.linalg.norm(vectors, axis=1), 1.0, rtol=1e-4)

        assert provider.model_id == ARCTIC_M
        assert isinstance(provider.revision, str) and provider.revision

        as_query = provider.embed_queries([passages[0]])
        as_passage = provider.embed_passages([passages[0]])
        assert not np.allclose(as_query[0], as_passage[0])
    finally:
        ArcticEmbedProvider.unload()
