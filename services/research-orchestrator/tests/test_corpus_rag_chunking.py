"""Two-tier structure-aware chunking tests.

Pins the public surface of ``app/corpus_rag/chunking.py``: token estimation
semantics mirrored from ``knowledge_manager.estimate_tokens``, tier budgets,
the character-slice invariant (``document_text[char_start:char_end] ==
chunk.text``), parent/child containment between section_units and
evidence_spans, and deterministic record ids across rebuilds.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from app.corpus_rag.chunking import (
    ChunkPlan,
    build_chunks,
    estimate_tokens,
)
from app.corpus_rag.contracts import RAG_INDEX_VERSION


@dataclass(frozen=True)
class FakeSection:
    """Minimal structural duck-type of documents.SectionNode."""

    path: str
    title: str | None
    level: int
    start_char: int
    end_char: int


def _sent(i: int, n_words: int = 16) -> str:
    body = ' '.join(f'w{i}k{j}' for j in range(n_words - 1))
    return f'{body} end{i}.'


def _page_for_char_factory(length: int):
    def page_for_char(char: int) -> int | None:
        if 0 <= char < length:
            return char // 1000 + 1
        return None

    return page_for_char


def _build_doc(bodies: list[tuple[str, str | None, int, str]]):
    parts: list[str] = []
    sections: list[FakeSection] = []
    pos = 0
    for path, title, level, body in bodies:
        parts.append(body)
        sections.append(FakeSection(path, title, level, pos, pos + len(body)))
        parts.append('\n\n')
        pos += len(body) + 2
    text = ''.join(parts[:-1])
    return text, sections


def test_estimate_tokens_matches_knowledge_manager_semantics():
    assert estimate_tokens('') == 1
    assert estimate_tokens('   \n\t ') == 1
    assert estimate_tokens('one two three') == 3


def test_two_tier_bounds_parents_and_offsets():
    small = 'Alpha beta gamma delta epsilon zeta.'
    long_body = ' '.join(_sent(i) for i in range(90))  # 90 * 16 = 1440 tokens
    medium = ' '.join(_sent(1000 + i, 12) for i in range(20))  # 240 tokens
    text, sections = _build_doc(
        [('1', 'Intro', 1, small), ('2', 'Long', 1, long_body), ('3', 'Mid', 2, medium)]
    )
    assert estimate_tokens(long_body) > 1300
    page_for_char = _page_for_char_factory(len(text))

    sections_out, chunks = build_chunks(
        text, sections, source_id='src-1', doc_id='doc-1',
        page_for_char=page_for_char,
    )
    units = [c for c in chunks if c.kind == 'section_unit']
    evidence = [c for c in chunks if c.kind == 'evidence_span']

    # Hard invariant: every chunk's text is the exact document slice.
    for chunk in chunks:
        assert chunk.char_start is not None and chunk.char_end is not None
        assert text[chunk.char_start:chunk.char_end] == chunk.text

    # Digests, token counts, provenance fields, deterministic ids.
    for chunk in chunks:
        assert chunk.digest == hashlib.sha256(chunk.text.encode('utf-8')).hexdigest()
        assert chunk.token_count == estimate_tokens(chunk.text)
        assert chunk.index_version == RAG_INDEX_VERSION
        assert chunk.source_id == 'src-1' and chunk.doc_id == 'doc-1'
        expected_id = hashlib.sha256(
            f'src-1|{chunk.kind}|{chunk.chunk_index}'.encode()
        ).hexdigest()[:32]
        assert chunk.chunk_id == expected_id
        assert chunk.page_start == page_for_char(chunk.char_start)
        assert chunk.page_end == page_for_char(chunk.char_end - 1)
        assert chunk.section_path is not None
        assert chunk.section_id is not None

    # Global monotonic chunk_index.
    assert [c.chunk_index for c in chunks] == list(range(len(chunks)))

    # Section records preserve identity and resolve pages.
    assert [s.path for s in sections_out] == ['1', '2', '3']
    for sec_in, sec_out in zip(sections, sections_out):
        assert sec_out.title == sec_in.title
        assert sec_out.level == sec_in.level
        assert sec_out.section_id == hashlib.sha256(
            f'src-1|{sec_in.path}'.encode()
        ).hexdigest()[:32]
        assert sec_out.page_start == page_for_char(sec_in.start_char)
        assert sec_out.page_end == page_for_char(sec_in.end_char - 1)

    # Tier structure: short sections -> one unit; long -> ceil(1440/1200) = 2.
    units_by_path: dict[str, list] = {}
    for unit in units:
        units_by_path.setdefault(unit.section_path, []).append(unit)
    assert len(units_by_path['1']) == 1
    assert len(units_by_path['2']) == 2
    assert len(units_by_path['3']) == 1
    for unit in units:
        assert unit.token_count <= ChunkPlan.UNIT_MAX

    # Union of unit spans covers each section body contiguously.
    for sec in sections:
        us = sorted(units_by_path[sec.path], key=lambda u: u.char_start)
        assert us[0].char_start == sec.start_char
        assert us[-1].char_end == sec.end_char
        for a, b in zip(us, us[1:]):
            assert a.char_end <= b.char_start
            assert text[a.char_end:b.char_start].strip() == ''

    # Each evidence span sits in exactly one unit of its own section.
    for ev in evidence:
        containers = [
            u for u in units
            if u.section_path == ev.section_path
            and u.char_start <= ev.char_start
            and ev.char_end <= u.char_end
        ]
        assert len(containers) == 1

    # Evidence bounds: interior spans within [MIN, MAX]; only a unit's final
    # span may be a merged tail outside that band.
    for unit in units:
        spans = sorted(
            (
                e for e in evidence
                if e.section_path == unit.section_path
                and unit.char_start <= e.char_start
                and e.char_end <= unit.char_end
            ),
            key=lambda e: e.char_start,
        )
        assert spans
        for span in spans[:-1]:
            assert ChunkPlan.EVIDENCE_MIN <= span.token_count <= ChunkPlan.EVIDENCE_MAX
        assert spans[0].char_start == unit.char_start
        assert spans[-1].char_end == unit.char_end
        for a, b in zip(spans, spans[1:]):
            assert a.char_end <= b.char_start
            assert text[a.char_end:b.char_start].strip() == ''

    # Determinism: identical inputs rebuild identical records.
    sections_out2, chunks2 = build_chunks(
        text, sections, source_id='src-1', doc_id='doc-1',
        page_for_char=page_for_char,
    )
    assert [s.model_dump() for s in sections_out2] == [s.model_dump() for s in sections_out]
    assert [c.model_dump() for c in chunks2] == [c.model_dump() for c in chunks]


def test_sentence_boundary_splitting_prefers_sentences():
    body = ' '.join(_sent(i) for i in range(90))
    text, sections = _build_doc([('9', 'Long', 1, body)])
    _, chunks = build_chunks(text, sections, source_id='src-2')
    units = sorted(
        (c for c in chunks if c.kind == 'section_unit'), key=lambda c: c.char_start
    )
    assert len(units) >= 2
    for unit in units:
        assert unit.token_count <= ChunkPlan.UNIT_MAX
        # Every unit breaks at a sentence end (the body itself ends in '.').
        assert unit.text[-1] in '.!?'
    for a, b in zip(units, units[1:]):
        assert text[a.char_end:b.char_start].strip() == ''
    # No resolver passed: pages stay unset.
    assert all(c.page_start is None and c.page_end is None for c in chunks)


def test_short_section_single_unit_and_tail_merge():
    body = 'Alpha beta gamma delta epsilon.'
    text, sections = _build_doc([('1', 'Tiny', 1, body)])
    sections_out, chunks = build_chunks(text, sections, source_id='src-3', doc_id='doc-3')
    units = [c for c in chunks if c.kind == 'section_unit']
    evidence = [c for c in chunks if c.kind == 'evidence_span']
    assert len(units) == 1
    assert len(evidence) == 1
    unit, ev = units[0], evidence[0]
    assert (unit.char_start, unit.char_end) == (
        sections[0].start_char, sections[0].end_char,
    )
    assert (ev.char_start, ev.char_end) == (unit.char_start, unit.char_end)
    assert ev.text == text
    assert ev.section_path == unit.section_path == '1'
    assert ev.section_id == unit.section_id
    assert sections_out[0].doc_id == 'doc-3'
