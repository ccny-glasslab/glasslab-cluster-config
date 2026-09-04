"""Ingest-time chunk normalization: size cap and deduplication.

Pure functions over ``RagChunkRecord`` lists, applied after ``build_chunks``
in the ingest path. Splitting only subdivides (never merges), so the tier
builder's overlap behavior is preserved; deduplication keeps the first
occurrence of each ``(source_id, kind, text)`` triple so the two-tier
structure survives even when a short section makes a unit and its evidence
span textually identical.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence

from app.corpus_rag.chunking import MAX_CHUNK_TOKENS
from app.corpus_rag.contracts import RagChunkRecord
from app.corpus_rag.spans import (
    estimate_tokens,
    group_atoms,
    sentence_atoms,
    split_oversized,
)

__all__ = ['normalize_chunks']


def _det_id(key: str) -> str:
    return hashlib.sha256(key.encode('utf-8')).hexdigest()[:32]


def normalize_chunks(
    chunks: Sequence[RagChunkRecord],
    *,
    max_tokens: int = MAX_CHUNK_TOKENS,
) -> list[RagChunkRecord]:
    """Split oversized chunks and deduplicate (source_id, kind, text) pairs.

    Chunks whose ``token_count`` exceeds ``max_tokens`` are subdivided at
    sentence boundaries (word-boundary fallback for oversized sentences) so
    every piece is <= ``max_tokens``. Duplicate chunks (same source_id, kind,
    and identical text) keep only the first occurrence. ``chunk_index`` is
    renumbered and ``chunk_id`` recomputed deterministically so rebuilds stay
    idempotent.
    """
    normalized: list[RagChunkRecord] = []
    seen: set[tuple[str, str, str]] = set()
    for chunk in chunks:
        for piece in _split_oversized_chunk(chunk, max_tokens):
            key = (piece.source_id, piece.kind, piece.text)
            if key in seen:
                continue
            seen.add(key)
            normalized.append(piece)
    return [
        piece.model_copy(
            update={
                'chunk_index': index,
                'chunk_id': _det_id(f'{piece.source_id}|{piece.kind}|{index}'),
            }
        )
        for index, piece in enumerate(normalized)
    ]


def _split_oversized_chunk(
    chunk: RagChunkRecord, max_tokens: int
) -> list[RagChunkRecord]:
    if chunk.token_count <= max_tokens:
        return [chunk]
    atoms = sentence_atoms(chunk.text, 0)
    unit_atoms: list[tuple[int, int]] = []
    for start, end in atoms:
        if estimate_tokens(chunk.text[start:end]) > max_tokens:
            unit_atoms.extend(split_oversized(chunk.text, start, end, max_tokens))
        else:
            unit_atoms.append((start, end))
    spans = group_atoms(
        unit_atoms,
        chunk.text,
        maximum=max_tokens,
        target=max_tokens,
        merge_tail=False,
    )
    pieces: list[RagChunkRecord] = []
    for start, end in spans:
        text = chunk.text[start:end]
        pieces.append(
            chunk.model_copy(
                update={
                    'text': text,
                    'digest': hashlib.sha256(text.encode('utf-8')).hexdigest(),
                    'token_count': estimate_tokens(text),
                    'char_start': (
                        chunk.char_start + start if chunk.char_start is not None else None
                    ),
                    'char_end': (
                        chunk.char_start + end if chunk.char_start is not None else None
                    ),
                }
            )
        )
    return pieces