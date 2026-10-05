"""Content-aware token estimation leaf (#517).

One dependency-free definition of the token estimate every orchestrator budget
shares. Wiring this in replaces the whitespace word count that was duplicated
in ``knowledge_manager``, ``corpus_rag.spans``, and ``corpus_rag.retrieval`` --
a measure that under-counted compact JSON, code, and tool output by several
times (a compact document is a single "word").

The estimate is the maximum of three floors:

* a floor of ``1`` so empty or whitespace-only text is never zero-cost;
* the whitespace word count, preserving the old behavior exactly as a lower
  bound; and
* ``ceil(len(text) / CHARS_PER_TOKEN)``, which counts compact content by size.

This leaf imports nothing from ``app`` so it can never participate in the
``app.knowledge_manager`` <-> ``app.corpus_rag`` import cycle documented at
``app/corpus_rag/chat.py``.
"""

from __future__ import annotations

# Characters per model token. The live fatal prompt measured ~3.8 characters
# per token; dividing by a smaller number over-counts, so budgets trip earlier
# rather than later. Integer math keeps the result deterministic.
CHARS_PER_TOKEN = 3


def estimate_tokens(text: str) -> int:
    """Estimate model tokens for ``text`` as a content-aware floor.

    Returns ``max(1, whitespace words, ceil(len(text) / CHARS_PER_TOKEN))``.
    The result is monotonic in ``text`` and never below the whitespace word
    count, so every existing word-based budget remains satisfiable while
    compact content is counted by its actual size. Empty and whitespace-only
    text has no content to size, so it costs the floor of one token even when
    its character count alone would round higher.
    """
    words = len(text.split())
    if words == 0:
        return 1
    return max(1, words, -(-len(text) // CHARS_PER_TOKEN))
