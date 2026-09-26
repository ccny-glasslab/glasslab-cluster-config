"""Map a citation excerpt onto the ranked-source block it quoted.

A ``ContextPacket`` persists the literal prompt text it supplied in
``exact_text_supplied``, which is a preamble followed by one
``<knowledge-context ...>...</knowledge-context>`` block per ranked source,
in ``ranked_sources`` order. This module re-parses those blocks so a read-only
UI can point a citation (``knowledge_uri``, ``source``, ``excerpt``) at the
exact passage it referenced, and can classify whether the model's excerpt is
verbatim.

The prefix-matching rule mirrors the Discord packet renderer
(``discord_controls.format_packet_for_discord`` / ``build_packet_button_view``):
a normalized alphanumeric prefix of the excerpt locates the block, and the
citation's 1-based rank is the fallback. The verbatim check reuses the
deterministic ``knowledge_manager.verify_excerpt`` whitespace-collapse matcher.
The stored ``ranked_sources[].verified`` flag is never consulted: it is a
tautology recorded at build time, not an independent verification.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import re
from typing import Literal

from .knowledge_manager import verify_excerpt

_KNOWLEDGE_CONTEXT_RE = re.compile(
    r'<knowledge-context[^>]*>(.*?)</knowledge-context>',
    re.S,
)
_NON_ALNUM_RE = re.compile(r'[^A-Za-z0-9]')
_EXCERPT_PREFIX_CHARS = 36

CitationClass = Literal['exact', 'fuzzy', 'none']


@dataclass(frozen=True, slots=True)
class ContextBlock:
    """One ranked-source block extracted from ``exact_text_supplied``."""

    index: int
    text: str


@dataclass(frozen=True, slots=True)
class MatchResult:
    """The located block: its 0-based position and its stored text."""

    block_index: int
    block_text: str


def parse_context_blocks(exact_text_supplied: str | None) -> list[ContextBlock]:
    """Split the supplied context into one block per ranked source.

    Returns ``[]`` for ``None`` or empty input. Block order matches the order
    of ``ContextPacket.ranked_sources``.
    """
    if not exact_text_supplied:
        return []
    return [
        ContextBlock(index=index, text=text)
        for index, text in enumerate(
            _KNOWLEDGE_CONTEXT_RE.findall(exact_text_supplied)
        )
    ]


def normalize_alnum(text: str) -> str:
    """Collapse whitespace, then drop every non-alphanumeric character."""
    return _NON_ALNUM_RE.sub('', ' '.join(text.split()))


def _excerpt_prefix(excerpt: str) -> str:
    return normalize_alnum(excerpt)[:_EXCERPT_PREFIX_CHARS]


def _prefix_match_positions(
    blocks: Sequence[ContextBlock], prefix: str
) -> list[int]:
    if not prefix:
        return []
    return [
        position
        for position, block in enumerate(blocks)
        if prefix in normalize_alnum(block.text)
    ]


def _prefer_verbatim(
    blocks: Sequence[ContextBlock],
    positions: Sequence[int],
    excerpt: str,
) -> int:
    # A 36-character normalized prefix can collide across blocks. Prefer a
    # block where the full excerpt verifies verbatim; otherwise keep the old
    # first-prefix-match behavior.
    for position in positions:
        if verify_excerpt(excerpt, blocks[position].text):
            return position
    return positions[0]


def match_block(
    blocks: Sequence[ContextBlock],
    excerpt: str,
    source_index: int | None,
) -> MatchResult | None:
    """Locate the block a citation excerpt came from.

    Tries the normalized prefix of the excerpt first. When several blocks share
    that prefix, the block where the full excerpt verifies verbatim wins over
    the first prefix match. If the prefix finds nothing and ``source_index``
    (1-based) is within range, falls back to that rank. Returns ``None`` when
    neither resolves.
    """
    if not blocks:
        return None
    matched: int | None = None
    prefix_positions = _prefix_match_positions(blocks, _excerpt_prefix(excerpt))
    if prefix_positions:
        matched = _prefer_verbatim(blocks, prefix_positions, excerpt)
    elif source_index is not None and 1 <= source_index <= len(blocks):
        matched = source_index - 1
    if matched is None:
        return None
    return MatchResult(block_index=matched, block_text=blocks[matched].text)


def classify_citation(excerpt: str, block_text: str) -> CitationClass:
    """Classify an excerpt against the matched block text.

    ``exact`` only when the deterministic verbatim matcher confirms the full
    excerpt; ``fuzzy`` when only the normalized-prefix match holds; ``none``
    otherwise (including an empty or whitespace-only excerpt).
    """
    if verify_excerpt(excerpt, block_text):
        return 'exact'
    prefix = _excerpt_prefix(excerpt)
    if prefix and prefix in normalize_alnum(block_text):
        return 'fuzzy'
    return 'none'
