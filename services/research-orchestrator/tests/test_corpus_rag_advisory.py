"""Advisory generation tests (network-free, lexical channel only).

Reuses the seeding style of ``test_corpus_rag_retrieval.py``: real
``SqliteStore`` fixtures, no embedding vectors, no model downloads. The
advisory is exercised through its public surface plus the ``check_advisory``
GATE CLI and the ``ask --advisory`` end-to-end path.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

from app.corpus_rag.contracts import QueryPlan, RagChunkRecord
from app.corpus_rag.retrieval import HybridRetriever, RetrievalOptions, RetrievalResult
from app.schemas import KnowledgeSource, SourceType
from app.storage import SqliteStore

_SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts' / 'corpus_rag'


def _load_script(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ask = _load_script('corpus_rag_ask', _SCRIPTS / 'ask.py')
check_advisory = _load_script('corpus_rag_check_advisory', _SCRIPTS / 'check_advisory.py')

QUESTION = 'How should I validate clustering stability with resampling?'

_REC_TEXT = (
    'We recommend consensus resampling because it is effective for cluster '
    'stability assessment across bootstrap replicates.'
)
_CRIT_TEXT = (
    'A critical limitation of consensus clustering is bias introduced by '
    'resampling artifacts in noisy datasets.'
)


def _source(uri: str) -> KnowledgeSource:
    return KnowledgeSource(
        source_type=SourceType.DOCUMENTATION,
        canonical_uri=uri,
        digest=hashlib.sha256(uri.encode()).hexdigest(),
    )


def _chunk(source_id: str, index: int, text: str) -> RagChunkRecord:
    return RagChunkRecord(
        chunk_id=f'{source_id}::c{index}',
        source_id=source_id,
        kind='evidence_span',
        chunk_index=index,
        text=text,
        digest=hashlib.sha256(text.encode()).hexdigest(),
        token_count=max(1, len(text.split())),
        section_path='methods.stability',
    )


def _seed(tmp_path: Path, name: str, texts: dict[str, list[str]]) -> Path:
    """Seed one source per key; returns the SQLite path."""
    db = tmp_path / name
    store = SqliteStore(str(db))
    for uri, chunk_texts in texts.items():
        src = _source(uri)
        store.save_knowledge_source(src)
        store.replace_rag_chunks(
            src.source_id,
            [_chunk(src.source_id, i, t) for i, t in enumerate(chunk_texts)],
        )
    return db


def _retrieve(db: Path) -> RetrievalResult:
    retriever = HybridRetriever(SqliteStore(str(db)))
    return retriever.retrieve(
        QUESTION, source_ids=None, options=RetrievalOptions(mode='lexical', k_final=8)
    )


def test_advisory_validates_and_cites(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from app.corpus_rag.advisory import build_method_advisory
    from app.corpus_rag.contracts import MethodAdvisory

    db = _seed(tmp_path, 'cites.db', {'rec://a': [_REC_TEXT]})
    result = _retrieve(db)
    assert result.hits

    advisory = build_method_advisory(
        objective=QUESTION, corpus_slug='test-corpus', retrieval=result,
        store=SqliteStore(str(db)), llm=None,
    )
    assert isinstance(advisory, MethodAdvisory)
    assert advisory.candidates
    assert all(candidate.citations for candidate in advisory.candidates)

    out = tmp_path / 'advisory.json'
    out.write_text(json.dumps(advisory.model_dump(mode='json')))
    rc = check_advisory.main(['--advisory-json', str(out), '--store', str(db)])
    assert rc == 0
    assert '"valid": true' in capsys.readouterr().out


def test_contradiction_pair_surfaced(tmp_path: Path) -> None:
    from app.corpus_rag.advisory import build_method_advisory
    from app.corpus_rag.contracts import MethodAdvisory

    db = _seed(
        tmp_path, 'contra.db',
        {'rec://pro': [_REC_TEXT], 'rec://con': [_CRIT_TEXT]},
    )
    advisory = build_method_advisory(
        objective=QUESTION, corpus_slug='test-corpus', retrieval=_retrieve(db),
        store=SqliteStore(str(db)), llm=None,
    )
    assert isinstance(advisory, MethodAdvisory)
    assert advisory.contradiction_pairs
    pair = advisory.contradiction_pairs[0]
    assert 'clustering' in pair['topic'].lower()
    assert pair['a'] and pair['b'] and pair['a'] != pair['b']


def test_insufficient_on_empty_retrieval(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from app.corpus_rag.advisory import build_method_advisory, render_markdown
    from app.corpus_rag.contracts import InsufficientCorpusAdvisory

    plan = QueryPlan(original_query=QUESTION, subqueries=[QUESTION], planner_mode='heuristic')
    empty = RetrievalResult(hits=[], plan=plan, citations=[], timings={})
    advisory = build_method_advisory(
        objective=QUESTION, corpus_slug='empty-corpus', retrieval=empty, store=None, llm=None,
    )
    assert isinstance(advisory, InsufficientCorpusAdvisory)

    markdown = render_markdown(advisory)
    assert advisory.reason in markdown

    out = tmp_path / 'insufficient.json'
    out.write_text(json.dumps(advisory.model_dump(mode='json')))
    rc = check_advisory.main(['--advisory-json', str(out), '--store', str(tmp_path / 'unused.db')])
    assert rc == 0
    assert '"valid": true' in capsys.readouterr().out


def test_ask_cli_advisory_flag_end_to_end(tmp_path: Path) -> None:
    db = _seed(tmp_path, 'cli.db', {'rec://a': [_REC_TEXT]})
    out = tmp_path / 'ask.json'
    rc = ask.main([
        '--store', str(db), '--question', QUESTION, '--mode', 'lexical',
        '--advisory', '--json-out', str(out),
    ])
    assert rc == 0
    payload = json.loads(out.read_text())
    assert payload['advisory']['kind'] == 'method_advisory'
    assert 'CITATIONS' in payload['advisory_markdown']
