"""Backend-neutral durability contract for SQLite and PostgreSQL stores.

The production store must preserve these semantics independently of SQL
dialect. PostgreSQL cases are enabled by GLASSLAB_TEST_POSTGRES_DSN; the
default local suite remains SQLite-only.
"""

from __future__ import annotations

from datetime import UTC, datetime
import os
from pathlib import Path
from uuid import uuid4

import pytest

from app.postgres_store import PostgresStore
from app.schemas import (
    ActionRecord,
    AgentName,
    ApprovalStatus,
    ContextPacket,
    EventRecord,
    ExperimentMatrix,
    ExpandedJobSpec,
    IngestedDatasetRecord,
    JobRecord,
    JobStatus,
    KnowledgeChunk,
    KnowledgeSource,
    PolicyClassification,
    ResourceRequest,
    RunRecord,
    RunState,
    SourceType,
    TurnKind,
    TurnRecord,
)
from app.state_machine import validate_transition
from app.storage import ConcurrencyConflict, SqliteStore


def _id(prefix: str) -> str:
    return f'{prefix}-{uuid4().hex}'


def _run(run_id: str | None = None, *, state: RunState = RunState.CREATED) -> RunRecord:
    now = datetime.now(UTC)
    return RunRecord(
        run_id=run_id or _id('run'),
        objective='store contract test',
        state=state,
        evaluation_contract_id='example-research-v1',
        evaluation_contract_version='1.0.0',
        evaluation_contract_digest='0' * 64,
        beaker_workspace='/tmp/beaker',
        honeydew_workspace='/tmp/honeydew',
        shared_artifacts_path='/tmp/artifacts',
        reports_path='/tmp/reports',
        maximum_turns=8,
        maximum_runtime_seconds=300,
        maximum_parallel_jobs=2,
        created_at=now,
        updated_at=now,
    )


def _action(run_id: str, *, key: str | None = None) -> ActionRecord:
    return ActionRecord(
        run_id=run_id,
        proposed_by=AgentName.BEAKER,
        type='submit_experiment',
        policy_classification=PolicyClassification.HUMAN_APPROVAL,
        approval_status=ApprovalStatus.PENDING,
        reason='contract test',
        idempotency_key=key or _id('action-key'),
    )


def _job(run: RunRecord, action: ActionRecord, *, key: str | None = None) -> JobRecord:
    resources = ResourceRequest(cpu=1, memory_gib=2, gpus=0, wallclock_minutes=5)
    matrix = ExperimentMatrix(
        base_config='configs/baseline.yaml',
        variants=[{'name': 'baseline'}],
        seeds=[17],
        maximum_parallel_jobs=1,
        runner_image='ghcr.io/ccny-glasslab/test-runner:sha',
        resources=resources,
    )
    spec = ExpandedJobSpec(
        orchestrator_job_id=_id('job'),
        run_id=run.run_id,
        action_id=action.action_id,
        variant_name='baseline',
        seed=17,
        idempotency_key=key or _id('job-key'),
        base_config=matrix.base_config,
        overrides={},
        runner_image=matrix.runner_image,
        resources=resources,
        required_artifacts=[],
        evaluation_contract_id=run.evaluation_contract_id,
        evaluation_contract_version=run.evaluation_contract_version,
        evaluation_contract_digest=run.evaluation_contract_digest,
    )
    return JobRecord(
        job_id=spec.orchestrator_job_id,
        run_id=run.run_id,
        action_id=action.action_id,
        kubernetes_namespace='glasslab-v2',
        status=JobStatus.QUEUED,
        requested_resources=resources,
        evaluation_contract_id=run.evaluation_contract_id,
        evaluation_contract_version=run.evaluation_contract_version,
        evaluation_contract_digest=run.evaluation_contract_digest,
        idempotency_key=spec.idempotency_key,
        variant_name='baseline',
        seed=17,
        spec=spec,
    )


@pytest.fixture(params=['sqlite', 'postgres'])
def store(request: pytest.FixtureRequest, tmp_path: Path):
    if request.param == 'sqlite':
        return SqliteStore(str(tmp_path / 'contract.db'))
    dsn = os.getenv('GLASSLAB_TEST_POSTGRES_DSN')
    if not dsn:
        pytest.skip('GLASSLAB_TEST_POSTGRES_DSN is not configured')
    return PostgresStore(dsn)


