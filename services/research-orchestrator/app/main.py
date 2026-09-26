"""FastAPI operator surface for the research orchestrator.

The app is a thin HTTP projection over the ResearchOrchestrator engine: it
validates requests, gates every endpoint behind the operator token, and maps
domain errors to HTTP statuses. State and policy live in the engine and
store, not here. Read endpoints were previously left unauthenticated because
the service binds to an internal network, but the documented contributor
port-forward makes them reachable from any workstation, so reads now require
the operator token too (issue #369). Only ``/health`` and ``/ready`` stay
anonymous for the kubelet probes. Free-form read payloads are redacted before
they leave the process even for authorized readers.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import asynccontextmanager
import asyncio
from dataclasses import dataclass
import html
import json
import logging
from pathlib import Path, PurePosixPath
import secrets
from typing import Any, AsyncIterator, assert_never

import httpx

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from pydantic import SecretStr

from .artifact_delivery import ArtifactDeliveryError, VerifiedArtifactReader
from .cluster import FakeClusterExecutor, WorkflowApiClusterExecutor
from .config import SERVICE_ROOT, Settings, get_settings
from .contract_candidates import ContractCandidateManager
from .contracts import ContractIntegrityError, EvaluationContractResolver
from .corpus_rag.pdf_backend import UnsupportedDocumentError
from .discord_adapter import (
    DisabledDiscordAdapter,
    DiscordAttachment,
    DiscordHttpAdapter,
)
from .discord_controls import DiscordControlGateway
from .discord_rest import (
    DiscordCircuitOpen,
    DiscordRestCircuit,
    DiscordRestPolicy,
    execute_guarded,
)
from .datasets import DatasetIngestionError, DatasetIngestionManager, DatasetUrlError
from .engine import ResearchOrchestrator, WorkflowError
from .hermes_runtime import HermesProcessRuntime
from .knowledge_manager import KnowledgeError
from .knowledge_tool import KnowledgeToolDenied, KnowledgeToolError
from .links import (
    LINKABLE_ARTIFACT_PREFIXES,
    LinkBuilder,
    LinkError,
    LinkKind,
    LinkPayload,
    build_link_url,
    link_token_fingerprint,
    run_relative_ref,
    verify_link,
)
from .opencode_runtime import AgentRuntime, OpenCodeProcessRuntime
from .policy import ActionPolicy
from .redaction import redact_payload
from .research_store import ResearchStore
from .schemas import (
    ActionRecord,
    ApprovalRequest,
    ArtifactListResponse,
    ArtifactRecord,
ChatRequest,
    ConversationSourceBindRequest,
    ConversationSourceBinding,
ConversationPromoteRequest,
    ContextPacket,
    ContextPacketListResponse,
    EventListResponse,
    EventRecord,
IngestedDatasetRecord,
    CatalogDatasetRecord,
    DatasetUrlRegisterRequest,
    KnowledgeSource,
    KnowledgeSourceListResponse,
    KnowledgeSourceRequest,
    RetrieveEvidenceToolRequest,
    RejectionRequest,
    ResearchAnswer,
    RunCreateRequest,
    RunListResponse,
    RunRecord,
    TerminalRetryRequest,
    SourceType,
    TurnListResponse,
    utc_now,
)
from .storage import ConcurrencyConflict, RecordNotFound, SqliteStore
from .postgres_store import PostgresStore
from .turn_inspection import DEFAULT_TURN_LIMIT, MAXIMUM_TURN_LIMIT, summarize_turns
from .task_bundles import (
    TaskBundleError,
    TaskBundleManager,
    TaskBundleRecord,
    TaskPreflight,
    require_profile_runner_images,
)
from .watcher import JobWatcher
from .workspaces import WorkspaceError, WorkspaceManager


logger = logging.getLogger(__name__)


class _RedactLinkTokensFilter(logging.Filter):
    """Keep signed-link bearer tokens out of the server access log.

    The token in ``/links/{token}`` is the entire authorization for that
    route, so the default uvicorn access log must not persist it verbatim.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3:
            target = args[2]
            if isinstance(target, str) and target.startswith('/links/'):
                record.args = (
                    args[0],
                    args[1],
                    '/links/<redacted>',
                    *args[3:],
                )
        return True


logging.getLogger('uvicorn.access').addFilter(_RedactLinkTokensFilter())


def build_agent_runtime(settings: Settings) -> AgentRuntime:
    if settings.agent_runtime_backend == 'hermes':
        return HermesProcessRuntime(settings)
    return OpenCodeProcessRuntime(settings)


