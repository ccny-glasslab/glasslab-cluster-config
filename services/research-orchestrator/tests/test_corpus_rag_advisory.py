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

from app.corpus_rag.advisory import build_method_advisory
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


def test_extractive_candidates_require_multi_hit_family_evidence() -> None:
    from types import SimpleNamespace

    from app.corpus_rag import RagChunkRecord, RetrievedHit

    def hit(cid: str, sid: str, text: str) -> RetrievedHit:
        return RetrievedHit(
            chunk=RagChunkRecord(
                source_id=sid,
                kind='evidence_span',
                chunk_index=0,
                text=text,
                digest='a' * 64,
                token_count=max(1, len(text.split())),
            ),
            score=1.0,
            stage_scores={},
            dense_rank=None,
            lexical_rank=None,
            rerank_score=None,
        )

    hits = [
        hit('c1', 's1', 'Clustering stability assessment via resampling: bootstrap stability of k-means under initialization perturbations.'),
        hit('c2', 's2', 'Consensus clustering critique: resampling-based stability claims can be unstable across initializations and random permutations; assumptions behind consensus deserve scrutiny.'),
        hit('c3', 's3', 'A passage that merely mentions false discovery rate once when surveying hypotheses.'),
    ]
    advisory = build_method_advisory(
        objective='clustering stability assessment',
        corpus_slug='t',
        retrieval=SimpleNamespace(hits=hits),
        store=None,
    )
    labels = [candidate.method_name for candidate in advisory.candidates]
    assert 'Resampling-based clustering validation' in labels
    # Single-keyword coincidences in one chunk must not spawn families.
    assert 'False-discovery-rate control' not in labels
    assert 'Assumption-light regression alternatives' not in labels


def _fake_knowledge_manager(store: SqliteStore) -> Any:
    """Minimal KnowledgeManager stand-in: real store, lexical-only retrieve."""
    from app.schemas import ContextPacket

    class _FakeKnowledgeManager:
        def __init__(self, store: SqliteStore) -> None:
            self.store = store
            self.dense_index = None

        def retrieve(self, **kwargs: Any) -> ContextPacket:
            chunk_ids = [
                chunk.chunk_id
                for source in store.list_knowledge_sources()
                for chunk in store.list_knowledge_chunks(source.source_id)
            ]
            return ContextPacket(
                run_id=kwargs['run_id'],
                agent='honeydew',
                turn_number=kwargs['turn_number'],
                turn_kind=kwargs['turn_kind'],
                query=kwargs['query'],
                index_version='test',
                ranked_sources=[
                    {
                        'kind': 'chunk',
                        'entry_id': cid,
                        'source_id': cid.split('::')[0],
                    }
                    for cid in chunk_ids
                ],
                token_budget=10000,
                retrieval_mode_actual='lexical',
            )

    return _FakeKnowledgeManager(store)


def _run_record(run_id: str) -> Any:
    from app.schemas import RunRecord, RunState, utc_now

    return RunRecord(
        run_id=run_id,
        objective=QUESTION,
        state=RunState.CREATED,
        evaluation_contract_id='example-research-v1',
        evaluation_contract_version='1.0.0',
        evaluation_contract_digest='a' * 64,
        beaker_workspace='/tmp/b',
        honeydew_workspace='/tmp/h',
        shared_artifacts_path='/tmp/s',
        reports_path='/tmp/r',
        maximum_turns=10,
        maximum_runtime_seconds=3600,
        maximum_parallel_jobs=1,
        created_at=utc_now(),
        updated_at=utc_now(),
    )


def _seed_knowledge(db: Path, texts: list[str]) -> None:
    """Seed one knowledge source with the given chunk texts."""
    from app.schemas import KnowledgeChunk

    store = SqliteStore(str(db))
    uri = 'rec://knowledge'
    src = _source(uri)
    src.source_id = 'deterministic-source'
    store.save_knowledge_source(src)
    store.replace_knowledge_chunks(
        src.source_id,
        [
            KnowledgeChunk(
                chunk_id=f'{src.source_id}::k{index}',
                source_id=src.source_id,
                chunk_index=index,
                text=text,
                digest=hashlib.sha256(text.encode()).hexdigest(),
                token_count=max(1, len(text.split())),
            )
            for index, text in enumerate(texts)
        ],
    )


def _build_advisory(db: Path, run_id: str) -> tuple[SqliteStore, dict[str, Any]]:
    from app.method_advisor import MethodAdvisor

    store = SqliteStore(str(db))
    store.create_run(_run_record(run_id), one_active_run=False)
    advisor = MethodAdvisor(_fake_knowledge_manager(store))
    rendered, payload = advisor.build_and_render(
        run_id=run_id,
        objective=QUESTION,
        turn_number=1,
        turn_kind='protocol_draft',
    )
    assert rendered and payload
    return store, payload


def test_advisory_persisted_as_durable_event_with_payload(tmp_path: Path) -> None:
    from app.method_advisor import ADVICE_GENERATED_EVENT

    db = tmp_path / 'durable.db'
    _seed_knowledge(db, [_REC_TEXT])
    store, payload = _build_advisory(db, 'run-durable')

    events = [
        event for event in store.list_events('run-durable')
        if event.event_type == ADVICE_GENERATED_EVENT
    ]
    assert len(events) == 1
    durable = events[0].payload
    assert durable['advisory_digest'] == payload['advisory_digest']
    assert durable['kind'] == 'method_advisory'
    assert durable['candidates'] == payload['candidates']
    assert durable['citations_all'] == payload['citations_all']


def test_advisory_retrievable_after_the_fact(tmp_path: Path) -> None:
    from app.method_advisor import ADVICE_GENERATED_EVENT

    db = tmp_path / 'retrieve.db'
    _seed_knowledge(db, [_REC_TEXT])
    _store, payload = _build_advisory(db, 'run-retrieve')

    # A fresh connection (new process) still sees the advisory: durable, not
    # ephemeral in-memory state.
    fresh = SqliteStore(str(db))
    events = [
        event for event in fresh.list_events('run-retrieve')
        if event.event_type == ADVICE_GENERATED_EVENT
    ]
    assert len(events) == 1
    assert events[0].payload['advisory_digest'] == payload['advisory_digest']
    assert events[0].payload['candidates']


def _advisory_content(payload: dict[str, Any]) -> dict[str, Any]:
    """Advisory content minus the per-run random packet_id and its digest."""
    content = {
        key: value
        for key, value in payload.items()
        if key not in ('advisory_digest', 'packet_id')
    }
    content['retrieval_metadata'] = {
        key: value
        for key, value in payload['retrieval_metadata'].items()
        if key != 'packet_id'
    }
    return content


def test_advisory_generation_is_deterministic(tmp_path: Path) -> None:
    db_a = tmp_path / 'det-a.db'
    _seed_knowledge(db_a, [_REC_TEXT])
    db_b = tmp_path / 'det-b.db'
    _seed_knowledge(db_b, [_REC_TEXT])
    _store_a, payload_a = _build_advisory(db_a, 'run-det-a')
    _store_b, payload_b = _build_advisory(db_b, 'run-det-b')

    assert _advisory_content(payload_a) == _advisory_content(payload_b)