def test_run_transitions_and_optimistic_concurrency(store) -> None:
    run = store.create_run(_run(), one_active_run=False)
    assert store.get_run(run.run_id).state is RunState.CREATED
    advanced = store.transition_run(run.run_id, RunState.PREPARING)
    assert advanced.version == run.version + 1
    with pytest.raises(ConcurrencyConflict):
        store.replace_run(run, expected_version=run.version)
    with pytest.raises(ValueError):
        validate_transition(RunState.CREATED, RunState.COMPLETE)


def test_event_sequences_are_contiguous_and_cursorable(store) -> None:
    run = store.create_run(_run(), one_active_run=False)
    first = store.append_event(run_id=run.run_id, source='test', event_type='one')
    second = store.append_event(run_id=run.run_id, source='test', event_type='two')
    assert [event.sequence_number for event in store.list_events(run.run_id)] == [1, 2, 3]
    assert [event.event_type for event in store.list_events(run.run_id, after_sequence=first.sequence_number)] == ['two']
    assert second.payload == {}


def test_actions_jobs_and_approvals_are_idempotent(store) -> None:
    run = store.create_run(_run(), one_active_run=False)
    action_key = _id('same-action')
    action = store.save_action(_action(run.run_id, key=action_key))
    duplicate = store.save_action(_action(run.run_id, key=action_key))
    assert duplicate.action_id == action.action_id
    approved = store.update_action(action.action_id, approval_status=ApprovalStatus.APPROVED, reviewer='human', reason='ok')
    with pytest.raises(ConcurrencyConflict):
        store.update_action(action.action_id, approval_status=ApprovalStatus.APPROVED, reviewer='human', reason='again')
    job_key = _id('same-job')
    job = _job(run, approved, key=job_key)
    stored, created = store.create_job_if_absent(job)
    duplicate_job, created_again = store.create_job_if_absent(job.model_copy(update={'job_id': _id('other-job')}))
    assert created is True and created_again is False
    assert duplicate_job.job_id == stored.job_id
    assert store.get_job(job.job_id).status is JobStatus.QUEUED


def test_honeydew_approval_and_execution_failure_are_durable(store) -> None:
    run = store.create_run(_run(), one_active_run=False)
    action = store.save_action(_action(run.run_id))
    reviewed = store.mark_action_honeydew_approved(action.action_id, review_turn_id=_id('turn'))
    assert reviewed.honeydew_approved is True
    assert reviewed.approval_status is ApprovalStatus.PENDING
    approved = store.update_action(action.action_id, approval_status=ApprovalStatus.APPROVED, reviewer='human', reason='approved')
    failed = store.mark_action_execution_failed(approved.action_id, reason='fake executor')
    assert failed.approval_status is ApprovalStatus.EXECUTION_FAILED


def test_retry_lineage_event_is_transactionally_visible(store) -> None:
    parent = store.create_run(_run(state=RunState.FAILED), one_active_run=False)
    child = _run(state=RunState.PREPARING)
    stored, created = store.create_terminal_retry(
        child,
        parent_run_id=parent.run_id,
        retry_key=_id('retry-key'),
        checkpoint_digest='3' * 64,
        one_active_run=False,
    )
    duplicate, created_again = store.create_terminal_retry(
        child.model_copy(update={'run_id': _id('other-child')}),
        parent_run_id=parent.run_id,
        retry_key=_id('other-retry-key'),
        checkpoint_digest='4' * 64,
        one_active_run=False,
    )

    assert created is True and created_again is False
    assert duplicate.run_id == stored.run_id == child.run_id
    assert store.get_terminal_retry_child(parent.run_id).run_id == child.run_id
    assert store.get_run(child.run_id).state is RunState.PREPARING
    parent_event = store.list_events(parent.run_id)[-1]
    child_event = store.list_events(child.run_id)[-1]
    assert parent_event.event_type == child_event.event_type == 'run.retry_created'
    assert parent_event.payload == child_event.payload == {
        'parent_run_id': parent.run_id,
        'child_run_id': child.run_id,
        'checkpoint_digest': '3' * 64,
    }


def _cancel_run(store, run: RunRecord) -> None:
    """Force a run into CANCELLED without transition validation (test seam)."""
    current = store.get_run(run.run_id)
    store.replace_run(current.model_copy(update={'state': RunState.CANCELLED}), expected_version=current.version)


