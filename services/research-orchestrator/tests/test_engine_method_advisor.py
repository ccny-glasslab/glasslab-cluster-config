"""Engine integration: Honeydew protocol turns receive the method advisory.

Drives a REAL ResearchOrchestrator end-to-end with the scripted mock runtime
(same harness as app/smoke.py) over an offline deterministic embedding index,
then asserts the authoritative provenance trail:

1. Honeydew's protocol drafting turn triggers exactly one
   ``agent.method_advisory_built`` event whose packet_id resolves to a
   persisted ContextPacket containing role-approved chunks only.
2. The advisory block was attached to the turn
   (``agent.method_advisory_attached``).
3. Only Honeydew's eligible phases (protocol_draft, methodology_review)
   ever build advisories — Beaker gets none.
4. A dense-backend outage degrades to lexical and the run still completes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.cluster import FakeClusterExecutor
from app.contract_candidates import ContractCandidateManager
from app.contracts import EvaluationContractResolver
from app.discord_adapter import DisabledDiscordAdapter
from app.engine import ResearchOrchestrator
from app.mock_runtime import ScriptedMockRuntime
from app.policy import ActionPolicy
from app.smoke import RUNNER_IMAGE, _create_repo
from app.storage import SqliteStore
from app.workspaces import WorkspaceManager
from app.schemas import (
    AgentName,
    AgentTurnResult,
    ApprovalStatus,
    ResearchAnswer,
    RunCreateRequest,
    RunRecord,
    RunState,
    SourceType,
    TurnKind,
    TurnRecord,
    utc_now,
)

SERVICE_ROOT = Path(__file__).resolve().parents[1]


def _build(tmp_path: Path, *, stability_text: str) -> tuple[
    ResearchOrchestrator, SqliteStore, Path,
]:
    root = tmp_path / 'engine'
    root.mkdir(parents=True, exist_ok=True)
    repo = _create_repo(root)
    approved = root / 'knowledge-approved'
    approved.mkdir(exist_ok=True)
    (approved / 'stability-note.md').write_text(stability_text)

    settings = _SettingsFactory()(root=root, repo=repo, approved=approved)
    store = SqliteStore(settings.database_path)
    engine = ResearchOrchestrator(
        settings=settings,
        store=store,
        runtime=ScriptedMockRuntime(runner_image=RUNNER_IMAGE),
        workspaces=WorkspaceManager(
            workspace_root=settings.workspace_root,
            approved_repo_path=settings.approved_repo_path,
            approved_repo_ref=settings.approved_repo_ref,
        ),
        contracts=EvaluationContractResolver(
            settings.promoted_contract_root,
            fallback_roots=[settings.evaluation_contract_root],
        ),
        contract_candidates=ContractCandidateManager(
            sealed_root=settings.sealed_contract_candidate_root,
            promoted_root=settings.promoted_contract_root,
            catalog_path=settings.trusted_contract_catalog_path,
            shared_mount_root=settings.shared_mount_root,
        ),
        policy=ActionPolicy(
            permitted_images=settings.permitted_job_images,
            maximum_cpu=settings.maximum_cpu,
            maximum_memory_gib=settings.maximum_memory_gib,
            maximum_gpus=settings.maximum_gpus,
            maximum_parallel_jobs=settings.maximum_parallel_jobs,
        ),
        cluster=FakeClusterExecutor(),
        discord=DisabledDiscordAdapter(),
    )
    engine.knowledge.ingest_source(
        source_type=SourceType.PAPER,
        path=str(approved / 'stability-note.md'),
        title='Cluster stability note',
    )
    return engine, store, approved


class _SettingsFactory:
    """Builds smoke-equivalent Settings with advisory knobs enabled."""

    def __call__(self, *, root: Path, repo: Path, approved: Path):
        from app.config import Settings

        return Settings(
            database_path=str(root / 'orchestrator.db'),
            workspace_root=str(root / 'runs'),
            artifact_root=str(root / 'artifacts'),
            approved_repo_path=str(repo),
            approved_repo_ref='main',
            evaluation_contract_root=str(SERVICE_ROOT / 'evaluation-contracts'),
            permitted_job_images=[RUNNER_IMAGE],
            cluster_execution_mode='fake',
            promoted_contract_root=str(root / 'trusted-contracts'),
            sealed_contract_candidate_root=str(root / 'contract-candidates'),
            trusted_contract_catalog_path=str(
                root / 'trusted-contracts' / 'catalog.json'
            ),
            shared_mount_root=str(root),
            task_bundle_root=str(root / 'task-bundles'),
            task_asset_root=str(root / 'task-assets'),
            dataset_upload_root=str(root / 'dataset-uploads'),
            benchmark_dataset_catalog_path=str(root / 'datasets' / 'catalog.json'),
            knowledge_root=str(root / 'knowledge'),
            knowledge_allowlist_roots=[str(approved)],
            one_active_run=True,
            maximum_parallel_jobs=2,
            knowledge_advisory_enabled=True,
            knowledge_dense_mode='dense',
            knowledge_embedding_model='offline-deterministic',
        )


STABILITY_TEXT = (
    'Assess cluster stability with bootstrap and consensus resampling. '
    'Track adjusted Rand index across replicates and compare k selection '
    'against a fixed-k baseline before trusting any clustering.'
)


def _built_events(store):
    return [
        event for event in store.list_events(_current_run_id(store))
        if event.event_type == 'agent.method_advisory_built'
    ]


def _current_run_id(store) -> str:
    runs = store.list_runs()
    assert runs, 'expected at least one run'
    return runs[-1].run_id


def test_honeydew_protocol_turn_receives_audited_advisory(tmp_path: Path) -> None:
    engine, store, _approved = _build(
        tmp_path,
        stability_text=(
            STABILITY_TEXT + ' Resampling artifacts can introduce bias; treat '
            'unstable solutions as failures requiring re-examination.'
        ),
    )
    run = engine.create_run(
        RunCreateRequest(objective='Prove advisory-grounded protocol drafting.')
    )
    assert run.state == RunState.AWAITING_PROTOCOL_APPROVAL

    built = [
        event for event in store.list_events(run.run_id)
        if event.event_type == 'agent.method_advisory_built'
    ]
    assert len(built) == 1
    payload = built[0].payload
    assert len(payload['advisory_digest']) == 64

    packet = store.get_context_packet(payload['packet_id'])
    assert packet.ranked_sources, 'advisory packet must contain ranked evidence'
    assert packet.exact_text_supplied  # bounded context actually delivered

    attached = [
        event for event in store.list_events(run.run_id)
        if event.event_type == 'agent.method_advisory_attached'
    ]
    assert len(attached) == 1
    assert attached[0].payload['advisory_digest'] == payload['advisory_digest']

    # Citations embedded in the packet resolve mechanically.
    chunk_ids = {entry['entry_id'] for entry in packet.ranked_sources}
    stored = {
        row['chunk_id'] for row in store.get_knowledge_chunks(list(chunk_ids))
    }
    assert chunk_ids == stored

    # Drive through approvals + fake execution + analysis turns: only
    # Honeydew's two eligible phases may ever build an advisory.
    protocol_action = next(
        action for action in store.list_actions(run.run_id)
        if action.type == 'approve_protocol'
        and action.approval_status == ApprovalStatus.PENDING
    )
    engine.approve_action(protocol_action.action_id, reviewer='smoke-human',
                          reason='ok')
    execution_action = next(
        action for action in store.list_actions(run.run_id)
        if action.type == 'submit_experiment_matrix'
        and action.approval_status == ApprovalStatus.PENDING
    )
    engine.approve_action(execution_action.action_id, reviewer='smoke-human',
                          reason='ok')
    for job in store.list_jobs(run.run_id):
        assert job.external_run_id is not None

    phases = sorted(
        event.payload['phase'] for event in _built_events(store)
    )
    assert phases == ['methodology_review', 'protocol_draft']


def test_dense_backend_failure_degrades_and_run_survives(tmp_path: Path) -> None:
    engine, store, _approved = _build(tmp_path, stability_text=STABILITY_TEXT)

    def _explode(*_args, **_kwargs):
        raise RuntimeError('dense backend exploded')

    assert engine.knowledge.dense_index is not None
    engine.knowledge.dense_index.search = _explode

    run = engine.create_run(
        RunCreateRequest(objective='Survive dense outage during protocol.')
    )
    assert run.state == RunState.AWAITING_PROTOCOL_APPROVAL

    retrieval_events = [
        event for event in store.list_events(run.run_id)
        if event.event_type == 'agent.context_retrieved'
    ]
    assert retrieval_events
    actual_modes = {
        event.payload.get('retrieval_mode_actual')
        for event in retrieval_events
    }
    assert any(mode.startswith('lexical(fallback)') for mode in actual_modes)


def test_uploaded_source_reachable_via_dense_on_next_advisory(
    tmp_path: Path,
) -> None:
    """The operator upload lifecycle, end to end through one advisory.

    Ingest bytes the way POST /knowledge/sources/upload does, then drive a
    real run: the incremental ensure must embed the uploaded chunk and the
    protocol_draft advisory must retrieve it via DENSE on the first try.
    """
    engine, store, _approved = _build(tmp_path, stability_text=STABILITY_TEXT)
    uploaded = engine.knowledge.ingest_bytes(
        source_type=SourceType.PAPER,
        filename='uploaded-probe.md',
        data=(
            b'Uploaded probe note: consensus clustering with bootstrap '
            b'resampling and adjusted rand index tracks cluster stability.'
        ),
        title='Uploaded probe',
    )

    run = engine.create_run(
        RunCreateRequest(objective='cluster stability assessment')
    )
    assert run.state == RunState.AWAITING_PROTOCOL_APPROVAL

    built = [
        event for event in store.list_events(run.run_id)
        if event.event_type == 'agent.method_advisory_built'
    ]
    assert len(built) == 1
    packet = store.get_context_packet(built[0].payload['packet_id'])
    cited_sources = {
        entry.get('source_id') for entry in packet.ranked_sources
    }
    assert uploaded.source_id in cited_sources

    retrieval = [
        event for event in store.list_events(run.run_id)
        if event.event_type == 'agent.context_retrieved'
        and event.payload.get('packet_id') == built[0].payload['packet_id']
    ]
    assert retrieval
    assert retrieval[-1].payload.get('retrieval_mode_actual') == 'dense'


def test_recover_builds_dense_index_when_mode_dense(
    tmp_path: Path, monkeypatch
) -> None:
    engine, _, _ = _build(
        tmp_path,
        stability_text='Recovery hook must trigger a dense index build.',
    )
    assert engine.settings.knowledge_dense_mode == 'dense'
    calls: list[tuple[object, object]] = []

    def fake_build(index, store):
        calls.append((index, store))

    monkeypatch.setattr(
        'app.knowledge_dense.ensure_index_built', fake_build
    )
    engine._rebuild_dense_index_if_needed()
    assert len(calls) == 1
    assert calls[0][0] is engine.knowledge.dense_index

    engine.settings.knowledge_dense_mode = 'lexical'
    engine._rebuild_dense_index_if_needed()
    assert len(calls) == 1


def test_recover_build_never_blocks_on_provider_failure(
    tmp_path: Path, monkeypatch
) -> None:
    engine, _, _ = _build(
        tmp_path,
        stability_text='A provider failure must not break recovery.',
    )

    def broken_build(index, store):
        raise RuntimeError('sentence-transformers unavailable')

    monkeypatch.setattr(
        'app.knowledge_dense.ensure_index_built', broken_build
    )
    engine._rebuild_dense_index_if_needed()  # must not raise


def test_conversation_prior_context_renders_last_turns_bounded(
    orchestrator_bundle,
) -> None:
    settings, store, _cluster, runtime, engine = orchestrator_bundle
    run_id = 'chat-memory'
    engine.store.create_run(
        RunRecord(
            run_id=run_id,
            objective='prior context',
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
        ),
        one_active_run=False,
    )
    for idx in range(3):
        engine.store.save_turn(
            TurnRecord(
                run_id=run_id,
                agent=AgentName.HONEYDEW,
                input_event={'question': f'question {idx}'},
                structured_output=AgentTurnResult(
                    kind=TurnKind.RESEARCH_ANSWER,
                    summary='prior turn',
                    research_answer=ResearchAnswer(
                        answer=f'answer {idx} with enough words to count toward the token budget',
                        citations=[],
                        unanswerable=False,
                        suggested_followups=[],
                    ),
                ),
                status='completed',
            )
        )
    context = engine._conversation_prior_context(run_id, max_turns=2, max_tokens=10000)
    assert 'question 2' in context
    assert 'answer 2' in context
    assert 'question 0' not in context  # bounded to last max_turns=2
    assert 'PRIOR CONVERSATION' not in context  # formatting is added by the caller


def test_conversation_prior_context_respects_token_budget(
    orchestrator_bundle,
) -> None:
    settings, store, _cluster, runtime, engine = orchestrator_bundle
    run_id = 'chat-budget'
    engine.store.create_run(
        RunRecord(
            run_id=run_id,
            objective='prior budget',
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
        ),
        one_active_run=False,
    )
    engine.store.save_turn(
        TurnRecord(
            run_id=run_id,
            agent=AgentName.HONEYDEW,
            input_event={'question': 'q'},
            structured_output=AgentTurnResult(
                kind=TurnKind.RESEARCH_ANSWER,
                summary='prior turn',
                research_answer=ResearchAnswer(
                    answer='this is a moderately sized answer text',
                    citations=[],
                    unanswerable=False,
                    suggested_followups=[],
                ),
            ),
            status='completed',
        )
    )
    context = engine._conversation_prior_context(run_id, max_turns=5, max_tokens=4)
    assert context == ''  # first turn alone already exceeds the 4-token budget


def test_promoted_run_seeds_protocol_and_binds_existing_thread(
    tmp_path: Path,
) -> None:
    engine, store, _approved = _build(tmp_path, stability_text=STABILITY_TEXT)
    run = engine.create_run(
        RunCreateRequest(
            objective='Seed the protocol from a research conversation.',
            seed_context='Q: what is metric learning\nA: embeddings and anchors',
            seed_source_ids=['source-a'],
            existing_discord_thread_id='thread-999',
        )
    )
    assert run.state == RunState.AWAITING_PROTOCOL_APPROVAL
    assert run.discord_thread_id == 'thread-999'
    assert run.seed_context
    assert run.seed_source_ids == ['source-a']
    honeydew_prompts = [
        prompt
        for (agent, prompt) in engine.runtime.prompts
        if agent == AgentName.HONEYDEW
    ]
    assert any(
        'Q: what is metric learning' in prompt for prompt in honeydew_prompts
    )
    assert any(
        'promoted from a research conversation' in prompt
        for prompt in honeydew_prompts
    )


def test_conversation_objective_is_descriptive_synthesis(
    orchestrator_bundle,
) -> None:
    settings, store, _cluster, runtime, engine = orchestrator_bundle
    run_id = 'chat-objective'
    engine.store.create_run(
        RunRecord(
            run_id=run_id,
            objective='synthesis',
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
            conversation=True,
            created_at=utc_now(),
            updated_at=utc_now(),
        ),
        one_active_run=False,
    )
    for idx, question in enumerate(
        (
            'what is metric learning',
            'how does cosine similarity relate to embedding retrieval',
        )
    ):
        engine.store.save_turn(
            TurnRecord(
                run_id=run_id,
                agent=AgentName.HONEYDEW,
                input_event={'question': question},
                structured_output=AgentTurnResult(
                    kind=TurnKind.RESEARCH_ANSWER,
                    summary='prior turn',
                    research_answer=ResearchAnswer(
                        answer=f'grounded answer {idx}',
                        citations=[],
                        unanswerable=False,
                        suggested_followups=[],
                    ),
                ),
                status='completed',
            )
        )
    objective = engine._conversation_objective(
        [t for t in engine.store.list_turns(run_id)]
    )
    assert objective.startswith('Investigate metric learning')
    assert 'cosine similarity relate to embedding retrieval' in objective
    assert 'what is' not in objective


def test_topic_phrase_strips_question_openers(orchestrator_bundle) -> None:
    settings, store, _cluster, runtime, engine = orchestrator_bundle
    assert engine._topic_phrase('what is metric learning?') == (
        'metric learning'
    )
    assert engine._topic_phrase('how does conformal work') == (
        'conformal work'
    )
    assert engine._topic_phrase('explain the protocol') == 'the protocol'


def test_topic_phrase_strips_leading_conjunction_before_opener(
    orchestrator_bundle,
) -> None:
    settings, store, _cluster, runtime, engine = orchestrator_bundle
    assert engine._topic_phrase(
        'and how does cosine similarity relate to retrieval'
    ) == 'cosine similarity relate to retrieval'