def _report_markdown_artifact(event: EventRecord) -> list[ArtifactRecord]:
    payload = event.payload
    path = payload.get('path')
    uri = payload.get('uri')
    digest = payload.get('sha256')
    if not (
        isinstance(path, str)
        and isinstance(uri, str)
        and isinstance(digest, str)
    ):
        return []
    # The engine only ever accepts a real regular file as a report; reject a
    # symlink before the reader resolves it, since resolution would otherwise
    # follow the link to its target.
    if Path(path).is_symlink():
        return []
    return [
        ArtifactRecord(
            run_id=event.run_id,
            type='report',
            uri=uri,
            sha256=digest,
            metadata={'path': path},
        )
    ]


def _report_bundle_artifacts(
    event: EventRecord,
    store: ResearchStore,
) -> list[ArtifactRecord]:
    by_uri = {
        artifact.uri: artifact for artifact in store.list_artifacts(event.run_id)
    }
    artifacts: list[ArtifactRecord] = []
    for key in ('pdf', 'docx'):
        uri = event.payload.get(key)
        if not isinstance(uri, str):
            continue
        artifact = by_uri.get(uri)
        if artifact is not None:
            artifacts.append(artifact)
    return artifacts


def build_report_attachment_loader(
    *,
    store: ResearchStore,
    shared_mount_root: str,
    maximum_bytes: int,
) -> Callable[[EventRecord], tuple[DiscordAttachment, ...]]:
    """Load a report event's files for Discord delivery.

    Reads go through the artifact-delivery guards (mount containment, symlink
    rejection, digest verification). A file that is unavailable or does not
    fit the remaining byte budget is skipped individually so the report
    message itself still posts.
    """
    reader = VerifiedArtifactReader(shared_mount_root)

    def load(event: EventRecord) -> tuple[DiscordAttachment, ...]:
        if event.event_type == 'report.created':
            artifacts = _report_markdown_artifact(event)
        elif event.event_type == 'report.bundle_created':
            artifacts = _report_bundle_artifacts(event, store)
        else:
            return ()
        attachments: list[DiscordAttachment] = []
        total = 0
        for artifact in artifacts:
            remaining = maximum_bytes - total
            if remaining <= 0:
                continue
            try:
                content = reader.read(artifact, maximum_bytes=remaining)
            except ArtifactDeliveryError:
                continue
            total += len(content)
            attachments.append(
                DiscordAttachment(
                    filename=Path(artifact.uri).name or artifact.type,
                    content=content,
                )
            )
        return tuple(attachments)

    return load


def build_link_builder(settings: Settings) -> LinkBuilder | None:
    """Return a callable that mints ``{public_base_url}/links/{token}`` URLs.

    ``None`` when ``public_base_url`` is unset, so callers keep their existing
    non-link rendering. A configured base URL with a missing signing secret
    still returns a callable, but every call returns ``None``: link emission
    fails closed instead of signing with an absent secret.
    """
    base_url = settings.public_base_url
    if not base_url:
        return None
    secret = (
        settings.link_signing_secret.get_secret_value()
        if settings.link_signing_secret is not None
        else None
    )
    ttl_seconds = settings.link_ttl_seconds

    def builder(kind: LinkKind, run_id: str, ref: str) -> str | None:
        return build_link_url(
            public_base_url=base_url,
            secret=secret,
            kind=kind,
            run_id=run_id,
            ref=ref,
            ttl_seconds=ttl_seconds,
        )

    return builder


LINK_HTML_HEADERS = {
    'Content-Security-Policy': "default-src 'none'; style-src 'unsafe-inline'",
    'X-Content-Type-Options': 'nosniff',
    'Referrer-Policy': 'no-referrer',
    'Content-Disposition': 'inline',
}
# Hard ceiling on a single signed-link artifact download; larger files remain
# reachable only through the operator artifact API, never an anonymous link.
MAXIMUM_LINK_ARTIFACT_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class LinkRedemption:
    engine: ResearchOrchestrator
    settings: Settings
    payload: LinkPayload
    token: str
    client_host: str | None


def _audit_link_redemption(
    *,
    token: str,
    outcome: str,
    payload: LinkPayload | None,
    client_host: str | None,
) -> None:
    # Structured key=value record: the fingerprint is a one-way prefix, so the
    # full token and its signature never reach the log.
    logger.info(
        'link redemption outcome=%s kind=%s run_id=%s ref=%s '
        'token_sha256_prefix=%s client_host=%s',
        outcome,
        payload.kind.value if payload is not None else None,
        payload.run_id if payload is not None else None,
        payload.ref if payload is not None else None,
        link_token_fingerprint(token),
        client_host,
    )


def _find_artifact_by_ref(
    artifacts: list[ArtifactRecord],
    ref: str,
) -> ArtifactRecord | None:
    # Latest record wins, mirroring artifact delivery's dedup semantics.
    match: ArtifactRecord | None = None
    for artifact in artifacts:
        if run_relative_ref(artifact.uri, artifact.run_id) == ref:
            match = artifact
    return match