def test_retry_pointer_supersedes_terminal_child(store) -> None:
    parent = store.create_run(_run(state=RunState.FAILED), one_active_run=False)
    first = _run(state=RunState.PREPARING)
    stored, created = store.create_terminal_retry(
        first,
        parent_run_id=parent.run_id,
        retry_key=_id('retry-key'),
        checkpoint_digest='3' * 64,
        one_active_run=False,
    )
    assert created is True
    _cancel_run(store, stored)
    before = store.get_run(stored.run_id).model_dump(mode='json')
    events_before = store.list_events(stored.run_id)
    parent_events_before = [e.event_type for e in store.list_events(parent.run_id)]

    replacement = _run(state=RunState.PREPARING)
    superseding, created_again = store.create_terminal_retry(
        replacement,
        parent_run_id=parent.run_id,
        retry_key=_id('retry-key'),
        checkpoint_digest='4' * 64,
        one_active_run=False,
    )

    assert created_again is True
    assert superseding.run_id == replacement.run_id
    assert superseding.run_id != stored.run_id
    assert store.get_terminal_retry_child(parent.run_id).run_id == replacement.run_id
    assert store.get_run(stored.run_id).model_dump(mode='json') == before
    superseded_events = store.list_events(stored.run_id)
    assert len(superseded_events) == len(events_before) + 1
    assert superseded_events[-1].event_type == 'run.retry_superseded'
    assert superseded_events[-1].payload['child_run_id'] == stored.run_id
    assert superseded_events[-1].payload['superseded_by'] == replacement.run_id
    parent_event_types = [e.event_type for e in store.list_events(parent.run_id)]
    assert parent_event_types.count('run.retry_created') == 2
    latest_parent_event = store.list_events(parent.run_id)[-1]
    assert latest_parent_event.payload == {
        'parent_run_id': parent.run_id,
        'child_run_id': replacement.run_id,
        'checkpoint_digest': '4' * 64,
    }
    replacement_events = [e.event_type for e in store.list_events(replacement.run_id)]
    assert replacement_events[0] == 'run.created'
    assert 'run.retry_created' in replacement_events
    assert parent_events_before.count('run.retry_created') == 1


def test_retry_returns_live_child_unchanged(store) -> None:
    parent = store.create_run(_run(state=RunState.FAILED), one_active_run=False)
    child = _run(state=RunState.PREPARING)
    store.create_terminal_retry(
        child,
        parent_run_id=parent.run_id,
        retry_key=_id('retry-key'),
        checkpoint_digest='3' * 64,
        one_active_run=False,
    )
    events_before = len(store.list_events(child.run_id))
    runs_before = {run.run_id for run in store.list_runs()}

    again, created = store.create_terminal_retry(
        child.model_copy(update={'run_id': _id('other-child')}),
        parent_run_id=parent.run_id,
        retry_key=_id('retry-key'),
        checkpoint_digest='9' * 64,
        one_active_run=False,
    )

    assert created is False
    assert again.run_id == child.run_id
    assert len(store.list_events(child.run_id)) == events_before
    assert {run.run_id for run in store.list_runs()} == runs_before


def test_retry_explicit_key_replay_after_terminal_returns_same_child(store) -> None:
    parent = store.create_run(_run(state=RunState.FAILED), one_active_run=False)
    key = _id('explicit-retry-key')
    child = _run(state=RunState.PREPARING)
    store.create_terminal_retry(
        child,
        parent_run_id=parent.run_id,
        retry_key=key,
        checkpoint_digest='3' * 64,
        one_active_run=False,
    )
    _cancel_run(store, store.get_run(child.run_id))
    events_before = len(store.list_events(child.run_id))
    runs_before = {run.run_id for run in store.list_runs()}

    replayed, created = store.create_terminal_retry(
        child.model_copy(update={'run_id': _id('other-child')}),
        parent_run_id=parent.run_id,
        retry_key=key,
        checkpoint_digest='5' * 64,
        one_active_run=False,
    )

    assert created is False
    assert replayed.run_id == child.run_id
    assert store.get_terminal_retry_child(parent.run_id).run_id == child.run_id
    assert len(store.list_events(child.run_id)) == events_before
    assert {run.run_id for run in store.list_runs()} == runs_before


