"""FastAPI operator surface for the research orchestrator.

The app is a thin HTTP projection over the ResearchOrchestrator engine: it
validates requests, gates mutations behind the operator token, and maps
domain errors to HTTP statuses. State and policy live in the engine and
store, not here. All read endpoints are intentionally unauthenticated because
the service binds to an internal network; only state-changing endpoints
require the operator token.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
import asyncio
import json
import secrets
from typing import AsyncIterator

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    UploadFile,
    status,
)
from fastapi.responses import StreamingResponse

from .cluster import FakeClusterExecutor, WorkflowApiClusterExecutor
from .config import SERVICE_ROOT, Settings, get_settings
from .contract_candidates import ContractCandidateManager
from .contracts import ContractIntegrityError, EvaluationContractResolver
from .discord_adapter import DisabledDiscordAdapter, DiscordHttpAdapter
from .discord_controls import DiscordControlGateway
from .datasets import DatasetIngestionError, DatasetIngestionManager
from .engine import ResearchOrchestrator, WorkflowError
from .hermes_runtime import HermesProcessRuntime
from .knowledge_manager import KnowledgeError
from .opencode_runtime import AgentRuntime, OpenCodeProcessRuntime
from .policy import ActionPolicy
from .research_store import ResearchStore
from .schemas import (
    ActionRecord,
    ApprovalRequest,
    ArtifactListResponse,
    ContextPacket,
    ContextPacketListResponse,
    EventListResponse,
    IngestedDatasetRecord,
    KnowledgeSource,
    KnowledgeSourceListResponse,
    KnowledgeSourceRequest,
    RejectionRequest,
    RunCreateRequest,
    RunListResponse,
    RunRecord,
    TerminalRetryRequest,
    SourceType,
    TurnListResponse,
)
from .storage import ConcurrencyConflict, RecordNotFound, SqliteStore
from .postgres_store import PostgresStore
from .turn_inspection import DEFAULT_TURN_LIMIT, MAXIMUM_TURN_LIMIT, summarize_turns
from .task_bundles import (
    TaskBundleError,
    TaskBundleManager,
    TaskBundleRecord,
    TaskPreflight,
)
from .watcher import JobWatcher
from .workspaces import WorkspaceError, WorkspaceManager


def build_agent_runtime(settings: Settings) -> AgentRuntime:
    if settings.agent_runtime_backend == 'hermes':
        return HermesProcessRuntime(settings)
    return OpenCodeProcessRuntime(settings)


def build_engine(
    settings: Settings,
    *,
    runtime: AgentRuntime | None = None,
    cluster=None,
    discord=None,
) -> ResearchOrchestrator:
    # Composition root: wires every subsystem against the same store so all
    # mutations share one transaction boundary and one event log. The cluster
    # executor is swapped for a fake when running without a live API.
    store: ResearchStore = (
        PostgresStore(settings.store_postgres_dsn)
        if settings.store_backend == 'postgres'
        else SqliteStore(settings.database_path)
    )
    if runtime is None:
        runtime = build_agent_runtime(settings)
    if cluster is None:
        cluster = (
            FakeClusterExecutor()
            if settings.cluster_execution_mode == 'fake'
            else WorkflowApiClusterExecutor(
                base_url=settings.cluster_execution_api_url,
                workload_id=settings.cluster_execution_workload_id,
                experiment_type=settings.cluster_execution_experiment_type,
                caller_name=settings.workflow_api_caller_name,
                token=(
                    settings.workflow_api_token.get_secret_value()
                    if settings.workflow_api_token
                    else ''
                ),
            )
        )
    if discord is None:
        if (
            settings.discord_enabled
            and settings.discord_bot_token
            and settings.discord_channel_id
        ):
            discord = DiscordHttpAdapter(
                bot_token=settings.discord_bot_token,
                channel_id=settings.discord_channel_id,
                webhook_url=settings.discord_webhook_url,
            )
        else:
            discord = DisabledDiscordAdapter()
    contract_candidates = ContractCandidateManager(
        sealed_root=settings.sealed_contract_candidate_root,
        promoted_root=settings.promoted_contract_root,
        catalog_path=settings.trusted_contract_catalog_path,
        shared_mount_root=settings.shared_mount_root,
    )
    baked_root = SERVICE_ROOT / 'evaluation-contracts'
    # Repository-baked contracts are installed at startup so the trusted
    # catalog is never empty even on a fresh deployment.
    for contract_id, version in (
        ('generic-task-integrity-v1', '1.0.0'),
        ('ml-benchmark-adult-income-v1', '1.0.0'),
        ('ml-benchmark-adult-income-v1', '1.1.0'),
        ('ml-benchmark-wine-clustering-v1', '1.0.0'),
        ('ml-benchmark-fashion-contrastive-v1', '1.0.0'),
    ):
        contract_candidates.install_repository_contract(
            baked_root / contract_id / version
        )
    datasets = DatasetIngestionManager(
        store=store,
        root=settings.dataset_upload_root,
        shared_mount_root=settings.shared_mount_root,
        maximum_bytes=settings.maximum_dataset_upload_bytes,
    )
    return ResearchOrchestrator(
        settings=settings,
        store=store,
        runtime=runtime,
        workspaces=WorkspaceManager(
            workspace_root=settings.workspace_root,
            approved_repo_path=settings.approved_repo_path,
            approved_repo_ref=settings.approved_repo_ref,
        ),
        contracts=EvaluationContractResolver(
            settings.promoted_contract_root,
            fallback_roots=[settings.evaluation_contract_root],
        ),
        contract_candidates=contract_candidates,
        datasets=datasets,
        task_bundles=TaskBundleManager(
            root=settings.task_bundle_root,
            shared_mount_root=settings.shared_mount_root,
            dataset_catalog_path=settings.benchmark_dataset_catalog_path,
            task_asset_root=settings.task_asset_root,
            maximum_asset_bytes=settings.maximum_task_asset_bytes,
            ingested_datasets=datasets,
        ),
        policy=ActionPolicy(
            permitted_images=settings.permitted_job_images,
            maximum_cpu=settings.maximum_cpu,
            maximum_memory_gib=settings.maximum_memory_gib,
            maximum_gpus=settings.maximum_gpus,
            maximum_parallel_jobs=settings.maximum_parallel_jobs,
        ),
        cluster=cluster,
        discord=discord,
    )


def create_app(
    settings: Settings | None = None,
    *,
    engine: ResearchOrchestrator | None = None,
    start_watcher: bool = True,
) -> FastAPI:
    settings = settings or get_settings()
    engine = engine or build_engine(settings)
    watcher = JobWatcher(
        engine,
        poll_interval_seconds=settings.job_poll_interval_seconds,
    )
    discord_controls = None
    if (
        settings.discord_controls_enabled
        and settings.discord_bot_token
        and settings.discord_guild_id
    ):
        discord_controls = DiscordControlGateway(
            engine=engine,
            bot_token=settings.discord_bot_token,
            guild_id=settings.discord_guild_id,
            channel_id=settings.discord_channel_id or '',
            admin_role_id=settings.discord_admin_role_id,
            admin_user_ids=settings.discord_admin_user_ids,
            maximum_dataset_upload_bytes=(
                settings.maximum_discord_dataset_upload_bytes
            ),
            maximum_artifact_bundle_bytes=(
                settings.maximum_discord_artifact_bundle_bytes
            ),
        )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        # Recovery runs on a background thread while the API is already
        # serving: restart-crash sweeps (interrupted turns) and in-flight job
        # reconciliation must not block operator requests. Shutdown stops the
        # watcher and closes agent runtimes first, then drains the tasks.
        recovery_task = asyncio.create_task(
            asyncio.to_thread(engine.recover),
            name='research-orchestrator-recovery',
        )
        watcher_task = (
            asyncio.create_task(
                watcher.run(),
                name='research-orchestrator-job-watcher',
            )
            if start_watcher
            else None
        )
        discord_task = (
            asyncio.create_task(
                discord_controls.run(),
                name='research-orchestrator-discord-controls',
            )
            if discord_controls is not None
            else None
        )
        try:
            yield
        finally:
            watcher.stop()
            engine.runtime.close()
            if discord_controls is not None:
                await discord_controls.close()
            tasks = [recovery_task]
            if watcher_task is not None:
                tasks.append(watcher_task)
            if discord_task is not None:
                tasks.append(discord_task)
            await asyncio.gather(*tasks, return_exceptions=True)

    app = FastAPI(
        title='Glasslab Research Orchestrator',
        version=settings.app_version,
        lifespan=lifespan,
    )
    app.state.engine = engine
    app.state.discord_controls = discord_controls

    def require_operator(
        supplied_token: str | None = Header(
            default=None,
            alias='X-Glasslab-Operator-Token',
        ),
    ) -> None:
        if not settings.require_operator_auth:
            return
        expected = settings.operator_api_token
        if not expected:
            # Fail loudly (503) rather than silently allowing requests through
            # when auth is required but no token is configured.
            raise HTTPException(
                status_code=503,
                detail='operator authentication is required but not configured',
            )
        if supplied_token is None or not secrets.compare_digest(
            supplied_token,
            expected,
        ):
            # compare_digest makes the token comparison constant-time.
            raise HTTPException(
                status_code=401,
                detail='valid operator token required',
            )

    def map_error(exc: Exception) -> HTTPException:
        # 404 for missing records, 409 for domain conflicts and integrity
        # violations (callers may retry after fixing the conflict), 500 for
        # anything unexpected. Domain errors never leak stack traces.
        if isinstance(exc, RecordNotFound):
            return HTTPException(status_code=404, detail=str(exc))
        if isinstance(
            exc,
            (
                ConcurrencyConflict,
                ContractIntegrityError,
                WorkflowError,
                WorkspaceError,
                TaskBundleError,
                DatasetIngestionError,
                KnowledgeError,
            ),
        ):
            return HTTPException(status_code=409, detail=str(exc))
        return HTTPException(status_code=500, detail=str(exc))

    @app.get('/health')
    def health() -> dict[str, object]:
        knowledge_dense: dict[str, object] | None = None
        try:
            dense_index = getattr(engine.knowledge, 'dense_index', None)
            if dense_index is not None:
                readiness = dense_index.readiness()
                knowledge_dense = {
                    'available': readiness.available,
                    'reason': readiness.reason,
                    'backend': readiness.backend,
                    'model_id': readiness.model_id,
                    'revision': readiness.revision,
                    'dims': readiness.dims,
                    'indexed_chunks': readiness.indexed_count,
                }
        except Exception as exc:  # noqa: BLE001 - diagnostics never fail /health
            knowledge_dense = {'available': False, 'reason': str(exc)}
        return {
            'status': 'ok',
            'service': settings.app_name,
            'version': settings.app_version,
            'knowledge_dense': knowledge_dense,
        }

    @app.get('/ready')
    def ready() -> dict[str, object]:
        try:
            database_ready = engine.store.ping()
            if (
                settings.require_operator_auth
                and not settings.operator_api_token
            ):
                raise RuntimeError(
                    'operator authentication is required but not configured'
                )
            contract = engine.contracts.resolve(
                settings.default_evaluation_contract_id,
                settings.default_evaluation_contract_version,
            )
        except Exception as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return {
            'status': 'ready',
            'database': database_ready,
            'discord_controls': (
                'ready'
                if discord_controls is not None
                and discord_controls.client.is_ready()
                else 'disabled'
                if discord_controls is None
                else 'connecting'
            ),
            'evaluation_contract': {
                'contract_id': contract.descriptor.contract_id,
                'version': contract.descriptor.version,
                'digest': contract.digest,
            },
        }

    @app.post('/runs', response_model=RunRecord, status_code=status.HTTP_201_CREATED)
    def create_run(
        request: RunCreateRequest,
        _: None = Depends(require_operator),
    ) -> RunRecord:
        try:
            return engine.create_run(request)
        except Exception as exc:
            raise map_error(exc) from exc

    @app.post('/runs/{run_id}/retry', response_model=RunRecord, status_code=status.HTTP_201_CREATED)
    def retry_terminal_run(
        run_id: str,
        request: TerminalRetryRequest,
        _: None = Depends(require_operator),
    ) -> RunRecord:
        try:
            return engine.retry_terminal_run(run_id, request)
        except Exception as exc:
            raise map_error(exc) from exc

    @app.post(
        '/task-bundles/import',
        response_model=TaskBundleRecord,
        status_code=status.HTTP_201_CREATED,
    )
    async def import_task_bundle(
        archive: UploadFile = File(...),
        _: None = Depends(require_operator),
    ) -> TaskBundleRecord:
        try:
            content = await archive.read(
                TaskBundleManager.MAX_ARCHIVE_BYTES + 1
            )
            return engine.import_task_bundle(
                filename=archive.filename or '',
                content=content,
            )
        except Exception as exc:
            raise map_error(exc) from exc
        finally:
            await archive.close()

    @app.get('/task-bundles', response_model=list[TaskBundleRecord])
    def list_task_bundles() -> list[TaskBundleRecord]:
        return engine.task_bundles.list()

    @app.post(
        '/datasets/import',
        response_model=IngestedDatasetRecord,
        status_code=status.HTTP_201_CREATED,
    )
    async def import_dataset(
        dataset: UploadFile = File(...),
        name: str = Form(...),
        role: str = Form(default='input'),
        contains_labels: bool = Form(default=False),
        uploaded_by: str | None = Form(default=None),
        _: None = Depends(require_operator),
    ) -> IngestedDatasetRecord:
        try:
            return await asyncio.to_thread(
                engine.datasets.ingest,
                dataset.file,
                filename=dataset.filename or '',
                name=name,
                role=role,
                contains_labels=contains_labels,
                media_type=dataset.content_type,
                uploaded_by=uploaded_by,
            )
        except Exception as exc:
            raise map_error(exc) from exc
        finally:
            await dataset.close()

    @app.get('/datasets', response_model=list[IngestedDatasetRecord])
    def list_datasets() -> list[IngestedDatasetRecord]:
        return engine.store.list_datasets()

    @app.get(
        '/datasets/{dataset_id}',
        response_model=IngestedDatasetRecord,
    )
    def get_dataset(dataset_id: str) -> IngestedDatasetRecord:
        try:
            return engine.store.get_dataset(dataset_id)
        except Exception as exc:
            raise map_error(exc) from exc

    @app.get('/task-bundles/{task_id}', response_model=TaskBundleRecord)
    def get_task_bundle(
        task_id: str,
        digest: str | None = Query(default=None),
    ) -> TaskBundleRecord:
        try:
            return engine.task_bundles.get(task_id, digest)
        except Exception as exc:
            raise map_error(exc) from exc

    @app.get(
        '/task-bundles/{task_id}/preflight',
        response_model=TaskPreflight,
    )
    def get_task_bundle_preflight(
        task_id: str,
        digest: str | None = Query(default=None),
    ) -> TaskPreflight:
        try:
            return engine.task_preflight(
                engine.task_bundles.get(task_id, digest)
            )
        except Exception as exc:
            raise map_error(exc) from exc

    @app.post(
        '/knowledge/sources',
        response_model=KnowledgeSource,
        status_code=status.HTTP_201_CREATED,
    )
    def ingest_knowledge_source(
        request: KnowledgeSourceRequest,
        _: None = Depends(require_operator),
    ) -> KnowledgeSource:
        # Operator-only ingestion from a path allowlisted in settings. The
        # endpoint performs the same allowlist, secret, and size checks as the
        # in-process ingest path; it never accepts raw text over HTTP.
        try:
            return engine.knowledge.ingest_source(
                source_type=request.source_type,
                path=request.path,
                title=request.title,
                source_version=request.source_version,
                metadata=request.metadata,
            )
        except Exception as exc:
            raise map_error(exc) from exc

    @app.get(
        '/knowledge/sources',
        response_model=KnowledgeSourceListResponse,
    )
    def list_knowledge_sources(
        source_type: str | None = Query(default=None),
        run_scope: str | None = Query(default=None),
    ) -> KnowledgeSourceListResponse:
        source_types = (
            [SourceType(source_type)] if source_type else None
        )
        return KnowledgeSourceListResponse(
            sources=engine.knowledge.store.list_knowledge_sources(
                source_types=source_types,
                run_scope=run_scope,
            )
        )

    @app.post(
        '/knowledge/index/rebuild',
        response_model=dict[str, object],
    )
    def rebuild_knowledge_index(
        _: None = Depends(require_operator),
    ) -> dict[str, object]:
        reindexed = engine.knowledge.rebuild_index()
        return {'index_version': 'v1', 'reindexed_sources': reindexed}

    @app.delete(
        '/knowledge/sources/{source_id}',
        response_model=dict[str, object],
    )
    def delete_knowledge_source(
        source_id: str,
        _: None = Depends(require_operator),
    ) -> dict[str, object]:
        removed = engine.knowledge.delete_source(source_id)
        return {'source_id': source_id, 'removed': removed}

    @app.delete(
        '/knowledge/sources/by-digest/{digest}',
        response_model=dict[str, object],
    )
    def invalidate_knowledge_by_digest(
        digest: str,
        _: None = Depends(require_operator),
    ) -> dict[str, object]:
        removed = engine.knowledge.invalidate_by_digest(digest)
        return {'digest': digest, 'removed': removed}

    @app.get(
        '/runs/{run_id}/context-packets',
        response_model=ContextPacketListResponse,
    )
    def list_context_packets(
        run_id: str,
        _: None = Depends(require_operator),
    ) -> ContextPacketListResponse:
        try:
            engine.store.get_run(run_id)
            return ContextPacketListResponse(
                packets=engine.store.list_context_packets(run_id)
            )
        except Exception as exc:
            raise map_error(exc) from exc

    @app.get(
        '/context-packets/{packet_id}',
        response_model=ContextPacket,
    )
    def get_context_packet(
        packet_id: str,
        _: None = Depends(require_operator),
    ) -> ContextPacket:
        try:
            return engine.knowledge.get_context_packet(packet_id)
        except Exception as exc:
            raise map_error(exc) from exc

    @app.get('/runs', response_model=RunListResponse)
    def list_runs() -> RunListResponse:
        # Read endpoints are intentionally unauthenticated (internal-only
        # network); the operator token gates only state-changing endpoints.
        return RunListResponse(runs=engine.store.list_runs())

    @app.get('/runs/{run_id}', response_model=RunRecord)
    def get_run(run_id: str) -> RunRecord:
        try:
            return engine.store.get_run(run_id)
        except Exception as exc:
            raise map_error(exc) from exc

    @app.get('/runs/{run_id}/events', response_model=EventListResponse)
    def get_events(
        run_id: str,
        after_sequence: int = Query(default=0, ge=0),
    ) -> EventListResponse:
        try:
            engine.store.get_run(run_id)
            return EventListResponse(
                events=engine.store.list_events(
                    run_id,
                    after_sequence=after_sequence,
                )
            )
        except Exception as exc:
            raise map_error(exc) from exc

    @app.get('/runs/{run_id}/artifacts', response_model=ArtifactListResponse)
    def get_artifacts(run_id: str) -> ArtifactListResponse:
        try:
            engine.store.get_run(run_id)
            return ArtifactListResponse(
                artifacts=engine.store.list_artifacts(run_id)
            )
        except Exception as exc:
            raise map_error(exc) from exc

    @app.get('/runs/{run_id}/turns', response_model=TurnListResponse)
    def get_turns(
        run_id: str,
        limit: int = Query(
            default=DEFAULT_TURN_LIMIT,
            ge=1,
            le=MAXIMUM_TURN_LIMIT,
        ),
    ) -> TurnListResponse:
        # Convenience view over already-persisted TurnRecords (see
        # turn_inspection.py): redacted and bounded, but never a second
        # source of truth. The normalized event log remains authoritative.
        try:
            engine.store.get_run(run_id)
            return TurnListResponse(
                turns=summarize_turns(
                    engine.store.list_turns(run_id),
                    limit=limit,
                )
            )
        except Exception as exc:
            raise map_error(exc) from exc

    @app.post('/runs/{run_id}/pause', response_model=RunRecord)
    def pause_run(
        run_id: str,
        _: None = Depends(require_operator),
    ) -> RunRecord:
        try:
            return engine.pause_run(run_id)
        except Exception as exc:
            raise map_error(exc) from exc

    @app.post('/runs/{run_id}/resume', response_model=RunRecord)
    def resume_run(
        run_id: str,
        _: None = Depends(require_operator),
    ) -> RunRecord:
        try:
            return engine.resume_run(run_id)
        except Exception as exc:
            raise map_error(exc) from exc

    @app.post('/runs/{run_id}/cancel', response_model=RunRecord)
    def cancel_run(
        run_id: str,
        _: None = Depends(require_operator),
    ) -> RunRecord:
        try:
            return engine.cancel_run(run_id)
        except Exception as exc:
            raise map_error(exc) from exc

    @app.get('/actions/{action_id}', response_model=ActionRecord)
    def get_action(action_id: str) -> ActionRecord:
        try:
            return engine.store.get_action(action_id)
        except Exception as exc:
            raise map_error(exc) from exc

    @app.post('/actions/{action_id}/approve', response_model=ActionRecord)
    def approve_action(
        action_id: str,
        request: ApprovalRequest,
        _: None = Depends(require_operator),
    ) -> ActionRecord:
        try:
            return engine.approve_action(
                action_id,
                reviewer=request.reviewer,
                reason=request.reason,
            )
        except Exception as exc:
            raise map_error(exc) from exc

    @app.post('/actions/{action_id}/reject', response_model=ActionRecord)
    def reject_action(
        action_id: str,
        request: RejectionRequest,
        _: None = Depends(require_operator),
    ) -> ActionRecord:
        try:
            return engine.reject_action(
                action_id,
                reviewer=request.reviewer,
                reason=request.reason,
            )
        except Exception as exc:
            raise map_error(exc) from exc

    @app.get('/runs/{run_id}/events/stream')
    async def stream_events(
        run_id: str,
        after_sequence: int = Query(default=0, ge=0),
    ) -> StreamingResponse:
        try:
            engine.store.get_run(run_id)
        except Exception as exc:
            raise map_error(exc) from exc

        async def generate() -> AsyncIterator[str]:
            # Each event is emitted with id: <sequence_number> so a reconnecting
            # client can resume with after_sequence=<last id>; list_events is
            # gap-free and ordered, so the cursor never skips or duplicates.
            cursor = after_sequence
            while True:
                events = engine.store.list_events(
                    run_id,
                    after_sequence=cursor,
                )
                for event in events:
                    cursor = event.sequence_number
                    yield (
                        f'id: {event.sequence_number}\n'
                        f'event: {event.event_type}\n'
                        f'data: {json.dumps(event.model_dump(mode="json"))}\n\n'
                    )
                await asyncio.sleep(0.5)

        return StreamingResponse(
            generate(),
            media_type='text/event-stream',
            headers={
                'Cache-Control': 'no-cache',
                'X-Accel-Buffering': 'no',
            },
        )

    return app


app = create_app()
# Module import builds the single app instance; uvicorn imports this module
# and serves the already-constructed app, so startup side effects (schema
# ensure, contract install) run exactly once per process.