def _read_link_artifact(
    redemption: LinkRedemption,
    artifact: ArtifactRecord | None,
) -> bytes | None:
    if artifact is None:
        return None
    try:
        return VerifiedArtifactReader(
            redemption.settings.shared_mount_root
        ).read(artifact, maximum_bytes=MAXIMUM_LINK_ARTIFACT_BYTES)
    except ArtifactDeliveryError:
        return None


def _deny_link(redemption: LinkRedemption, outcome: str) -> None:
    _audit_link_redemption(
        token=redemption.token,
        outcome=outcome,
        payload=redemption.payload,
        client_host=redemption.client_host,
    )
    raise HTTPException(status_code=404, detail='not found')


def _serve_report_link(redemption: LinkRedemption) -> Response:
    payload = redemption.payload
    if not payload.ref.startswith('reports/'):
        _deny_link(redemption, 'ref_denied')
    artifact = _find_artifact_by_ref(
        redemption.engine.store.list_artifacts(payload.run_id),
        payload.ref,
    )
    content = _read_link_artifact(redemption, artifact)
    if content is None:
        _deny_link(redemption, 'target_unavailable')
    _audit_link_redemption(
        token=redemption.token,
        outcome='served_report',
        payload=payload,
        client_host=redemption.client_host,
    )
    return HTMLResponse(
        content=render_report_link_html(payload, artifact, content),
        headers=LINK_HTML_HEADERS,
    )


def _serve_artifact_link(redemption: LinkRedemption) -> Response:
    payload = redemption.payload
    if not payload.ref.startswith(LINKABLE_ARTIFACT_PREFIXES):
        _deny_link(redemption, 'ref_denied')
    artifact = _find_artifact_by_ref(
        redemption.engine.store.list_artifacts(payload.run_id),
        payload.ref,
    )
    content = _read_link_artifact(redemption, artifact)
    if content is None:
        _deny_link(redemption, 'target_unavailable')
    _audit_link_redemption(
        token=redemption.token,
        outcome='served_artifact',
        payload=payload,
        client_host=redemption.client_host,
    )
    return Response(
        content=content,
        media_type='application/octet-stream',
        headers={
            'Content-Disposition': (
                f'attachment; filename="{_sanitized_link_filename(payload.ref)}"'
            ),
            'X-Content-Type-Options': 'nosniff',
            'Referrer-Policy': 'no-referrer',
        },
    )


def _serve_packet_link(redemption: LinkRedemption) -> Response:
    payload = redemption.payload
    packet_id = payload.ref.removeprefix('knowledge://context:')
    try:
        packet = redemption.engine.knowledge.get_context_packet(packet_id)
    except Exception:  # noqa: BLE001 - a missing packet must never leak why
        _deny_link(redemption, 'target_unavailable')
    if packet.run_id != payload.run_id:
        _deny_link(redemption, 'run_mismatch')
    _audit_link_redemption(
        token=redemption.token,
        outcome='served_packet',
        payload=payload,
        client_host=redemption.client_host,
    )
    return HTMLResponse(
        content=render_context_packet_html(packet, packet_id),
        headers=LINK_HTML_HEADERS,
    )


def serve_link(redemption: LinkRedemption) -> Response:
    match redemption.payload.kind:
        case LinkKind.REPORT:
            return _serve_report_link(redemption)
        case LinkKind.ARTIFACT:
            return _serve_artifact_link(redemption)
        case LinkKind.PACKET:
            return _serve_packet_link(redemption)
        case _ as unreachable:
            assert_never(unreachable)


def _sanitized_link_filename(ref: str) -> str:
    name = PurePosixPath(ref).name
    safe = ''.join(
        char
        for char in name
        if char.isascii() and (char.isalnum() or char in '._-')
    )
    return safe or 'artifact.bin'


def render_report_link_html(
    payload: LinkPayload,
    artifact: ArtifactRecord,
    markdown: bytes,
) -> str:
    # Escape-first: the report body is agent-authored markdown and is rendered
    # as escaped text inside <pre>, never converted to raw HTML.
    text = markdown.decode('utf-8', errors='replace')
    return (
        '<html><head><title>Glasslab research report</title>'
        '<meta charset="utf-8">'
        '<style>body{font-family:system-ui,sans-serif;max-width:900px;'
        'margin:2rem auto;padding:0 1rem;line-height:1.5}'
        'h1{font-size:1.2rem} pre{white-space:pre-wrap;background:#f6f8fa;'
        'padding:1rem;border-radius:6px;font-size:.9rem}</style>'
        '</head><body>'
        f'<h1>Research report <code>{html.escape(payload.run_id)}</code></h1>'
        f'<p><strong>Artifact:</strong> <code>{html.escape(payload.ref)}</code>'
        ' · <strong>SHA-256:</strong> '
        f'<code>{html.escape(artifact.sha256[:12])}...</code></p>'
        '<h2>Report</h2>'
        f'<pre>{html.escape(text)}</pre>'
        '</body></html>'
    )


