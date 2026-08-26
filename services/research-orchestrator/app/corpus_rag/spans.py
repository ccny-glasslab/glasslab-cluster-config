"""Character-span arithmetic for structure-aware chunking.

Pure helpers shared by the chunking tier builder: sentence segmentation,
word-boundary splitting of oversized atoms, and bounded grouping of ordered
atoms into contiguous spans. Every function is deterministic and side-effect
free; spans are absolute ``(start, end)`` offsets into the document text and
always satisfy ``text[start:end] == concatenation of covered atoms``.
"""

from __future__ import annotations

import re
from math import ceil

_SENTENCE_SPLIT = re.compile(r'(?<=[.!?])\s+')
_WORD = re.compile(r'\S+')


def estimate_tokens(text: str) -> int:
    """Whitespace-split token count with floor 1 (mirrors knowledge_manager)."""
    return max(1, len(text.split()))


def sentence_atoms(body: str, base: int) -> list[tuple[int, int]]:
    """Absolute (start, end) spans of sentences in ``body`` offset by base."""
    atoms: list[tuple[int, int]] = []
    start = 0
    for match in _SENTENCE_SPLIT.finditer(body):
        atoms.append((base + start, base + match.start()))
        start = match.end()
    atoms.append((base + start, base + len(body)))
    return [(s, e) for s, e in atoms if e > s]


def split_oversized(
    text: str, start: int, end: int, max_tokens: int
) -> list[tuple[int, int]]:
    """Word-boundary split of one atom so every piece has <= max_tokens."""
    pieces: list[tuple[int, int]] = []
    piece_start: int | None = None
    piece_words = 0
    piece_end = start
    for match in _WORD.finditer(text, start, end):
        if piece_start is not None and piece_words + 1 > max_tokens:
            pieces.append((piece_start, piece_end))
            piece_start = None
            piece_words = 0
        if piece_start is None:
            piece_start = match.start()
        piece_words += 1
        piece_end = match.end()
    if piece_start is not None:
        pieces.append((piece_start, piece_end))
    return pieces


def group_atoms(
    atoms: list[tuple[int, int]],
    text: str,
    *,
    maximum: int,
    target: int,
    merge_tail: bool,
    group_count: int | None = None,
) -> list[tuple[int, int]]:
    """Group ordered atoms into contiguous (start, end) spans.

    A group never exceeds ``maximum`` tokens (oversized lone atoms must have
    been pre-split by the caller). With ``group_count``, groups flush early
    once they reach the ideal per-group share so the count approaches
    ``group_count``. With ``merge_tail``, a final group below ``target`` is
    folded into the previous group.
    """
    ideal: int | None = None
    if group_count is not None and group_count > 1:
        total = sum(estimate_tokens(text[s:e]) for s, e in atoms)
        ideal = ceil(total / group_count)
    groups: list[list[tuple[int, int]]] = []
    current: list[tuple[int, int]] = []
    current_tokens = 0
    for span in atoms:
        span_tokens = estimate_tokens(text[span[0]:span[1]])
        if current and current_tokens + span_tokens > maximum:
            groups.append(current)
            current = []
            current_tokens = 0
        current.append(span)
        current_tokens += span_tokens
        if (
            ideal is not None
            and len(groups) < group_count - 1
            and current_tokens >= ideal
        ):
            groups.append(current)
            current = []
            current_tokens = 0
    if current:
        if merge_tail and groups and current_tokens < target:
            groups[-1].extend(current)
        else:
            groups.append(current)
    return [(spans[0][0], spans[-1][1]) for spans in groups]
