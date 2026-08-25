"""Engine integration: Honeydew protocol turns receive the method advisory.

Drives a REAL ResearchOrchestrator end-to-end with the scripted mock runtime
(same harness as app/smoke.py) over an offline deterministic embedding index,
then asserts the authoritative provenance trail:

1. Honeydew's protocol drafting turn triggers exactly one
   ``agent.method_advisory_built`` event whose packet_id resolves to a
   persisted ContextPacket containing role-approved chunks only.
2. The advisory block was attached to the turn
   (``agent.method_advisory_attached``).
3. Beaker never receives the advisory (no additional build events after
   execution/analysis turns; hook condition excludes Beaker entirely).
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
from app.schemas import ApprovalStatus, RunCreateRequest, RunState, SourceType

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

    # Drive through approvals + fake execution + analysis turns: Beaker must
    # never trigger another advisory build.
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

    assert len(_built_events(store)) == 1


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
