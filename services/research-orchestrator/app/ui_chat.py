"""UI-facing, URI-free contract for grounded corpus chat.

The operator ``/ui`` page must never emit a ``knowledge://`` evidence URI or
a filesystem path: a citation is reduced to the opaque ``source_id``, a
human-readable ``title``, the quoted ``excerpt``, its grounding ``verdict``,
and the 0-based ``page``. :class:`ChatCitation` therefore has no URI or path
field at all, and :class:`ChatAnswer` forbids extra fields, so a future
adapter cannot accidentally widen the leak surface without a contract change
here.

This module imports only pydantic; the retrieval/synthesis engine lives in
:mod:`app.corpus_rag.chat`.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ChatCitation(BaseModel):
    """One grounded chat citation, deliberately without a URI/path field.

    ``verdict`` is the deterministic :func:`app.citation_locator.classify_citation`
    classification of ``excerpt`` against the cited chunk text: ``exact`` for a
    verbatim (whitespace-collapsed) quote, ``fuzzy`` when only the normalized
    alphanumeric prefix matches, ``none`` when it cannot be grounded.
    """

    model_config = ConfigDict(extra='forbid')

    source_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    excerpt: str
    verdict: Literal['exact', 'fuzzy', 'none']
    page: int | None = None


class ChatAnswer(BaseModel):
    """One chat answer with its grounded citations and refusal flag."""

    model_config = ConfigDict(extra='forbid')

    answer: str
    citations: list[ChatCitation] = Field(default_factory=list)
    insufficient: bool = False
