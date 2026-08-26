"""Two-tier structure-aware chunking over plain data.

Pure functions only: no store, no database, no heavy imports. The public API
takes ``document_text`` plus a sequence of section-like objects exposing
``path``/``title``/``level``/``start_char``/``end_char`` (structurally the
same shape ``documents.SectionNode`` will have), so chunking never imports
the extraction layer directly.

Tier semantics:

* ``section_unit`` — one per section whose body is at most
  ``UNIT_TARGET_TOKENS``; longer bodies split into
  ``ceil(tokens / UNIT_TARGET_TOKENS)`` sequential units at sentence
  boundaries (word-boundary fallback for oversized sentences), each capped at
  ``UNIT_MAX_TOKENS``.
* ``evidence_span`` — within each unit, a sliding window over sentences
  accumulating to ~``EVIDENCE_TARGET_TOKENS``; windows are hard-capped at
  ``EVIDENCE_MAX_TOKENS`` by splitting oversized sentences at word
  boundaries; a final window below ``EVIDENCE_MIN_TOKENS`` is merged into the
  previous span. Evidence spans never cross a section boundary.

Deterministic ids make rebuilds idempotent (safe for ``replace_rag_*``
store operations): ``section_id = sha256('{source_id}|{path}')[:32]`` and
``chunk_id = sha256('{source_id}|{kind}|{chunk_index}')[:32]``. No random
uuid4 is used anywhere in this module, so two invocations over identical
input produce identical records.

Character spans index into ``document_text`` with the hard invariant
``document_text[char_start:char_end] == chunk.text``; text is never stripped
independently of its offsets. ``doc_id`` defaults to ``source_id`` when not
provided because ``RagSectionRecord.doc_id`` must be non-empty.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from math import ceil
from typing import Literal, Protocol

from app.corpus_rag.contracts import (
    RAG_INDEX_VERSION,
    RagChunkRecord,
    RagSectionRecord,
)
from app.corpus_rag.spans import (
    estimate_tokens,
    group_atoms,
    sentence_atoms,
    split_oversized,
)

__all__ = [
    'ChunkPlan',
    'SupportsSpan',
    'build_chunks',
    'estimate_tokens',
]


@dataclass(frozen=True)
class ChunkPlan:
    """Token budgets for the two chunk tiers."""

    EVIDENCE_TARGET_TOKENS = 300
    EVIDENCE_MIN = 120
    EVIDENCE_MAX = 340
    UNIT_TARGET_TOKENS = 1200
    UNIT_MAX = 1260


class SupportsSpan(Protocol):
    """Structural expectation for section-like inputs (no documents import)."""

    @property
    def path(self) -> str: ...

    @property
    def title(self) -> str | None: ...

    @property
    def level(self) -> int: ...

    @property
    def start_char(self) -> int: ...

    @property
    def end_char(self) -> int: ...


def _det_id(key: str) -> str:
    return hashlib.sha256(key.encode('utf-8')).hexdigest()[:32]


def build_chunks(
    document_text: str,
    sections: Sequence[SupportsSpan],
    *,
    source_id: str,
    doc_id: str | None = None,
    page_for_char: Callable[[int], int | None] | None = None,
) -> tuple[list[RagSectionRecord], list[RagChunkRecord]]:
    """Build section_unit and evidence_span records for one document.

    Returns ``(section_records, chunk_records)`` with globally monotonic
    ``chunk_index`` ordered by (section order, unit position, parent before
    children). See the module docstring for id determinism and tier rules.
    """
    resolve_page: Callable[[int], int | None] = (
        page_for_char if page_for_char is not None else lambda _char: None
    )
    resolved_doc_id = doc_id if doc_id is not None else source_id

    section_records: list[RagSectionRecord] = []
    chunk_records: list[RagChunkRecord] = []
    chunk_index = 0

    ordered = sorted(sections, key=lambda sec: (sec.start_char, sec.end_char))
    for section in ordered:
        section_id = _det_id(f'{source_id}|{section.path}')
        section_records.append(
            RagSectionRecord(
                section_id=section_id,
                doc_id=resolved_doc_id,
                path=section.path,
                title=section.title,
                level=section.level,
                page_start=resolve_page(section.start_char),
                page_end=resolve_page(max(section.start_char, section.end_char - 1)),
            )
        )

        atoms = [
            (section.start_char + start, section.start_char + end)
            for start, end in sentence_atoms(
                document_text[section.start_char:section.end_char], 0
            )
        ]
        if not atoms:
            continue

        body_tokens = sum(estimate_tokens(document_text[s:e]) for s, e in atoms)
        unit_atoms: list[tuple[int, int]] = []
        for start, end in atoms:
            if estimate_tokens(document_text[start:end]) > ChunkPlan.UNIT_MAX:
                unit_atoms.extend(
                    split_oversized(document_text, start, end, ChunkPlan.UNIT_MAX)
                )
            else:
                unit_atoms.append((start, end))
        unit_spans = group_atoms(
            unit_atoms,
            document_text,
            maximum=ChunkPlan.UNIT_MAX,
            target=ChunkPlan.UNIT_TARGET_TOKENS,
            merge_tail=False,
            group_count=max(1, ceil(body_tokens / ChunkPlan.UNIT_TARGET_TOKENS)),
        )

        for unit_start, unit_end in unit_spans:
            chunk_records.append(
                _make_chunk(
                    document_text,
                    kind='section_unit',
                    chunk_index=chunk_index,
                    start=unit_start,
                    end=unit_end,
                    source_id=source_id,
                    doc_id=resolved_doc_id,
                    section_id=section_id,
                    section_path=section.path,
                    resolve_page=resolve_page,
                )
            )
            chunk_index += 1

            evidence_atoms: list[tuple[int, int]] = []
            for start, end in atoms:
                if unit_start <= start and end <= unit_end:
                    if estimate_tokens(document_text[start:end]) > ChunkPlan.EVIDENCE_MAX:
                        evidence_atoms.extend(
                            split_oversized(
                                document_text, start, end, ChunkPlan.EVIDENCE_MAX
                            )
                        )
                    else:
                        evidence_atoms.append((start, end))
            evidence_spans = group_atoms(
                evidence_atoms,
                document_text,
                maximum=ChunkPlan.EVIDENCE_MAX,
                target=ChunkPlan.EVIDENCE_TARGET_TOKENS,
                merge_tail=True,
            )
            for ev_start, ev_end in evidence_spans:
                chunk_records.append(
                    _make_chunk(
                        document_text,
                        kind='evidence_span',
                        chunk_index=chunk_index,
                        start=ev_start,
                        end=ev_end,
                        source_id=source_id,
                        doc_id=resolved_doc_id,
                        section_id=section_id,
                        section_path=section.path,
                        resolve_page=resolve_page,
                    )
                )
                chunk_index += 1

    return section_records, chunk_records


def _make_chunk(
    document_text: str,
    *,
    kind: Literal['evidence_span', 'section_unit'],
    chunk_index: int,
    start: int,
    end: int,
    source_id: str,
    doc_id: str,
    section_id: str,
    section_path: str,
    resolve_page: Callable[[int], int | None],
) -> RagChunkRecord:
    text = document_text[start:end]
    return RagChunkRecord(
        chunk_id=_det_id(f'{source_id}|{kind}|{chunk_index}'),
        source_id=source_id,
        doc_id=doc_id,
        section_id=section_id,
        kind=kind,
        chunk_index=chunk_index,
        text=text,
        digest=hashlib.sha256(text.encode('utf-8')).hexdigest(),
        token_count=estimate_tokens(text),
        page_start=resolve_page(start),
        page_end=resolve_page(max(start, end - 1)),
        char_start=start,
        char_end=end,
        section_path=section_path,
        index_version=RAG_INDEX_VERSION,
    )