def test_retry_supersede_rechecks_parent_terminal_and_slot(store) -> None:
    slot_parent = store.create_run(_run(state=RunState.FAILED), one_active_run=False)
    blocked_child = _run(state=RunState.PREPARING)
    stored, _ = store.create_terminal_retry(
        blocked_child,
        parent_run_id=slot_parent.run_id,
        retry_key=_id('retry-key'),
        checkpoint_digest='3' * 64,
        one_active_run=False,
    )
    _cancel_run(store, stored)
    foreign_active = store.create_run(_run(state=RunState.BEAKER_PLANNING), one_active_run=False)

    with pytest.raises(ConcurrencyConflict, match='active run already exists'):
        store.create_terminal_retry(
            _run(state=RunState.PREPARING),
            parent_run_id=slot_parent.run_id,
            retry_key=_id('retry-key'),
            checkpoint_digest='6' * 64,
            one_active_run=True,
        )

    state_parent = store.create_run(_run(state=RunState.FAILED), one_active_run=False)
    state_child = _run(state=RunState.PREPARING)
    stored_state_child, _ = store.create_terminal_retry(
        state_child,
        parent_run_id=state_parent.run_id,
        retry_key=_id('retry-key'),
        checkpoint_digest='7' * 64,
        one_active_run=False,
    )
    _cancel_run(store, stored_state_child)
    awakened = store.get_run(state_parent.run_id).model_copy(update={'state': RunState.BEAKER_PLANNING})
    store.replace_run(awakened, expected_version=awakened.version)

    with pytest.raises(ConcurrencyConflict, match='not terminal'):
        store.create_terminal_retry(
            _run(state=RunState.PREPARING),
            parent_run_id=state_parent.run_id,
            retry_key=_id('retry-key'),
            checkpoint_digest='8' * 64,
            one_active_run=False,
        )
    assert store.get_run(foreign_active.run_id).state is RunState.BEAKER_PLANNING


def test_knowledge_and_context_records_round_trip(store) -> None:
    run = store.create_run(_run(), one_active_run=False)
    source_digest = uuid4().hex + uuid4().hex
    source = KnowledgeSource(
        source_type=SourceType.DOCUMENTATION,
        canonical_uri='repo://docs/contract.md',
        digest=source_digest,
        run_scope=run.run_id,
    )
    store.save_knowledge_source(source)
    chunk = KnowledgeChunk(source_id=source.source_id, chunk_index=0, text='immutable contract evidence', digest='2' * 64, token_count=3)
    store.replace_knowledge_chunks(source.source_id, [chunk])
    assert store.get_knowledge_source(source.source_id).digest == source_digest
    assert store.list_knowledge_chunks(source.source_id)[0].text == chunk.text
    assert store.search_knowledge_chunks('contract evidence', source_ids=[source.source_id], limit=1)[0]['chunk_id'] == chunk.chunk_id
    packet = ContextPacket(run_id=run.run_id, agent=AgentName.HONEYDEW, turn_number=1, turn_kind=TurnKind.PROTOCOL_DRAFT, query='contract', index_version='v1', token_budget=100)
    store.save_context_packet(packet)
    assert store.list_context_packets(run.run_id)[0].packet_id == packet.packet_id


def test_knowledge_search_matches_partial_term_overlap(store) -> None:
    """Long agent-context queries must still hit chunks sharing any term.

    Retrieval queries combine turn kind, objective, and prompt prefixes into
    one string. AND-ing every token against short chunks returns nothing, so
    the lexical search treats whitespace-separated terms as alternatives.
    """
    run = store.create_run(_run(), one_active_run=False)
    source = KnowledgeSource(
        source_type=SourceType.DOCUMENTATION,
        canonical_uri='repo://docs/arbitration.md',
        digest=uuid4().hex + uuid4().hex,
        run_scope=run.run_id,
    )
    store.save_knowledge_source(source)
    chunk = KnowledgeChunk(
        source_id=source.source_id,
        chunk_index=0,
        text='wine clustering stability analysis runs inside one job',
        digest='4' * 64,
        token_count=9,
    )
    store.replace_knowledge_chunks(source.source_id, [chunk])

    long_query = (
        'revision Complete and evaluate the imported UCI Wine Multi-Algorithm '
        'Clustering benchmark revision candidate.yaml seeds matrix'
    )
    hits = store.search_knowledge_chunks(long_query, source_ids=[source.source_id], limit=5)
    assert [h['chunk_id'] for h in hits] == [chunk.chunk_id]


def test_restart_recovery_marks_running_turns_failed(store) -> None:
    run = store.create_run(_run(), one_active_run=False)
    turn = TurnRecord(run_id=run.run_id, agent=AgentName.BEAKER, status='running', created_at=datetime.now(UTC), updated_at=datetime.now(UTC))
    store.save_turn(turn)
    assert store.mark_running_turns_interrupted(run.run_id) == 1
    assert store.list_turns(run.run_id)[0].status == 'failed'
    assert store.mark_running_turns_interrupted(run.run_id) == 0