def render_context_packet_html(packet: ContextPacket, packet_id: str) -> str:
    exact = (packet.exact_text_supplied or '').strip()
    sources = '\n'.join(
        '<tr>'
        f'<td>{index}</td>'
        f'<td><code>{html.escape(str(s.get("kind", "")))}</code></td>'
        f'<td>{html.escape(str(s.get("source_id", "")))}</td>'
        f'<td><code>{html.escape(str(s.get("uri", "")))}</code></td>'
        f'<td>{html.escape(str(s.get("digest", "")))[:16]}</td>'
        f'<td>{s.get("score", 0):.3f}</td>'
        '</tr>'
        for index, s in enumerate(packet.ranked_sources, start=1)
    ) or '<tr><td colspan="6">no ranked sources</td></tr>'
    return (
        '<html><head><title>Knowledge packet</title>'
        '<meta charset="utf-8">'
        '<style>body{font-family:system-ui,sans-serif;max-width:900px;'
        'margin:2rem auto;padding:0 1rem;line-height:1.5}'
        'h1{font-size:1.2rem} pre{white-space:pre-wrap;background:#f6f8fa;'
        'padding:1rem;border-radius:6px;font-size:.9rem}'
        'table{border-collapse:collapse;width:100%} '
        'td,th{border:1px solid #d0d7de;padding:.3rem .5rem;'
        'font-size:.85rem;text-align:left}</style>'
        '<script id="MathJax-script" async '
        'src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-chtml.js">'
        '</script></head><body>'
        f'<h1>Knowledge packet <code>{html.escape(packet_id)}</code></h1>'
        f'<p><strong>Query:</strong> {html.escape(packet.query)}</p>'
        f'<p><strong>Agent:</strong> {packet.agent} · '
        f'<strong>turn:</strong> {packet.turn_kind} '
        f'#{packet.turn_number} · '
        f'<strong>budget:</strong> {packet.token_budget} tokens</p>'
        '<h2>Exact text supplied to the agent</h2>'
        f'<pre>{html.escape(exact)}</pre>'
        '<h2>Ranked sources</h2>'
        '<table><tr><th>#</th><th>kind</th><th>source_id</th>'
        '<th>uri</th><th>digest</th><th>score</th></tr>'
        f'{sources}</table></body></html>'
    )


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
    if settings.cluster_execution_mode != 'fake':
        # A real-execution deployment must be able to permit every runtime
        # profile it can compile; otherwise a GPU-profile task fails later at
        # task preflight with an error that looks like task authoring instead
        # of deployment configuration (issue #502).
        require_profile_runner_images(settings.permitted_job_images)
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
                submission_state_path=settings.cluster_submission_state_path,
            )
        )
    if discord is None:
        link_builder = build_link_builder(settings)
        if (
            settings.discord_enabled
            and settings.discord_bot_token
            and settings.discord_channel_id
        ):
            discord_rest_circuit = DiscordRestCircuit(
                policy=DiscordRestPolicy(
                    circuit_open_failures=(
                        settings.discord_rest_circuit_max_failures
                    ),
                    cooldown_seconds=(
                        settings.discord_rest_circuit_cooldown_seconds
                    ),
                )
            )
            discord = DiscordHttpAdapter(
                bot_token=settings.discord_bot_token,
                channel_id=settings.discord_channel_id,
                webhook_url=settings.discord_webhook_url,
                circuit=discord_rest_circuit,
                attachment_loader=build_report_attachment_loader(
                    store=store,
                    shared_mount_root=settings.shared_mount_root,
                    maximum_bytes=(
                        settings.maximum_discord_artifact_bundle_bytes
                    ),
                ),
                link_builder=link_builder,
            )
        else:
            discord = DisabledDiscordAdapter()
            discord_rest_circuit = None
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
            asset_download_timeout_seconds=(
                settings.task_asset_download_timeout_seconds
            ),
            asset_download_connect_timeout_seconds=(
                settings.task_asset_download_connect_timeout_seconds
            ),
            asset_download_max_retries=settings.task_asset_download_max_retries,
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


def probe_discord_rest(
    *,
    circuit: DiscordRestCircuit,
    token: str,
    transport: httpx.BaseTransport | None = None,
) -> None:
    """Bounded, best-effort REST health probe.

    Runs ``GET /applications/@me`` with the bot token through the same
    guarded executor the projection adapter uses, so an open circuit fails
    fast with zero network attempts and every outcome is recorded in the
    circuit. Exceptions are intentionally swallowed: this is a diagnostic
    probe, not workflow state.
    """

    def attempt() -> httpx.Response:
        with httpx.Client(
            base_url='https://discord.com/api/v10',
            headers={'Authorization': f'Bot {token}'},
            timeout=10,
            transport=transport,
        ) as client:
            return client.get('/applications/@me')

    try:
        execute_guarded(circuit=circuit, policy=circuit.policy, attempt=attempt)
    except (DiscordCircuitOpen, httpx.HTTPError):
        pass


