"""Evaluation-asset contract tests for the corpus-RAG prototype.

Pins manifest/questions/qrels/rubric integrity: every row parses into a
frozen contracts model, graded keys reference non-skipped manifest ids, and
the TSV qrels stay in exact mechanical agreement with questions.jsonl.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.corpus_rag import BenchmarkQuestion, CorpusManifestEntry

ASSET_DIR = Path(__file__).resolve().parents[1] / 'eval' / 'corpus_rag'

HEX64 = set('0123456789abcdef')


def _manifest() -> list[CorpusManifestEntry]:
    lines = (ASSET_DIR / 'manifest.jsonl').read_text().splitlines()
    return [CorpusManifestEntry.model_validate(json.loads(line)) for line in lines if line.strip()]


def _questions() -> list[BenchmarkQuestion]:
    lines = (ASSET_DIR / 'questions.jsonl').read_text().splitlines()
    return [BenchmarkQuestion.model_validate(json.loads(line)) for line in lines if line.strip()]


def test_manifest_parses_and_is_complete() -> None:
    entries = _manifest()
    assert len(entries) >= 19
    skipped = [entry for entry in entries if entry.skip]
    assert len(skipped) == 4
    assert {entry.skip_reason for entry in skipped} == {'paywalled', 'image-only scan'}
    for entry in entries:
        assert entry.url.startswith(('https://', 'http://'))
        if not entry.skip:
            assert entry.sha256 is not None
            assert len(entry.sha256) == 64
            assert set(entry.sha256) <= HEX64


def test_questions_parse_with_unique_ids_and_grade2_evidence() -> None:
    entries = _manifest()
    manifest_ids = {entry.id for entry in entries}
    non_skipped = {entry.id for entry in entries if not entry.skip}

    questions = _questions()
    assert len(questions) >= 8
    qids = [question.qid for question in questions]
    assert len(qids) == len(set(qids))

    for question in questions:
        grades = list(question.graded_relevance.values())
        assert grades, f'{question.qid} has no graded relevance'
        assert max(grades) == 2, f'{question.qid} lacks directly-relevant evidence'
        assert all(0 <= grade <= 2 for grade in grades)
        for key in question.graded_relevance:
            assert key in manifest_ids, f'{question.qid} references unknown id {key}'
            assert key in non_skipped, f'{question.qid} references skipped id {key}'


def test_qrels_tsv_matches_questions_exactly() -> None:
    questions = {question.qid: question for question in _questions()}
    rows: dict[tuple[str, str], int] = {}
    lines = (ASSET_DIR / 'qrels.tsv').read_text().splitlines()
    header = lines[0].split('\t')
    assert header == ['qid', 'key', 'grade']

    for line in lines[1:]:
        qid, key, grade = line.split('\t')
        rows[(qid, key)] = int(grade)

    expected: dict[tuple[str, str], int] = {}
    for question in questions.values():
        for key, grade in question.graded_relevance.items():
            expected[(question.qid, key)] = grade
    assert rows == expected
    assert all(0 <= grade <= 2 for grade in rows.values())


def test_rubric_documents_scale_and_convention() -> None:
    text = (ASSET_DIR / 'rubric.md').read_text()
    for dimension in (
        'groundedness',
        'citation validity',
        'methodological relevance',
        'candidate diversity',
        'assumptions surfaced',
        'failure modes surfaced',
        'overreach',
        'experiment-matrix usefulness',
    ):
        assert dimension in text.lower(), f'rubric missing {dimension}'
    assert 'manifest' in text.lower(), 'rubric must document the qrels key convention'


@pytest.mark.parametrize(
    'filename', ['manifest.jsonl', 'questions.jsonl', 'qrels.tsv', 'rubric.md']
)
def test_asset_files_exist(filename: str) -> None:
    assert (ASSET_DIR / filename).is_file()
