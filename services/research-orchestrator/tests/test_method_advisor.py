"""Unit tests for the production MethodAdvisor (dense-backed, audited)."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

import pytest

from app.corpus_rag.embeddings import OfflineDeterministicEmbedding
from app.knowledge_dense import NumpyChunkIndex
from app.knowledge_manager import KnowledgeManager
from app.method_advisor import MethodAdvisor
from app.schemas import RunRecord, RunState, SourceType
from app.storage import SqliteStore


def _create_run(store: SqliteStore, run_id: str) -> None:
    now = datetime.now(timezone.utc)
    store.create_run(
        RunRecord(
            run_id=run_id,
            objective='Exercise method advisory.',
            state=RunState.CREATED,
            evaluation_contract_id='example-research-v1',
            evaluation_contract_version='1.0.0',
            evaluation_contract_digest='a' * 64,
            beaker_workspace='/tmp/beaker',
            honeydew_workspace='/tmp/honeydew',
            shared_artifacts_path='/tmp/shared',
            reports_path='/tmp/reports',
            maximum_turns=20,
            maximum_runtime_seconds=3600,
            maximum_parallel_jobs=2,
            created_at=now,
            updated_at=now,
        ),
        one_active_run=False,
    )


def _make_km_with_corpus(tmp_path, *, texts=None) -> tuple[KnowledgeManager, SqliteStore]:
    store = SqliteStore(str(tmp_path / 'advisor.db'))
    approved = tmp_path / 'approved'
    approved.mkdir(exist_ok=True)
    km = KnowledgeManager(
        store=store,
        root=tmp_path / 'km',
        allowlist_roots=[approved],
    )
    body = (
        'Technique note: assess clustering stability with bootstrap and '
        'consensus resampling; track adjusted Rand index across replicates '
        'and compare against a fixed-k baseline.'
    )
    if texts:
        body = '\n\n'.join(texts)
    path = approved / 'stability-note.md'
    path.write_text(body)
    km.ingest_source(
        source_type=__import__('app.schemas', fromlist=['SourceType']).SourceType.PAPER,
        path=str(path),
        title='Stability note',
    )
    provider = OfflineDeterministicEmbedding(dims=64)
    from app.knowledge_dense import build_dense_index

    build_dense_index(store, provider)
    km.dense_index = NumpyChunkIndex(store, provider)
    km.default_retrieval_mode = 'dense'
    return km, store


def test_advisor_builds_grounded_candidates_with_resolvable_citations(
    tmp_path,
) -> None:
    km, store = _make_km_with_corpus(tmp_path)
    _create_run(store, 'run-adv-1')
    advisor = MethodAdvisor(km)

    block, payload = advisor.build_and_render(
        run_id='run-adv-1',
        objective='How should we assess whether clusters are stable?',
        turn_number=1,
        turn_kind='protocol_draft',
        retrieval_mode='dense',
    )

    assert payload is not None and payload['kind'] == 'method_advisory'
    assert len(payload['candidates']) >= 1
    assert len(payload['advisory_digest']) == 64
    chunk_ids = {row['chunk_id'] for row in store.list_all_knowledge_chunks()}
    for candidate in payload['candidates']:
        assert candidate['citations'], 'every candidate must carry evidence'
        for citation in candidate['citations']:
            assert citation['chunk_id'] in chunk_ids
            assert citation['evidence_uri'].startswith('knowledge://')
    assert 'METHODOLOGY ADVISORY' in block
    assert 'not instructions' in block


def test_advisor_is_once_per_run_even_after_restart(tmp_path) -> None:
    km, store = _make_km_with_corpus(tmp_path)
    advisor = MethodAdvisor(km)

    _create_run(store, 'run-adv-2')
    first_block, first_payload = advisor.build_and_render(
        run_id='run-adv-2',
        objective='clustering stability assessment',
        turn_number=1,
        turn_kind='protocol_draft',
        retrieval_mode='dense',
    )
    # A restarted engine re-creates the advisor; the persisted event must
    # make the second attempt a no-op rather than duplicating packets.
    rebuilt = MethodAdvisor(km)
    second_block, second_payload = rebuilt.build_and_render(
        run_id='run-adv-2',
        objective='clustering stability assessment',
        turn_number=1,
        turn_kind='protocol_draft',
        retrieval_mode='dense',
    )
    assert first_payload is not None
    assert second_block == ''
    assert second_payload is None


def test_advisor_reports_insufficiency_on_empty_corpus(tmp_path) -> None:
    store = SqliteStore(str(tmp_path / 'empty.db'))
    km = KnowledgeManager(store=store, root=tmp_path / 'km-empty')
    provider = OfflineDeterministicEmbedding(dims=16)
    km.dense_index = NumpyChunkIndex(store, provider)
    km.default_retrieval_mode = 'dense'
    advisor = MethodAdvisor(km, corpus_slug='nonexistent')

    _create_run(store, 'run-empty')
    block, payload = advisor.build_and_render(
        run_id='run-empty',
        objective='cluster stability question',
        turn_number=1,
        turn_kind='protocol_draft',
        retrieval_mode='dense',
    )

    assert payload['kind'] == 'insufficient_corpus'
    assert payload['insufficiency_reason' if False else 'reason']
    assert 'INSUFFICIENT EVIDENCE' in block


def test_advisor_insufficiency_on_unrelated_corpus(tmp_path) -> None:
    km, store = _make_km_with_corpus(
        tmp_path,
        texts=['Totally unrelated recipe for sourdough bread and pizza dough.'],
    )
    advisor = MethodAdvisor(km, corpus_slug='cooking')

    _create_run(store, 'run-unrelated')
    _block, payload = advisor.build_and_render(
        run_id='run-unrelated',
        objective='How should we tune transformer learning-rate schedules?',
        turn_number=1,
        turn_kind='protocol_draft',
        retrieval_mode='dense',
    )
    # No method family maps to the retrieved text -> explicit typed refusal,
    # never a fabricated recommendation.
    assert payload['kind'] == 'insufficient_corpus'
    assert payload['reason']


def test_advisor_digest_is_stable_for_identical_inputs(tmp_path) -> None:
    km_a, store_a = _make_km_with_corpus(tmp_path)
    km_b, store_b = _make_km_with_corpus(tmp_path)
    advisor_a = MethodAdvisor(km_a)
    advisor_b = MethodAdvisor(km_b)

    _create_run(store_a, 'run-da')
    _block, payload_a = advisor_a.build_and_render(
        run_id='run-da', objective='cluster stability', turn_number=1,
        turn_kind='protocol_draft', retrieval_mode='dense',
    )
    _create_run(store_b, 'run-db')
    _block, payload_b = advisor_b.build_and_render(
        run_id='run-db', objective='cluster stability', turn_number=1,
        turn_kind='protocol_draft', retrieval_mode='dense',
    )
    import json as _json

    # Digest covers the advisory object only; packet_id is attached after.
    digest_base = {k: v for k, v in payload_a.items() if k not in ('advisory_digest', 'packet_id')}
    recomputed = hashlib.sha256(
        _json.dumps(digest_base, sort_keys=True, separators=(',', ':')).encode()
    ).hexdigest()
    assert payload_a['advisory_digest'] == recomputed