async def _discord_rest_probe_loop(
    circuit: DiscordRestCircuit,
    token: str,
    interval_seconds: float,
) -> None:
    while True:
        await asyncio.sleep(interval_seconds)
        await asyncio.to_thread(
            probe_discord_rest,
            circuit=circuit,
            token=token,
        )


def _discord_rest_status(circuit: DiscordRestCircuit | None) -> str:
    if circuit is None:
        return 'disabled'
    snapshot = circuit.snapshot()
    if snapshot['state'] != 'closed':
        return 'blocked'
    if snapshot['total_successes'] > 0:
        return 'ready'
    return 'unknown'


def _discord_rest_reason(circuit: DiscordRestCircuit | None) -> str | None:
    if circuit is None:
        return None
    snapshot = circuit.snapshot()
    if snapshot['state'] == 'closed':
        return None
    return snapshot['last_outcome_category'] or 'circuit_open'


def redact_event(event: EventRecord) -> EventRecord:
    return event.model_copy(update={'payload': redact_payload(event.payload)})


def redact_action(action: ActionRecord) -> ActionRecord:
    return action.model_copy(
        update={'arguments': redact_payload(action.arguments)}
    )


def redact_artifact(artifact: ArtifactRecord) -> ArtifactRecord:
    return artifact.model_copy(
        update={'metadata': redact_payload(artifact.metadata)}
    )


def redact_run(run: RunRecord) -> RunRecord:
    # Objective stays readable: it is human-authored prose, and the keyword
    # heuristic would replace the entire field rather than a secret inside it.
    update: dict[str, Any] = {}
    if run.task_definition is not None:
        update['task_definition'] = redact_payload(run.task_definition)
    if run.seed_context is not None:
        update['seed_context'] = redact_payload(run.seed_context)
    return run.model_copy(update=update)


def sse_frame(event: EventRecord) -> str:
    redacted = redact_event(event)
    return (
        f'id: {redacted.sequence_number}\n'
        f'event: {redacted.event_type}\n'
        f'data: {json.dumps(redacted.model_dump(mode="json"))}\n\n'
    )


def create_app(
    settings: Settings | None = None,
    *,
    engine: ResearchOrchestrator | None = None,
    start_watcher: bool = True,
) -> FastAPI:
    settings = settings or get_settings()
    engine = engine or build_engine(settings)
    discord_adapter = getattr(engine, 'discord', None)
    discord_rest_circuit = (
        discord_adapter.circuit
        if isinstance(discord_adapter, DiscordHttpAdapter)
        else None
    )
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
        discord_rest_probe_task = (
            asyncio.create_task(
                _discord_rest_probe_loop(
                    discord_rest_circuit,
                    settings.discord_bot_token,
                    settings.discord_rest_probe_interval_seconds,
                ),
                name='research-orchestrator-discord-rest-probe',
            )
            if discord_rest_circuit is not None
            and settings.discord_rest_probe_interval_seconds > 0
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
            if discord_rest_probe_task is not None:
                tasks.append(discord_rest_probe_task)
            await asyncio.gather(*tasks, return_exceptions=True)

    app = FastAPI(
        title='Glasslab Research Orchestrator',
        version=settings.app_version,
        lifespan=lifespan,
        # The auto-generated OpenAPI/docs routes are plain unauthenticated
        # GETs and cannot carry the per-route operator dependency, so they are
        # disabled rather than left outside the read-API auth boundary (#369).
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.engine = engine
    app.state.discord_controls = discord_controls
    app.state.discord_rest = discord_rest_circuit

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
        if isinstance(exc, DatasetUrlError):
            # The failure class is machine-readable so callers can react
            # (retry, fix the URL, verify a checksum) without parsing prose;
            # the message never includes resolved addresses or transport text.
            return HTTPException(
                status_code=409,
                detail={'kind': exc.kind.value, 'message': str(exc)},
            )
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
        # Unexpected failures are server bugs: map_error is called from the
        # handler's except block, so logger.exception records the traceback
        # server-side. The client gets a generic detail because str(exc) can
        # embed stored payload values (e.g. pydantic ValidationError input
        # snapshots) that this surface deliberately redacts on success.
        logger.exception('unhandled %s mapped to HTTP 500', type(exc).__name__)
        return HTTPException(status_code=500, detail='internal server error')

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
            'discord_gateway': (
                'ready'
                if discord_controls is not None
                and discord_controls.client.is_ready()
                else 'disabled'
                if discord_controls is None
                else 'connecting'
            ),
            'discord_rest': _discord_rest_status(
                app.state.discord_rest
            ),
            'discord_rest_reason': _discord_rest_reason(
                app.state.discord_rest
            ),
            'discord_rest_detail': (
                app.state.discord_rest.snapshot()
                if app.state.discord_rest is not None
                else None
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
            # Offload the synchronous compile (a 40-90s OpenCode agent turn on
            # first import) so the event loop — and therefore the Discord
            # Gateway task and /ready probe — stays responsive.
            return await asyncio.to_thread(
                engine.import_task_bundle,
                filename=archive.filename or '',
                content=content,
            )
        except Exception as exc:
            raise map_error(exc) from exc
        finally:
            await archive.close()

    @app.get('/task-bundles', response_model=list[TaskBundleRecord])
    def list_task_bundles(
        _: None = Depends(require_operator),
    ) -> list[TaskBundleRecord]:
        return engine.task_bundles.list()

    @app.post(
        '/datasets/import',
        response_model=CatalogDatasetRecord,
        status_code=status.HTTP_201_CREATED,
    )
    async def import_dataset(
        dataset: UploadFile = File(...),
        name: str = Form(...),
        role: str = Form(default='input'),
        contains_labels: bool = Form(default=False),
        uploaded_by: str | None = Form(default=None),
        _: None = Depends(require_operator),
    ) -> CatalogDatasetRecord:
        try:
            return await asyncio.to_thread(
                engine.datasets.register_upload,
                dataset.file,
                filename=dataset.filename or '',
                name=name,
                role=role,
                contains_labels=contains_labels,
                media_type=dataset.content_type,
                created_by=uploaded_by or 'operator',
            )
        except Exception as exc:
            raise map_error(exc) from exc
        finally:
            await dataset.close()

    @app.get('/datasets/catalog', response_model=list[CatalogDatasetRecord])
    def list_catalog_datasets(
        _: None = Depends(require_operator),
    ) -> list[CatalogDatasetRecord]:
        return engine.store.list_catalog_datasets()

    @app.post(
        '/datasets/register-url',
        response_model=CatalogDatasetRecord,
    )
    def register_dataset_url(
        request: DatasetUrlRegisterRequest,
        _: None = Depends(require_operator),
    ) -> CatalogDatasetRecord:
        try:
            return engine.datasets.register_url(
                url=request.url,
                name=request.name,
                expected_sha256=request.expected_sha256,
                role=request.role,
                contains_labels=request.contains_labels,
                created_by=request.created_by or 'operator',
            )
        except Exception as exc:
            raise map_error(exc) from exc

    @app.get('/datasets', response_model=list[IngestedDatasetRecord])
    def list_datasets(
        _: None = Depends(require_operator),
    ) -> list[IngestedDatasetRecord]:
        return engine.store.list_datasets()

    @app.get(
        '/datasets/{dataset_id}',
        response_model=IngestedDatasetRecord,
    )
    def get_dataset(
        dataset_id: str,
        _: None = Depends(require_operator),
    ) -> IngestedDatasetRecord:
        try:
            return engine.store.get_dataset(dataset_id)
        except Exception as exc:
            raise map_error(exc) from exc

    @app.get('/task-bundles/{task_id}', response_model=TaskBundleRecord)
    def get_task_bundle(
        task_id: str,
        digest: str | None = Query(default=None),
        _: None = Depends(require_operator),
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
        _: None = Depends(require_operator),
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

    def _control_plane_secret_values(settings: Settings) -> tuple[str, ...]:
        """Live control-plane secret VALUES the corpus must never contain.

        Operator uploads skip the broad content heuristic (credential-shaped
        prose is legitimate in operator-curated material); instead the actual
        configured secret values are rejected as substrings, so a real
        credential can still never enter the corpus while textbooks and
        papers are unaffected.
        """
        import os

        raw: list[Any] = [
            settings.operator_api_token,
            settings.discord_bot_token,
            settings.discord_webhook_url,
            settings.workflow_api_token,
            os.environ.get('GLASSLAB_ORCHESTRATOR_STORE_POSTGRES_DSN', ''),
        ]
        values: list[str] = []
        for item in raw:
            if item is None:
                continue
            if isinstance(item, SecretStr):
                item = item.get_secret_value()
            if item:
                values.append(str(item))
        return tuple(values)

    @app.post(
        '/knowledge/sources/upload',
        response_model=KnowledgeSource,
        status_code=status.HTTP_201_CREATED,
    )
    def upload_knowledge_source(
        file: UploadFile = File(...),
        source_type: str = Form(default='documentation'),
        title: str | None = Form(default=None),
        _: None = Depends(require_operator),
    ) -> KnowledgeSource:
        # Operator-only content upload: the HTTP twin of path ingestion for
        # material that lives outside the service filesystem (an operator
        # laptop full of PDFs). Size-capped; content is checked against the
        # LIVE control-plane secret values (not the broad heuristic, which
        # would reject legitimate credential-shaped prose in books).
        data = file.file.read(settings.knowledge_max_source_bytes + 1)
        if len(data) > settings.knowledge_max_source_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=(
                    'upload exceeds knowledge_max_source_bytes '
                    f'({settings.knowledge_max_source_bytes})'
                ),
            )
        try:
            resolved_type = SourceType(source_type)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f'unknown source_type {source_type!r}',
            ) from None
        try:
            return engine.knowledge.ingest_bytes(
                source_type=resolved_type,
                filename=file.filename or 'upload',
                data=data,
                title=title,
                forbidden_values=_control_plane_secret_values(settings),
            )
        except UnsupportedDocumentError as exc:
            raise HTTPException(
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                detail=str(exc),
            ) from exc
        except Exception as exc:
            raise map_error(exc) from exc

    @app.get(
        '/knowledge/sources',
        response_model=KnowledgeSourceListResponse,
    )
    def list_knowledge_sources(
        source_type: str | None = Query(default=None),
        run_scope: str | None = Query(default=None),
        _: None = Depends(require_operator),
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
        # Re-chunking replaces chunk rows, which cascades away their vector
        # rows — so this endpoint must also re-embed, or uploads/rebuilds
        # would silently degrade retrieval to lexical.
        dense_summary: dict[str, object] | None = None
        dense_error: str | None = None
        dense_index = getattr(engine.knowledge, 'dense_index', None)
        if dense_index is not None:
            try:
                from .knowledge_dense import ensure_index_built

                dense_summary = ensure_index_built(
                    dense_index, engine.knowledge.store
                )
            except Exception as exc:  # noqa: BLE001 - dense stays additive
                dense_error = f'{type(exc).__name__}: {exc}'
        response: dict[str, object] = {
            'index_version': 'v1',
            'reindexed_sources': reindexed,
            'dense': dense_summary,
        }
        if dense_error is not None:
            response['dense_error'] = dense_error
        return response

    @app.post(
        '/chat',
        response_model=ResearchAnswer,
        status_code=status.HTTP_201_CREATED,
    )
    async def answer_research_question(
        request: ChatRequest,
        _: None = Depends(require_operator),
    ) -> ResearchAnswer:
        # A research_answer turn is a synchronous agent turn (minutes); the
        # event loop must stay responsive for the Discord Gateway task and
        # /ready probes.
        conversation_id = request.conversation_id or f'chat-{secrets.token_hex(8)}'
        try:
            return await asyncio.to_thread(
                engine.answer_research_question,
                question=request.question,
                conversation_id=conversation_id,
                bind_source_ids=request.bind_source_ids,
            )
        except Exception as exc:
            raise map_error(exc) from exc

    @app.get(
        '/chat/{conversation_id}',
        response_model=dict[str, object],
    )
    def get_conversation(
        conversation_id: str,
        _: None = Depends(require_operator),
    ) -> dict[str, object]:
        binding = engine.store.get_conversation_binding(conversation_id)
        turns = [
            {
                'question': turn.input_event.get('question'),
                'answer': (
                    turn.structured_output.research_answer.answer
                    if turn.structured_output
                    and turn.structured_output.research_answer
                    else None
                ),
                'citations': [
                    citation.model_dump(mode='json')
                    for citation in (
                        turn.structured_output.research_answer.citations
                        if turn.structured_output
                        and turn.structured_output.research_answer
                        else []
                    )
                ],
                'status': turn.status,
            }
            for turn in engine.store.list_turns(conversation_id)
        ]
        return {
            'conversation_id': conversation_id,
            'sources': binding.source_ids if binding else [],
            'turns': turns,
        }

    @app.post(
        '/chat/{conversation_id}/sources',
        response_model=ConversationSourceBinding,
    )
    def bind_conversation_sources(
        conversation_id: str,
        request: ConversationSourceBindRequest,
        _: None = Depends(require_operator),
    ) -> ConversationSourceBinding:
        try:
            return engine.store.bind_conversation_sources(
                conversation_id,
                request.source_ids,
            )
        except Exception as exc:
            raise map_error(exc) from exc

    @app.post(
        '/chat/{conversation_id}/promote',
        response_model=RunRecord,
    )
    def promote_conversation(
        conversation_id: str,
        request: ConversationPromoteRequest,
        _: None = Depends(require_operator),
    ) -> RunRecord:
        try:
            return engine.promote_conversation(
                conversation_id,
                objective=request.objective,
                evaluation_contract_id=request.evaluation_contract_id,
                evaluation_contract_version=(
                    request.evaluation_contract_version
                ),
                investigation_id=request.investigation_id,
            )
        except Exception as exc:
            raise map_error(exc) from exc

    @app.post(
        '/internal/agent-tools/retrieve-evidence',
        response_model=dict[str, object],
    )
    def retrieve_evidence_tool(
        request: RetrieveEvidenceToolRequest,
        supplied_token: str | None = Header(
            default=None,
            alias='X-Glasslab-Tool-Token',
        ),
    ) -> dict[str, object]:
        if not supplied_token:
            raise HTTPException(
                status_code=401,
                detail='tool capability token required',
            )
        try:
            result = engine.execute_knowledge_tool(
                token=supplied_token,
                query=request.query,
                k=request.k,
            )
        except KnowledgeToolDenied as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except KnowledgeToolError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {
            'packet_id': result.packet_id,
            'context': result.context,
            'returned_uris': list(result.uris),
            'verified': list(result.verified),
            'chunk_count': result.chunk_count,
            'bytes_returned': result.bytes_returned,
            'truncated': result.truncated,
        }

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

    @app.get(
        '/knowledge/packets/{packet_id}',
        response_class=HTMLResponse,
    )
    def render_context_packet(
        packet_id: str,
        _: None = Depends(require_operator),
    ) -> HTMLResponse:
        # Level-3 citation page: a Discord citation link resolves here and the
        # operator's browser renders the exact text the agent saw + source
        # metadata. MathJax renders LaTeX when the cluster can reach the CDN;
        # without it the raw LaTeX remains readable.
        try:
            packet = engine.knowledge.get_context_packet(packet_id)
        except Exception as exc:
            raise map_error(exc) from exc
        return HTMLResponse(content=render_context_packet_html(packet, packet_id))

    @app.get('/links/{token}', response_class=HTMLResponse)
    def redeem_link(token: str, request: Request) -> Response:
        # Deliberately NOT gated by require_operator: the signed, expiring
        # token is the entire authorization for this route. A link token can
        # never satisfy require_operator, and the operator token is never
        # accepted here.
        client_host = request.client.host if request.client is not None else None
        if settings.public_base_url is None:
            _audit_link_redemption(
                token=token,
                outcome='links_not_configured',
                payload=None,
                client_host=client_host,
            )
            raise HTTPException(status_code=404, detail='not found')
        if settings.link_signing_secret is None:
            # Fail loudly (503) rather than serving unsigned links: a base URL
            # without a signing secret is a deployment error, mirroring the
            # require_operator fail-closed pattern.
            _audit_link_redemption(
                token=token,
                outcome='signing_secret_missing',
                payload=None,
                client_host=client_host,
            )
            raise HTTPException(
                status_code=503,
                detail='link signing is not configured',
            )
        try:
            payload = verify_link(
                settings.link_signing_secret.get_secret_value(),
                token,
                now=utc_now(),
            )
        except LinkError:
            _audit_link_redemption(
                token=token,
                outcome='invalid',
                payload=None,
                client_host=client_host,
            )
            raise HTTPException(status_code=404, detail='not found') from None
        return serve_link(
            LinkRedemption(
                engine=engine,
                settings=settings,
                payload=payload,
                token=token,
                client_host=client_host,
            )
        )

    @app.get('/runs', response_model=RunListResponse)
    def list_runs(
        _: None = Depends(require_operator),
    ) -> RunListResponse:
        return RunListResponse(
            runs=[redact_run(run) for run in engine.store.list_runs()]
        )

    @app.get('/runs/{run_id}', response_model=RunRecord)
    def get_run(
        run_id: str,
        _: None = Depends(require_operator),
    ) -> RunRecord:
        try:
            return redact_run(engine.store.get_run(run_id))
        except Exception as exc:
            raise map_error(exc) from exc

    @app.get('/runs/{run_id}/events', response_model=EventListResponse)
    def get_events(
        run_id: str,
        after_sequence: int = Query(default=0, ge=0),
        _: None = Depends(require_operator),
    ) -> EventListResponse:
        try:
            engine.store.get_run(run_id)
            return EventListResponse(
                events=[
                    redact_event(event)
                    for event in engine.store.list_events(
                        run_id,
                        after_sequence=after_sequence,
                    )
                ]
            )
        except Exception as exc:
            raise map_error(exc) from exc

    @app.get('/runs/{run_id}/artifacts', response_model=ArtifactListResponse)
    def get_artifacts(
        run_id: str,
        _: None = Depends(require_operator),
    ) -> ArtifactListResponse:
        try:
            engine.store.get_run(run_id)
            return ArtifactListResponse(
                artifacts=[
                    redact_artifact(artifact)
                    for artifact in engine.store.list_artifacts(run_id)
                ]
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
        _: None = Depends(require_operator),
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
    def get_action(
        action_id: str,
        _: None = Depends(require_operator),
    ) -> ActionRecord:
        try:
            return redact_action(engine.store.get_action(action_id))
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
        _: None = Depends(require_operator),
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
                    yield sse_frame(event)
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
