"""FastAPI application factory and pipeline orchestrator for the workflow API.

This module wires together the full research pipeline: intake, interpretation,
assessment, design drafting, run creation, and Kubernetes job submission. It is
the top-level assembly point that receives dependencies (settings, registry,
store, submitter) via create_app() and exposes them through FastAPI's app.state
so route modules can access them without dynamic imports or globals.

Route registrations are delegated to sub-modules (autoresearch, execution,
investigation, literature, schedule, transitions) to keep this file focused on
orchestration and pipeline assembly. The create_app() factory is the single
entry point used by the uvicorn server.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import time
from typing import Any, Iterable
from urllib.parse import urlsplit, urlunsplit
from urllib import request as urllib_request
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from services.common.schemas import ArtifactIndexEntry, ArtifactsIndex, RunManifest, RunStatus, WorkflowRegistryEntry

from .config import Settings, get_settings
from .auth import authenticate_request

from .digest_scheduling import schedule_is_due
from .execution_routes import register_execution_routes
from .execution_preflight import build_execution_preflight_result
from .external_literature import search_external_literature
from .investigation_routes import register_investigation_routes
from .job_submission import JobSubmitter, create_job_submitter
from .literature_routes import register_literature_routes
from .paper_pipeline import (
    auto_resolve_pipeline_design_inputs as auto_resolve_pipeline_design_inputs_impl,
    build_fresh_paper_intake_request as build_fresh_paper_intake_request_impl,
    default_paper_pipeline_request_text as default_paper_pipeline_request_text_impl,
    resolve_replication_repository_url as resolve_replication_repository_url_impl,
)
from .persistence import RunStore, create_run_store
from .registry import WorkflowRegistry
from .schedule_routes import register_schedule_routes
from .source_documents import ingest_source_document, register_source_document_routes
from .technique_catalog import register_technique_catalog_routes
from .stage_interpretation import (
    build_interpretation_record_from_agent_draft,
    call_interpretation_agent,
    validate_interpretation_agent_draft,
)
from .stage_design import (
    build_design_draft as build_design_draft_impl,
    build_design_draft_from_agent_draft as build_design_draft_from_agent_draft_impl,
    build_replicability_assessment as build_replicability_assessment_impl,
    build_replicability_assessment_from_agent_draft as build_replicability_assessment_from_agent_draft_impl,
    call_assessment_agent as call_assessment_agent_impl,
    call_design_agent as call_design_agent_impl,
    choose_workflow_for_intake as choose_workflow_for_intake_impl,
    derive_design_from_intake as derive_design_from_intake_impl,
    refresh_design_method_spec,
    validate_assessment_agent_draft as validate_assessment_agent_draft_impl,
    validate_design_agent_draft as validate_design_agent_draft_impl,
)
from .stage_inference import (
    build_interpretation_notes,
    build_interpretation_record,
    build_intake_record_from_agent_draft,
    build_ranker_candidates,
    call_intake_agent,
    infer_bounded_experiment_ideas,
    infer_dataset_hints,
    infer_evaluation_targets,
    infer_extracted_claims,
    infer_intake_source_type,
    infer_technique_tags,
    infer_literature_state_summary,
    infer_research_gaps,
    infer_unresolved_questions,
    infer_workflow_candidates,
    normalize_unique_strings,
    reorder_intake_candidates_with_ranker,
    resolve_intake_agent_base_url,
    summarize_intake,
    validate_intake_agent_draft,
    validate_ranker_response,
)
from .session_helpers import (
    attach_dataset_to_session,
    append_research_session_memory,
    build_research_problem_request_from_session,
    build_research_session_context,
    build_research_session_literature_digest,
    build_research_session_record,
    get_required_research_session,
    get_required_session_latest_assessment,
    get_required_session_latest_design,
    get_required_session_latest_intake,
    get_required_session_latest_interpretation,
    touch_research_session,
)
from .run_artifacts import (
    MEDIA_TYPES,
    load_artifacts_from_disk,
    load_logs_from_disk,
    resolve_run_status,
)
from .schemas import (
    DatasetCreateRequest,
    DatasetRecord,
    DesignDraftRecord,
    DesignDraftReviewRequest,
    ExecutionPreflightResult,
    FreshPaperPipelineRequest,
    FreshPaperPipelineResponse,
    IntakeCreateRequest,
    IntakeRecord,
    InterpretationRecord,
    LogEntry,
    OperationRecord,
    PaperIntakeCandidateRecord,
    PaperIntakeQueueCreateRequest,
    PaperIntakeQueueRecord,
    PaperPipelineReportState,
    ResearchProblemPaperCandidate,
    ResearchProblemRecord,
    ResearchProblemPipelineRequest,
    ResearchProblemPipelineResponse,
    ResearchSessionNextCommandResponse,
    ResearchSessionRecord,
    ResearchSessionRunCommandResponse,
    SessionDecisionRequest,
    SessionDecisionResponse,
    SessionDatasetAttachRequest,
    SessionIntakeRequest,
    SessionIntakeResponse,
    ReplicabilityAssessmentRecord,
    RunArtifactsResponse,
    RunCreateRequest,
    RunLogsResponse,
    RunRecord,
    ScheduledExecutionRecord,
    ScheduledOperationRecord,
    SourceDocumentIngestRequest,
    SourceDocumentRecord,
    WorkflowFamilySummary,
)
from .validation import validate_run_request

UNRESOLVED_PREFIX = 'UNRESOLVED_'
LOGGER = logging.getLogger(__name__)


DEPRECATED_ROUTE_EXACT_PATHS = {
    '/paper-pipelines/fresh-paper',
    '/paper-pipelines/from-latest-research-problem',
    '/paper-pipelines/from-research-problem',
    '/paper-intake-queues',
    '/paper-intake-queues/from-research-problem',
    '/paper-intake-queues/latest',
    '/paper-intake-queues/{queue_id}',
    '/paper-intake-queues/{queue_id}/stage-next-intake',
    '/research-problems',
    '/research-problems/latest',
    '/research-sessions/bootstrap',
    '/research-sessions/bootstrap-status',
    '/research-sessions/from-latest-research-problem',
    '/research-sessions/start-literature-search',
}

DEPRECATED_ROUTE_PATH_PARTS = (
    '/literature-digest',
    '/paper-intake-queue',
    '/paper-intake-queues',
    '/research-problem',
    '/research-problems',
    '/skills/external-literature-search',
    '/skills/literature-harvest',
    '/skills/paper-intake',
)


def is_deprecated_route_path(path: str) -> bool:
    if path in DEPRECATED_ROUTE_EXACT_PATHS:
        return True
    return any(part in path for part in DEPRECATED_ROUTE_PATH_PARTS)


def mark_deprecated_api_routes(app: FastAPI) -> None:
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        path = getattr(route, 'path_format', route.path)
        if is_deprecated_route_path(path):
            route.deprecated = True


def log_stage_record_source(stage: str, source: str, record_id: str, **context: str) -> None:
    fields = [f'stage={stage}', f'source={source}', f'record_id={record_id}']
    for key in sorted(context):
        value = context[key]
        if value:
            fields.append(f'{key}={value}')
    LOGGER.info('stage-record-created %s', ' '.join(fields))


def redact_dsn_secret(dsn: str) -> str:
    parts = urlsplit(dsn)
    if not parts.password:
        return dsn
    username = parts.username or ''
    host = parts.hostname or ''
    port = f":{parts.port}" if parts.port else ''
    userinfo = f"{username}:***@" if username else "***@"
    netloc = f"{userinfo}{host}{port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def record_operation(
    store: RunStore,
    *,
    operation_type: str,
    started_at: datetime,
    status: str,
    result_detail: str,
    session_id: str | None = None,
    queue_id: str | None = None,
    document_id: str | None = None,
    intake_id: str | None = None,
    error_detail: str | None = None,
) -> OperationRecord:
    record = OperationRecord(
        operation_id=uuid4().hex,
        operation_type=operation_type,
        status=status,
        started_at=started_at,
        finished_at=datetime.now(timezone.utc),
        session_id=session_id,
        queue_id=queue_id,
        document_id=document_id,
        intake_id=intake_id,
        result_detail=result_detail,
        error_detail=error_detail,
    )
    store.save_operation(record)
    return record

def build_replicability_assessment(
    interpretation: InterpretationRecord,
    registry: WorkflowRegistry,
) -> ReplicabilityAssessmentRecord:
    return build_replicability_assessment_impl(interpretation, registry)


def validate_assessment_agent_draft(
    draft: dict[str, Any],
    interpretation: InterpretationRecord,
    registry: WorkflowRegistry,
) -> dict[str, Any]:
    _ = interpretation
    return validate_assessment_agent_draft_impl(draft, registry)


def build_replicability_assessment_from_agent_draft(
    interpretation: InterpretationRecord,
    validated_draft: dict[str, Any],
) -> ReplicabilityAssessmentRecord:
    return build_replicability_assessment_from_agent_draft_impl(interpretation, validated_draft)


def call_assessment_agent(
    interpretation: InterpretationRecord,
    settings: Settings,
    registry: WorkflowRegistry,
) -> ReplicabilityAssessmentRecord | None:
    return call_assessment_agent_impl(interpretation, settings, registry)


def choose_workflow_for_intake(intake: IntakeRecord, registry: WorkflowRegistry) -> WorkflowRegistryEntry | None:
    return choose_workflow_for_intake_impl(intake, registry)


def derive_design_from_intake(intake: IntakeRecord, workflow: WorkflowRegistryEntry) -> tuple[dict[str, Any], list[str], list[str]]:
    return derive_design_from_intake_impl(intake, workflow)


def compute_unresolved_inputs(declared_inputs: dict[str, Any]) -> list[str]:
    return [
        name for name, value in declared_inputs.items() if isinstance(value, str) and value.startswith(UNRESOLVED_PREFIX)
    ]


def default_paper_pipeline_request_text(paper_ref: str) -> str:
    return default_paper_pipeline_request_text_impl(paper_ref)


def build_fresh_paper_intake_request(
    request: FreshPaperPipelineRequest,
    settings: Settings,
) -> IntakeCreateRequest:
    return build_fresh_paper_intake_request_impl(request, settings)


def resolve_replication_repository_url(intake: IntakeRecord) -> str | None:
    return resolve_replication_repository_url_impl(intake)


def auto_resolve_pipeline_design_inputs(
    design: DesignDraftRecord,
    intake: IntakeRecord,
    interpretation: InterpretationRecord,
    request: FreshPaperPipelineRequest,
    settings: Settings,
) -> tuple[dict[str, Any], list[str]]:
    return auto_resolve_pipeline_design_inputs_impl(design, intake, interpretation, request, settings)


def wait_for_terminal_run_state(
    run: RunRecord,
    settings: Settings,
    submitter: JobSubmitter,
    timeout_seconds: float,
    poll_interval_seconds: float,
) -> RunRecord:
    deadline = time.monotonic() + timeout_seconds
    current = run
    while True:
        resolved_status = resolve_run_status(current, settings, submitter)
        current = current.model_copy(update={'status': resolved_status, 'updated_at': resolved_status.updated_at})
        if resolved_status.status in {'succeeded', 'failed', 'rejected'}:
            return current
        if time.monotonic() >= deadline:
            return current
        time.sleep(poll_interval_seconds)


def build_paper_pipeline_report_state(
    run: RunRecord | None,
    settings: Settings,
    submitter: JobSubmitter,
    store: RunStore,
) -> PaperPipelineReportState:
    if run is None:
        return PaperPipelineReportState(
            run_id=None,
            run_status='not-submitted',
            terminal=False,
            report_available=False,
            report_path=None,
            artifact_count=0,
            artifact_names=[],
        )

    resolved_status = resolve_run_status(run, settings, submitter)
    artifacts = load_artifacts_from_disk(settings, run.run_id) or store.get_artifacts(run.run_id)
    artifact_names: list[str] = []
    report_path = None
    if artifacts is not None:
        artifact_names = [artifact.name for artifact in artifacts.artifacts]
        for artifact in artifacts.artifacts:
            if artifact.name == 'report.md':
                report_path = artifact.path
                break

    return PaperPipelineReportState(
        run_id=run.run_id,
        run_status=resolved_status.status,
        terminal=resolved_status.status in {'succeeded', 'failed', 'rejected'},
        report_available=report_path is not None,
        report_path=report_path,
        artifact_count=len(artifact_names),
        artifact_names=artifact_names,
    )


def validate_problem_harvester_response(payload: dict[str, Any]) -> dict[str, Any]:
    selected_tracks = payload.get('selected_tracks')
    selected_queries = payload.get('selected_queries')
    selected_papers = payload.get('selected_papers')
    if not isinstance(selected_tracks, list):
        raise ValueError('problem harvester response missing selected_tracks')
    if not isinstance(selected_queries, list):
        raise ValueError('problem harvester response missing selected_queries')
    if not isinstance(selected_papers, list):
        raise ValueError('problem harvester response missing selected_papers')
    return payload


def call_problem_harvester_plan(
    request: ResearchProblemPipelineRequest,
    settings: Settings,
) -> dict[str, Any]:
    payload = {
        'request_id': uuid4().hex,
        'problem_statement': request.problem_statement,
        'priorities': request.priorities,
        'max_papers': request.max_candidate_papers,
    }
    request_obj = urllib_request.Request(
        resolve_intake_agent_base_url(settings) + '/paper-harvester/plan-from-problem',
        data=json.dumps(payload).encode('utf-8'),
        headers={'Content-Type': 'application/json'},
        method='POST',
    )

    with urllib_request.urlopen(request_obj, timeout=settings.intake_agent_timeout_seconds) as response:
        body = json.loads(response.read().decode('utf-8'))
    return validate_problem_harvester_response(body)


def build_fresh_paper_request_from_problem(
    request: ResearchProblemPipelineRequest,
    chosen_paper: ResearchProblemPaperCandidate,
    selected_track_ids: list[str],
) -> FreshPaperPipelineRequest:
    paper_ref = chosen_paper.official_page or chosen_paper.pdf_url or chosen_paper.paper_id
    notes = [f"Source title: {chosen_paper.title.strip()}"]
    if chosen_paper.abstract_excerpt:
        notes.append(chosen_paper.abstract_excerpt.strip())
    return FreshPaperPipelineRequest(
        paper_ref=paper_ref,
        raw_request=(
            f'Use this source to plan a bounded experiment for the active session goal: '
            f'{request.problem_statement.strip()}'
        ),
        notes=notes,
        submitted_by=request.submitted_by,
        wait_for_terminal_state=request.wait_for_terminal_state,
        wait_timeout_seconds=request.wait_timeout_seconds,
        poll_interval_seconds=request.poll_interval_seconds,
    )


def enrich_intake_with_interpretation_context(
    intake: IntakeRecord,
    interpretation: InterpretationRecord | None,
) -> IntakeRecord:
    if interpretation is None:
        return intake
    extra_notes = list(intake.notes)
    if interpretation.literature_state_summary:
        extra_notes.append('Literature state: ' + interpretation.literature_state_summary)
    if interpretation.bounded_experiment_ideas:
        extra_notes.append('Bounded experiment ideas: ' + '; '.join(interpretation.bounded_experiment_ideas[:2]))
    handoff_parts: list[str] = []
    if interpretation.recommended_method_family:
        handoff_parts.append(f'method_family={interpretation.recommended_method_family}')
    if interpretation.recommended_datasets:
        handoff_parts.append(f"datasets={', '.join(interpretation.recommended_datasets[:3])}")
    if interpretation.recommended_metrics:
        handoff_parts.append(f"metrics={', '.join(interpretation.recommended_metrics[:3])}")
    if interpretation.recommended_baselines:
        handoff_parts.append(f"baselines={', '.join(interpretation.recommended_baselines[:3])}")
    if interpretation.recommended_architectures:
        handoff_parts.append(f"architectures={', '.join(interpretation.recommended_architectures[:3])}")
    if interpretation.recommended_python_packages:
        handoff_parts.append(f"python_packages={', '.join(interpretation.recommended_python_packages[:4])}")
    if interpretation.preferred_workflow_id:
        handoff_parts.append(f'preferred_workflow={interpretation.preferred_workflow_id}')
    if interpretation.preferred_resource_profile:
        handoff_parts.append(f'resource_profile={interpretation.preferred_resource_profile}')
    if interpretation.gpu_required:
        handoff_parts.append('gpu_required=true')
    if interpretation.mutation_axes:
        handoff_parts.append(f"mutation_axes={', '.join(interpretation.mutation_axes[:4])}")
    if handoff_parts:
        extra_notes.append('Methodology handoff: ' + '; '.join(handoff_parts))
    if interpretation.research_gaps:
        extra_notes.append('Research gaps: ' + '; '.join(interpretation.research_gaps[:2]))
    return intake.model_copy(
        update={
            'notes': normalize_unique_strings(extra_notes),
            'workflow_family_candidates': normalize_unique_strings(
                [
                    *intake.workflow_family_candidates,
                    *interpretation.candidate_workflow_families,
                    *( [interpretation.preferred_workflow_id] if interpretation.preferred_workflow_id else [] ),
                ]
            ),
            'updated_at': datetime.now(timezone.utc),
        }
    )


def build_research_problem_record(
    request: ResearchProblemPipelineRequest,
    settings: Settings,
    session_id: str | None = None,
) -> ResearchProblemRecord:
    now = datetime.now(timezone.utc)
    return ResearchProblemRecord(
        problem_id=uuid4().hex,
        created_at=now,
        updated_at=now,
        status='staged',
        problem_statement=request.problem_statement.strip(),
        max_candidate_papers=request.max_candidate_papers,
        priorities=request.priorities,
        submitted_by=request.submitted_by or settings.default_submitted_by,
        session_id=session_id,
    )


def build_research_problem_request_from_record(
    record: ResearchProblemRecord,
    settings: Settings,
) -> ResearchProblemPipelineRequest:
    return ResearchProblemPipelineRequest(
        problem_statement=record.problem_statement,
        max_candidate_papers=record.max_candidate_papers,
        priorities=record.priorities,
        submitted_by=record.submitted_by or settings.default_submitted_by,
    )


def build_paper_intake_queue_record(
    request: PaperIntakeQueueCreateRequest,
    selected_track_ids: list[str],
    selected_queries: list[str],
    selected_papers: list[ResearchProblemPaperCandidate],
    coverage_summary: dict[str, Any],
    warnings: list[str],
    settings: Settings,
    session_id: str | None = None,
) -> PaperIntakeQueueRecord:
    now = datetime.now(timezone.utc)
    candidates = [
        PaperIntakeCandidateRecord(**candidate.model_dump())
        for candidate in selected_papers
    ]
    status_value = 'ready' if candidates else 'exhausted'
    return PaperIntakeQueueRecord(
        queue_id=uuid4().hex,
        created_at=now,
        updated_at=now,
        status=status_value,
        problem_statement=request.problem_statement.strip(),
        selected_tracks=selected_track_ids,
        selected_queries=selected_queries,
        coverage_summary=coverage_summary,
        warnings=warnings,
        candidates=candidates,
        submitted_by=request.submitted_by or settings.default_submitted_by,
        session_id=session_id,
    )


def build_intake_request_from_problem_candidate(
    queue: PaperIntakeQueueRecord,
    candidate: PaperIntakeCandidateRecord,
    document_refs: list[str] | None = None,
    extra_notes: list[str] | None = None,
) -> IntakeCreateRequest:
    def append_unique_note(target: list[str], value: str | None) -> None:
        if not value or value in target:
            return
        target.append(value)

    paper_ref = candidate.official_page or candidate.pdf_url or candidate.paper_id
    manual_source = 'manual' in candidate.tags or 'manual' in candidate.tracks
    notes: list[str] = []
    append_unique_note(notes, f'Source title: {candidate.title.strip()}')
    for note in candidate.first_jobs[:2]:
        append_unique_note(notes, note)
    if not manual_source and candidate.abstract_excerpt:
        append_unique_note(notes, candidate.abstract_excerpt.strip())
    if extra_notes:
        for note in extra_notes:
            append_unique_note(notes, note)
    return IntakeCreateRequest(
        raw_request=(
            'Use this source to plan a bounded experiment for the active session goal: '
            + queue.problem_statement.strip()
        ),
        source_refs=[paper_ref],
        document_refs=document_refs or [],
        source_type='paper-link',
        notes=notes,
        submitted_by=queue.submitted_by,
    )


def build_session_bootstrap_intake_request(
    session: ResearchSessionRecord,
    store: RunStore,
) -> IntakeCreateRequest:
    raw_request = (session.goal_statement or session.title or '').strip()
    if len(raw_request) < 10:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail='research session goal is too sparse to bootstrap an intake',
        )

    def append_unique_note(target: list[str], value: str | None) -> None:
        if not value:
            return
        cleaned = value.strip()
        if cleaned and cleaned not in target:
            target.append(cleaned)

    notes = [item.strip() for item in session.working_notes[-3:] if item.strip()]
    source_refs: list[str] = []
    document_refs: list[str] = []
    active_dataset = store.get_dataset_record(session.latest_dataset_id or '')
    if active_dataset is not None:
        source_refs.append(active_dataset.uri)
        append_unique_note(notes, f'Selected dataset: {active_dataset.name}')
        append_unique_note(notes, f'Dataset URI: {active_dataset.uri}')
        if active_dataset.image_field:
            append_unique_note(notes, f'Dataset image field: {active_dataset.image_field}')
        if active_dataset.label_field:
            append_unique_note(notes, f'Dataset label field: {active_dataset.label_field}')
        if active_dataset.split_strategy:
            append_unique_note(notes, f'Dataset split strategy: {active_dataset.split_strategy}')
        for note in active_dataset.provenance_notes[:2]:
            append_unique_note(notes, note)
    latest_document = store.get_source_document(session.latest_document_id or '')
    if latest_document is not None:
        if latest_document.source_url:
            source_refs.append(latest_document.source_url)
        if latest_document.status == 'fetched':
            document_refs.append(latest_document.document_id)
        append_unique_note(notes, f"Attached source: {latest_document.title or latest_document.source_url}")
        append_unique_note(notes, latest_document.abstract_excerpt)
        for note in latest_document.validation_notes[:2]:
            append_unique_note(notes, note)
    return IntakeCreateRequest(
        raw_request=raw_request,
        source_refs=list(dict.fromkeys(source_refs)),
        document_refs=document_refs,
        technique_tags=infer_technique_tags(raw_request, source_refs, notes),
        notes=notes,
        submitted_by=session.submitted_by,
    )


def summarize_dataset_name(name: str | None, uri: str) -> str:
    cleaned = ' '.join((name or '').split()).strip()
    if cleaned:
        return cleaned[:120]
    tail = uri.rstrip('/').rsplit('/', 1)[-1]
    return (tail or uri)[:120]


def build_dataset_record(
    request: DatasetCreateRequest,
    settings: Settings,
) -> DatasetRecord:
    now = datetime.now(timezone.utc)
    return DatasetRecord(
        dataset_id=uuid4().hex,
        created_at=now,
        updated_at=now,
        status='registered',
        name=summarize_dataset_name(request.name, request.uri.strip()),
        uri=request.uri.strip(),
        modality=request.modality,
        task_type=request.task_type,
        label_field=request.label_field,
        image_field=request.image_field,
        split_strategy=request.split_strategy,
        provenance_notes=request.provenance_notes,
        submitted_by=request.submitted_by or settings.default_submitted_by,
    )


def ensure_session_latest_intake(
    store: RunStore,
    settings: Settings,
    registry: WorkflowRegistry,
    session_id: str,
) -> IntakeRecord:
    session = get_required_research_session(store, session_id)
    intake = store.get_intake(session.latest_intake_id or '')
    if intake is not None:
        return intake
    request = build_session_bootstrap_intake_request(session, store)
    return stage_intake_from_request(request, settings, registry, store, session_id=session_id)


def stage_intake_from_request(
    request: IntakeCreateRequest,
    settings: Settings,
    registry: WorkflowRegistry,
    store: RunStore,
    session_id: str | None = None,
) -> IntakeRecord:
    record = call_intake_agent(request, settings, registry)
    source = 'agent'
    if record is None:
        source = 'deterministic'
        now = datetime.now(timezone.utc)
        intake_id = uuid4().hex
        candidates = [
            workflow_id
            for workflow_id in infer_workflow_candidates(request.raw_request)
            if registry.get_workflow(workflow_id) is not None
        ]
        record = IntakeRecord(
            intake_id=intake_id,
            created_at=now,
            updated_at=now,
            status='ready_for_design',
            source_type=infer_intake_source_type(request),
            source_refs=request.source_refs,
            document_refs=request.document_refs,
            technique_tags=request.technique_tags or infer_technique_tags(request.raw_request, request.source_refs, request.notes),
            raw_request=request.raw_request.strip(),
            normalized_summary=summarize_intake(request.raw_request, request.notes),
            workflow_family_candidates=candidates,
            notes=request.notes,
            submitted_by=request.submitted_by or settings.default_submitted_by,
            session_id=session_id,
        )
    elif request.document_refs:
        record = record.model_copy(
            update={
                'document_refs': normalize_unique_strings(list(record.document_refs) + list(request.document_refs)),
                'technique_tags': normalize_unique_strings(list(record.technique_tags) + list(request.technique_tags)),
                'updated_at': datetime.now(timezone.utc),
                'session_id': session_id or record.session_id,
            }
        )
    elif request.technique_tags:
        record = record.model_copy(
            update={
                'technique_tags': normalize_unique_strings(list(record.technique_tags) + list(request.technique_tags)),
                'updated_at': datetime.now(timezone.utc),
                'session_id': session_id or record.session_id,
            }
        )
    elif session_id and record.session_id != session_id:
        record = record.model_copy(
            update={
                'session_id': session_id,
                'updated_at': datetime.now(timezone.utc),
            }
        )
    record = reorder_intake_candidates_with_ranker(record, settings, registry)
    store.save_intake(record)
    log_stage_record_source(
        'intake',
        source,
        record.intake_id,
        intake_id=record.intake_id,
        submitted_by=record.submitted_by,
    )
    touch_research_session(store, record.session_id, latest_intake_id=record.intake_id)
    return record




def build_design_draft(
    intake: IntakeRecord,
    workflow: WorkflowRegistryEntry,
    submitted_by: str,
    interpretation: InterpretationRecord | None = None,
    source_assessment_id: str | None = None,
) -> DesignDraftRecord:
    return build_design_draft_impl(
        intake,
        workflow,
        submitted_by=submitted_by,
        interpretation=interpretation,
        source_assessment_id=source_assessment_id,
    )


def validate_design_agent_draft(
    draft: dict[str, Any],
    workflow: WorkflowRegistryEntry,
) -> dict[str, Any]:
    return validate_design_agent_draft_impl(draft, workflow)


def build_design_draft_from_agent_draft(
    intake: IntakeRecord,
    workflow: WorkflowRegistryEntry,
    submitted_by: str,
    validated_draft: dict[str, Any],
    source_assessment_id: str | None = None,
) -> DesignDraftRecord:
    return build_design_draft_from_agent_draft_impl(
        intake,
        workflow,
        submitted_by=submitted_by,
        validated_draft=validated_draft,
        source_assessment_id=source_assessment_id,
    )


def call_design_agent(
    intake: IntakeRecord,
    workflow: WorkflowRegistryEntry,
    submitted_by: str,
    settings: Settings,
    source_assessment_id: str | None = None,
) -> DesignDraftRecord | None:
    return call_design_agent_impl(
        intake,
        workflow,
        submitted_by=submitted_by,
        settings=settings,
        source_assessment_id=source_assessment_id,
    )


def review_design_draft(
    design: DesignDraftRecord,
    request: DesignDraftReviewRequest,
) -> DesignDraftRecord:
    now = datetime.now(timezone.utc)
    declared_inputs = dict(design.declared_inputs)
    declared_inputs.update(request.resolved_inputs)
    unresolved_inputs = compute_unresolved_inputs(declared_inputs)
    design_notes = list(design.design_notes)

    for key in sorted(request.resolved_inputs):
        design_notes.append(f'Review resolved input: {key}.')
    design_notes.extend(request.review_notes)

    status_value = 'ready_for_run'
    if unresolved_inputs:
        status_value = 'needs_review'
    if design.approval_tier != 'tier-2-approved-execution':
        status_value = 'needs_review'
        note = f'Approval tier {design.approval_tier} still requires operator review before run creation.'
        if note not in design_notes:
            design_notes.append(note)

    refreshed = design.model_copy(
        update={
            'updated_at': now,
            'declared_inputs': declared_inputs,
            'unresolved_inputs': unresolved_inputs,
            'design_notes': design_notes,
            'status': status_value,
        }
    )
    return refresh_design_method_spec(refreshed)


def build_approved_rerun_schedule(
    request: ApprovedRerunScheduleCreateRequest,
    run: RunRecord,
    settings: Settings,
) -> ScheduledOperationRecord:
    if run.manifest.approval_tier != 'tier-2-approved-execution':
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail='current run is not eligible for approved rerun scheduling',
        )
    if run.status.status != 'succeeded':
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail='current run must have status succeeded before creating an approved rerun schedule',
        )

    dataset_uri = None
    for candidate in ('dataset_uri', 'train_uri'):
        value = run.manifest.inputs.get(candidate)
        if isinstance(value, str) and value.strip():
            dataset_uri = value.strip()
            break

    now = datetime.now(timezone.utc)
    return ScheduledOperationRecord(
        schedule_id=uuid4().hex,
        created_at=now,
        updated_at=now,
        status='active',
        operation_type='approved-rerun',
        approval_tier=run.manifest.approval_tier,
        owner=request.owner or settings.default_submitted_by,
        cron_expr=request.cron_expr.strip(),
        scope_filter={'workflow_id': run.workflow_id, 'source_run_id': run.run_id},
        source_design_id=run.source_design_id,
        source_run_id=run.run_id,
        workflow_id=run.workflow_id,
        allowed_dataset_uri=dataset_uri,
        allowed_model_ids=list(run.manifest.requested_models),
        allowed_runner_image=run.manifest.runner_image,
        resource_profile=run.manifest.resource_profile,
    )


def execute_due_approved_rerun_schedules(
    store: RunStore,
    now: datetime,
    settings: Settings,
    registry: WorkflowRegistry,
    submitter: JobSubmitter,
) -> list[ScheduledExecutionRecord]:
    executions: list[ScheduledExecutionRecord] = []
    for schedule in store.list_schedules(operation_type='approved-rerun'):
        if not schedule_is_due(schedule, now):
            continue
        if schedule.last_execution_at is not None:
            last = schedule.last_execution_at.astimezone(timezone.utc)
            if last.year == now.year and last.month == now.month and last.day == now.day and last.hour == now.hour and last.minute == now.minute:
                continue

        started_at = now
        source_run = store.get_run(schedule.source_run_id or '')
        failure_reason = None
        if source_run is None:
            failure_reason = 'source run not found'
        else:
            resolved_status = resolve_run_status(source_run, settings, submitter)
            if resolved_status != source_run.status:
                source_run = source_run.model_copy(update={'status': resolved_status, 'updated_at': resolved_status.updated_at})
                store.save_run(source_run)
        if failure_reason is None and source_run.status.status != 'succeeded':
            failure_reason = 'source run is no longer succeeded'
        elif failure_reason is None and schedule.workflow_id != source_run.workflow_id:
            failure_reason = 'scheduled workflow_id drifted from source run'
        elif failure_reason is None and schedule.resource_profile != source_run.manifest.resource_profile:
            failure_reason = 'scheduled resource profile drifted from source run'
        elif failure_reason is None and schedule.allowed_runner_image != source_run.manifest.runner_image:
            failure_reason = 'scheduled runner image drifted from source run'
        elif failure_reason is None and schedule.allowed_model_ids != list(source_run.manifest.requested_models):
            failure_reason = 'scheduled model ids drifted from source run'
        elif failure_reason is None:
            dataset_uri = None
            for candidate in ('dataset_uri', 'train_uri'):
                value = source_run.manifest.inputs.get(candidate)
                if isinstance(value, str) and value.strip():
                    dataset_uri = value.strip()
                    break
            if schedule.allowed_dataset_uri != dataset_uri:
                failure_reason = 'scheduled dataset uri drifted from source run'

        if failure_reason is not None:
            execution = ScheduledExecutionRecord(
                execution_id=uuid4().hex,
                schedule_id=schedule.schedule_id,
                operation_type=schedule.operation_type,
                started_at=started_at,
                finished_at=started_at,
                result_status='failed-closed',
                result_detail=failure_reason,
                produced_run_ids=[],
                digest_payload={},
            )
            store.save_execution(execution)
            store.save_schedule(
                schedule.model_copy(
                    update={
                        'updated_at': started_at,
                        'last_execution_at': started_at,
                        'last_result_status': 'failed-closed',
                        'last_result_detail': failure_reason,
                    }
                )
            )
            executions.append(execution)
            continue

        assert source_run is not None
        workflow = registry.get_workflow(source_run.workflow_id)
        if workflow is None:
            detail = 'workflow registry entry not found for scheduled rerun'
            execution = ScheduledExecutionRecord(
                execution_id=uuid4().hex,
                schedule_id=schedule.schedule_id,
                operation_type=schedule.operation_type,
                started_at=started_at,
                finished_at=started_at,
                result_status='failed-closed',
                result_detail=detail,
                produced_run_ids=[],
                digest_payload={},
            )
            store.save_execution(execution)
            store.save_schedule(
                schedule.model_copy(
                    update={
                        'updated_at': started_at,
                        'last_execution_at': started_at,
                        'last_result_status': 'failed-closed',
                        'last_result_detail': detail,
                    }
                )
            )
            executions.append(execution)
            continue

        rerun_request = RunCreateRequest(
            workflow_id=source_run.workflow_id,
            objective=source_run.manifest.objective,
            inputs=source_run.manifest.inputs,
            models=list(source_run.manifest.requested_models),
            resource_profile=source_run.manifest.resource_profile,
            run_priority='autonomous',
            submitted_by=schedule.owner,
        )
        rerun_record = create_run_record(
            rerun_request,
            workflow,
            settings,
            submitter,
            store,
            source_design_id=source_run.source_design_id,
            source_intake_id=source_run.source_intake_id,
            run_purpose='approved-rerun',
            session_id=source_run.session_id,
        )
        finished_at = datetime.now(timezone.utc)
        detail = f'Approved rerun submitted as {rerun_record.run_id}.'
        execution = ScheduledExecutionRecord(
            execution_id=uuid4().hex,
            schedule_id=schedule.schedule_id,
            operation_type=schedule.operation_type,
            started_at=started_at,
            finished_at=finished_at,
            result_status='ok',
            result_detail=detail,
            produced_run_ids=[rerun_record.run_id],
            digest_payload={},
        )
        store.save_execution(execution)
        store.save_schedule(
            schedule.model_copy(
                update={
                    'updated_at': finished_at,
                    'last_execution_at': finished_at,
                    'last_result_status': 'ok',
                    'last_result_detail': detail,
                }
            )
        )
        executions.append(execution)
    return executions


def create_run_record(
    request: RunCreateRequest,
    workflow: WorkflowRegistryEntry,
    settings: Settings,
    submitter: JobSubmitter,
    store: RunStore,
    source_design_id: str | None = None,
    source_intake_id: str | None = None,
    run_purpose: str | None = None,
    session_id: str | None = None,
) -> RunRecord:
    # Persistent failure: validation only blocks this request; unknown or
    # disallowed fields are rejected so the caller can fix their input.
    issues = validate_run_request(request, workflow)
    if issues:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=[issue.model_dump() for issue in issues],
        )

    preflight = build_execution_preflight_result(workflow, settings)
    if not preflight.ready:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                'message': 'execution preflight failed',
                'workflow_id': workflow.workflow_id,
                'blocking_issues': preflight.blocking_issues,
                'warnings': preflight.warnings,
                'eligible_nodes': preflight.eligible_nodes,
            },
        )

    now = datetime.now(timezone.utc)
    run_id = uuid4().hex
    manifest = RunManifest(
        run_id=run_id,
        workflow_id=workflow.workflow_id,
        workflow_family=workflow.workflow_family,
        display_name=workflow.display_name,
        objective=request.objective,
        submitted_by=request.submitted_by or settings.default_submitted_by,
        submitted_at=now,
        run_priority=request.run_priority,
        inputs=request.inputs,
        requested_models=request.models,
        resource_profile=request.resource_profile or workflow.resource_profile.profile_name,
        resource_requests=workflow.resource_profile.requests,
        resource_limits=workflow.resource_profile.limits,
        node_selector=workflow.resource_profile.node_selector,
        runner_image=workflow.runner_image,
        runner_service_account_name=workflow.runner_service_account_name,
        maximum_wallclock_minutes=workflow.max_wallclock_minutes,
        budget={'max_wallclock_minutes': workflow.max_wallclock_minutes},
        entrypoint=list(workflow.default_entrypoint),
        evaluator_type=workflow.evaluator_type,
        approval_tier=workflow.approval_tier,
        expected_artifacts=workflow.expected_artifacts.model_dump(mode='json'),
    )
    status_payload = RunStatus(run_id=run_id, status='accepted', updated_at=now, detail='Run accepted by workflow-api.')
    submission = submitter.submit_run(manifest)
    record = RunRecord(
        run_id=run_id,
        workflow_id=workflow.workflow_id,
        created_at=now,
        updated_at=now,
        manifest=manifest,
        status=status_payload,
        job_submission=submission,
        source_design_id=source_design_id,
        source_intake_id=source_intake_id,
        run_purpose=run_purpose,
        run_priority=request.run_priority,
        session_id=session_id,
    )
    artifacts = build_artifact_index(run_id, workflow.expected_artifacts.required, workflow.expected_artifacts.optional)
    # Touch the session AFTER the run is stored so a crash before submission
    # doesn't leave a dangling latest_run_id pointer to a run that was never
    # actually submitted.
    store.save_run(record)
    touch_research_session(store, session_id, latest_run_id=run_id)
    store.save_artifacts(run_id, artifacts)
    store.append_log(
        run_id,
        LogEntry(
            timestamp=now,
            level='INFO',
            message='run accepted',
            payload={'workflow_id': workflow.workflow_id, 'job_name': submission.job_name},
        ),
    )
    return record


def enrich_run_inputs_with_method_context(design: DesignDraftRecord) -> dict[str, Any]:
    method_spec = getattr(design, 'method_spec', None)
    inputs = dict(method_spec.execution_inputs) if method_spec is not None else dict(design.declared_inputs)
    if method_spec is None:
        return inputs
    if method_spec.candidate_models:
        inputs['technique_candidate_models'] = list(method_spec.candidate_models)
    if method_spec.baseline_models:
        inputs['technique_baseline_models'] = list(method_spec.baseline_models)
    if method_spec.loss_or_distance:
        inputs['technique_loss_or_distance'] = method_spec.loss_or_distance
    if method_spec.task_type:
        inputs['technique_task_type'] = method_spec.task_type
    if method_spec.metrics:
        inputs['technique_metrics'] = list(method_spec.metrics)
    return inputs


def filter_run_inputs_for_workflow(inputs: dict[str, Any], workflow: WorkflowRegistryEntry) -> dict[str, Any]:
    allowed_inputs = {item.name for item in workflow.required_inputs}
    return {key: value for key, value in inputs.items() if key in allowed_inputs}


def resolve_run_requested_models(requested_models: list[str], workflow: WorkflowRegistryEntry) -> list[str]:
    # Only models listed in the registry are allowed; when none of the
    # requested models are in the allow-list, fall back to the first
    # allowed model (not a silent empty list that would fail the
    # min_length=1 constraint on the manifest).
    allowed = list(workflow.allowed_models or [])
    requested = [str(model).strip() for model in requested_models if str(model).strip()]
    compatible = [model for model in requested if model in allowed]
    if compatible:
        return compatible
    if allowed:
        return allowed[:1]
    return requested[:1]

def create_app(
    settings: Settings | None = None,
    registry: WorkflowRegistry | None = None,
    store: RunStore | None = None,
    submitter: JobSubmitter | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    registry = registry or WorkflowRegistry(settings.registry_dir)
    if store is None:
        if settings.store_backend == 'memory' and not settings.allow_inmemory_store:
            raise RuntimeError(
                'workflow-api store backend is set to memory but allow_inmemory_store=false; '
                'choose a durable backend or explicitly allow in-memory mode'
            )
        store = create_run_store(
            settings.store_backend,
            state_path=settings.store_json_path,
            postgres_dsn=settings.store_postgres_dsn,
        )
    submitter = submitter or create_job_submitter(settings)

    app = FastAPI(title=settings.app_name, version=settings.app_version)
    app.state.settings = settings
    app.state.registry = registry
    app.state.store = store
    app.state.submitter = submitter

    @app.middleware('http')
    async def authorize_requests(request: Request, call_next):
        if not (request.method == 'GET' and request.url.path == '/healthz'):
            try:
                authenticate_request(request, settings)
            except HTTPException as exc:
                return JSONResponse(status_code=exc.status_code, content={'detail': exc.detail})
        return await call_next(request)

    @app.get('/healthz')
    def healthz() -> dict:
        return {
            'status': 'ok',
            'app': settings.app_name,
            'version': settings.app_version,
            'build_source_revision': settings.build_source_revision,
            'build_source_label': settings.build_source_label,
            'workflow_count': len(registry.list_workflows()),
            'store_backend': settings.store_backend,
            'store_target': (
                redact_dsn_secret(settings.store_postgres_dsn)
                if settings.store_backend == 'postgres'
                else settings.store_json_path
            ),
        }

    def resolve_latest_session_id(session_id: str) -> str:
        if session_id != 'latest':
            return session_id
        session = store.get_latest_research_session()
        if session is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='no research session has been created yet')
        return session.session_id

    @app.post('/datasets', response_model=DatasetRecord, status_code=status.HTTP_201_CREATED)
    def create_dataset(request: DatasetCreateRequest) -> DatasetRecord:
        record = build_dataset_record(request, settings)
        store.save_dataset_record(record)
        return record

    @app.get('/datasets', response_model=list[DatasetRecord])
    def list_datasets() -> list[DatasetRecord]:
        return store.list_dataset_records()

    @app.get('/datasets/{dataset_id}', response_model=DatasetRecord)
    def get_dataset(dataset_id: str) -> DatasetRecord:
        record = store.get_dataset_record(dataset_id)
        if record is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='dataset not found')
        return record

    @app.get('/research-sessions/{session_id}/datasets', response_model=list[DatasetRecord])
    def list_session_datasets(session_id: str) -> list[DatasetRecord]:
        session_id = resolve_latest_session_id(session_id)
        session = get_required_research_session(store, session_id)
        return [
            record
            for dataset_id in session.dataset_ids
            if (record := store.get_dataset_record(dataset_id)) is not None
        ]

    @app.get('/research-sessions/latest/datasets', response_model=list[DatasetRecord])
    def list_latest_session_datasets() -> list[DatasetRecord]:
        return list_session_datasets('latest')

    @app.post('/research-sessions/{session_id}/datasets', response_model=DatasetRecord, status_code=status.HTTP_201_CREATED)
    def create_session_dataset(session_id: str, request: DatasetCreateRequest) -> DatasetRecord:
        session_id = resolve_latest_session_id(session_id)
        session = get_required_research_session(store, session_id)
        record = build_dataset_record(
            request.model_copy(
                update={
                    'submitted_by': request.submitted_by or session.submitted_by,
                }
            ),
            settings,
        )
        store.save_dataset_record(record)
        attach_dataset_to_session(store, session_id, record)
        return record

    @app.post('/research-sessions/latest/datasets', response_model=DatasetRecord, status_code=status.HTTP_201_CREATED)
    def create_latest_session_dataset(request: DatasetCreateRequest) -> DatasetRecord:
        return create_session_dataset('latest', request)

    @app.post('/research-sessions/{session_id}/datasets/attach', response_model=DatasetRecord)
    def attach_existing_dataset_to_session(session_id: str, request: SessionDatasetAttachRequest) -> DatasetRecord:
        session_id = resolve_latest_session_id(session_id)
        get_required_research_session(store, session_id)
        record = store.get_dataset_record(request.dataset_id)
        if record is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='dataset not found')
        attach_dataset_to_session(store, session_id, record)
        return record

    @app.post('/research-sessions/latest/datasets/attach', response_model=DatasetRecord)
    def attach_existing_dataset_to_latest_session(request: SessionDatasetAttachRequest) -> DatasetRecord:
        return attach_existing_dataset_to_session('latest', request)

    @app.post('/research-sessions/{session_id}/intake', response_model=SessionIntakeResponse)
    def create_session_intake_entry(session_id: str, request: SessionIntakeRequest) -> SessionIntakeResponse:
        session_id = resolve_latest_session_id(session_id)
        session = get_required_research_session(store, session_id)

        provided = [
            name
            for name, value in {
                'source_url': request.source_url,
                'document_url': request.document_url,
                'note': request.note,
                'dataset_uri': request.dataset_uri,
                'baseline_name': request.baseline_name,
            }.items()
            if value
        ]
        if len(provided) != 1:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='exactly one of source_url, document_url, note, dataset_uri, or baseline_name must be provided',
            )

        current_plan_status = None
        current_design = store.get_design_draft(session.latest_design_id or '')
        if current_design is not None:
            current_plan_status = current_design.status

        if request.source_url or request.document_url:
            source_url = request.source_url or request.document_url
            document = ingest_source_document(
                source_url or '',
                submitted_by=request.submitted_by or session.submitted_by,
                settings=settings,
                store=store,
                session_id=session_id,
            )
            updated_session = touch_research_session(store, session_id, latest_document_id=document.document_id) or session
            return SessionIntakeResponse(
                session=updated_session,
                record_type='source_document',
                source_document=document.model_dump(mode='json'),
                current_plan_status=current_plan_status,
            )

        if request.dataset_uri:
            dataset = build_dataset_record(
                DatasetCreateRequest(
                    uri=request.dataset_uri,
                    submitted_by=request.submitted_by or session.submitted_by,
                ),
                settings,
            )
            store.save_dataset_record(dataset)
            updated_session = attach_dataset_to_session(store, session_id, dataset)
            return SessionIntakeResponse(
                session=updated_session,
                record_type='dataset',
                dataset=dataset.model_dump(mode='json'),
                current_plan_status='needs_plan',
            )

        if request.note:
            updated_session = append_research_session_memory(
                store,
                session_id,
                working_note=request.note,
            )
            return SessionIntakeResponse(
                session=updated_session,
                record_type='note',
                recorded_value=request.note,
                current_plan_status=current_plan_status,
            )

        updated_session = append_research_session_memory(
            store,
            session_id,
            working_note=f'baseline: {request.baseline_name}',
        )
        return SessionIntakeResponse(
            session=updated_session,
            record_type='baseline',
            recorded_value=request.baseline_name,
            current_plan_status=current_plan_status,
        )

    @app.post('/research-sessions/latest/intake', response_model=SessionIntakeResponse)
    def create_latest_session_intake_entry(request: SessionIntakeRequest) -> SessionIntakeResponse:
        return create_session_intake_entry('latest', request)

    @app.post('/intakes', response_model=IntakeRecord, status_code=status.HTTP_201_CREATED)
    def create_intake(request: IntakeCreateRequest) -> IntakeRecord:
        return stage_intake_from_request(request, settings, registry, store)

    @app.post('/research-sessions/{session_id}/intakes', response_model=IntakeRecord, status_code=status.HTTP_201_CREATED)
    def create_session_intake(session_id: str, request: IntakeCreateRequest) -> IntakeRecord:
        session_id = resolve_latest_session_id(session_id)
        get_required_research_session(store, session_id)
        return stage_intake_from_request(request, settings, registry, store, session_id=session_id)

    @app.post('/research-sessions/latest/intakes', response_model=IntakeRecord, status_code=status.HTTP_201_CREATED)
    def create_latest_session_intake(request: IntakeCreateRequest) -> IntakeRecord:
        session = store.get_latest_research_session()
        if session is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='no research session has been created yet')
        return create_session_intake(session.session_id, request)

    @app.post('/research-sessions/{session_id}/source-documents/ingest', response_model=SourceDocumentRecord, status_code=status.HTTP_201_CREATED)
    def ingest_session_source_document(session_id: str, request: SourceDocumentIngestRequest) -> SourceDocumentRecord:
        session_id = resolve_latest_session_id(session_id)
        session = get_required_research_session(store, session_id)
        record = ingest_source_document(
            request.source_url,
            submitted_by=request.submitted_by or session.submitted_by,
            settings=settings,
            store=store,
            session_id=session_id,
            expected_title=request.expected_title,
        )
        touch_research_session(store, session_id, latest_document_id=record.document_id)
        return record

    @app.post('/research-sessions/latest/source-documents/ingest', response_model=SourceDocumentRecord, status_code=status.HTTP_201_CREATED)
    def ingest_latest_session_source_document(request: SourceDocumentIngestRequest) -> SourceDocumentRecord:
        session = store.get_latest_research_session()
        if session is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='no research session has been created yet')
        return ingest_session_source_document(session.session_id, request)

    @app.get('/intakes/latest', response_model=IntakeRecord)
    def get_latest_intake() -> IntakeRecord:
        record = store.get_latest_intake()
        if record is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='intake not found')
        return record

    @app.get('/intakes/{intake_id}', response_model=IntakeRecord)
    def get_intake(intake_id: str) -> IntakeRecord:
        record = store.get_intake(intake_id)
        if record is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='intake not found')
        return record

    @app.get('/research-sessions/latest/intake', response_model=IntakeRecord)
    def get_latest_session_intake() -> IntakeRecord:
        session = store.get_latest_research_session()
        if session is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='no research session has been created yet')
        return get_session_intake(session.session_id)

    @app.get('/research-sessions/{session_id}/intake', response_model=IntakeRecord)
    def get_session_intake(session_id: str) -> IntakeRecord:
        session_id = resolve_latest_session_id(session_id)
        return get_required_session_latest_intake(store, session_id)

    @app.post('/interpretations/from-latest-intake', response_model=InterpretationRecord, status_code=status.HTTP_201_CREATED)
    def create_interpretation_from_latest_intake() -> InterpretationRecord:
        intake = store.get_latest_intake()
        if intake is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='intake not found')
        return create_interpretation_for_intake(intake)

    def create_interpretation_for_intake(intake: IntakeRecord) -> InterpretationRecord:
        record = call_interpretation_agent(intake, settings, registry, store)
        source = 'agent'
        if record is None:
            source = 'deterministic'
            record = build_interpretation_record(intake, store)
        store.save_interpretation(record)
        log_stage_record_source(
            'interpretation',
            source,
            record.interpretation_id,
            intake_id=record.intake_id,
            submitted_by=record.submitted_by,
        )
        touch_research_session(
            store,
            record.session_id,
            latest_interpretation_id=record.interpretation_id,
            next_experiment_ideas=list(dict.fromkeys(record.bounded_experiment_ideas)),
        )
        return record

    @app.post('/research-sessions/{session_id}/skills/interpretation', response_model=InterpretationRecord, status_code=status.HTTP_201_CREATED)
    def apply_session_interpretation_skill(session_id: str) -> InterpretationRecord:
        session_id = resolve_latest_session_id(session_id)
        intake = ensure_session_latest_intake(store, settings, registry, session_id)
        return create_interpretation_for_intake(intake)

    @app.post('/research-sessions/latest/skills/interpretation', response_model=InterpretationRecord, status_code=status.HTTP_201_CREATED)
    def apply_latest_session_interpretation_skill() -> InterpretationRecord:
        session = store.get_latest_research_session()
        if session is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='no research session has been created yet')
        return apply_session_interpretation_skill(session.session_id)

    @app.post(
        '/research-sessions/{session_id}/transitions/create-interpretation',
        response_model=InterpretationRecord,
        status_code=status.HTTP_201_CREATED,
    )
    def transition_create_interpretation(session_id: str) -> InterpretationRecord:
        return apply_session_interpretation_skill(session_id)

    @app.post(
        '/research-sessions/latest/transitions/create-interpretation',
        response_model=InterpretationRecord,
        status_code=status.HTTP_201_CREATED,
    )
    def transition_create_latest_interpretation() -> InterpretationRecord:
        return apply_latest_session_interpretation_skill()

    @app.get('/interpretations/latest', response_model=InterpretationRecord)
    def get_latest_interpretation() -> InterpretationRecord:
        record = store.get_latest_interpretation()
        if record is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='interpretation not found')
        return record

    @app.get('/interpretations/{interpretation_id}', response_model=InterpretationRecord)
    def get_interpretation(interpretation_id: str) -> InterpretationRecord:
        record = store.get_interpretation(interpretation_id)
        if record is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='interpretation not found')
        return record

    @app.get('/research-sessions/latest/interpretation', response_model=InterpretationRecord)
    def get_latest_session_interpretation() -> InterpretationRecord:
        session = store.get_latest_research_session()
        if session is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='no research session has been created yet')
        return get_session_interpretation(session.session_id)

    @app.get('/research-sessions/{session_id}/interpretation', response_model=InterpretationRecord)
    def get_session_interpretation(session_id: str) -> InterpretationRecord:
        session_id = resolve_latest_session_id(session_id)
        return get_required_session_latest_interpretation(store, session_id)

    @app.post(
        '/replicability-assessments/from-latest-interpretation',
        response_model=ReplicabilityAssessmentRecord,
        status_code=status.HTTP_201_CREATED,
    )
    def create_replicability_assessment_from_latest_interpretation() -> ReplicabilityAssessmentRecord:
        interpretation = store.get_latest_interpretation()
        if interpretation is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='interpretation not found')
        return create_replicability_assessment_for_interpretation(interpretation)

    def create_replicability_assessment_for_interpretation(
        interpretation: InterpretationRecord,
    ) -> ReplicabilityAssessmentRecord:
        record = call_assessment_agent(interpretation, settings, registry)
        source = 'agent'
        if record is None:
            source = 'deterministic'
            record = build_replicability_assessment(interpretation, registry)
        store.save_replicability_assessment(record)
        log_stage_record_source(
            'assessment',
            source,
            record.assessment_id,
            intake_id=record.intake_id,
            interpretation_id=record.interpretation_id,
            submitted_by=record.submitted_by,
        )
        touch_research_session(
            store,
            record.session_id,
            latest_assessment_id=record.assessment_id,
            decision_log=list(
                dict.fromkeys(
                    [
                        f'assessment recommendation: {record.recommendation}',
                        *record.blocking_reasons,
                    ]
                )
            ),
        )
        return record

    @app.post(
        '/research-sessions/{session_id}/skills/assessment',
        response_model=ReplicabilityAssessmentRecord,
        status_code=status.HTTP_201_CREATED,
    )
    def apply_session_assessment_skill(session_id: str) -> ReplicabilityAssessmentRecord:
        session_id = resolve_latest_session_id(session_id)
        interpretation = get_required_session_latest_interpretation(store, session_id)
        return create_replicability_assessment_for_interpretation(interpretation)

    @app.post(
        '/research-sessions/latest/skills/assessment',
        response_model=ReplicabilityAssessmentRecord,
        status_code=status.HTTP_201_CREATED,
    )
    def apply_latest_session_assessment_skill() -> ReplicabilityAssessmentRecord:
        session = store.get_latest_research_session()
        if session is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='no research session has been created yet')
        return apply_session_assessment_skill(session.session_id)

    @app.get('/replicability-assessments/latest', response_model=ReplicabilityAssessmentRecord)
    def get_latest_replicability_assessment() -> ReplicabilityAssessmentRecord:
        record = store.get_latest_replicability_assessment()
        if record is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='replicability assessment not found')
        return record

    @app.get('/replicability-assessments/{assessment_id}', response_model=ReplicabilityAssessmentRecord)
    def get_replicability_assessment(assessment_id: str) -> ReplicabilityAssessmentRecord:
        record = store.get_replicability_assessment(assessment_id)
        if record is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='replicability assessment not found')
        return record

    @app.get('/research-sessions/latest/assessment', response_model=ReplicabilityAssessmentRecord)
    def get_latest_session_assessment() -> ReplicabilityAssessmentRecord:
        session = store.get_latest_research_session()
        if session is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='no research session has been created yet')
        return get_session_assessment(session.session_id)

    @app.get('/research-sessions/{session_id}/assessment', response_model=ReplicabilityAssessmentRecord)
    def get_session_assessment(session_id: str) -> ReplicabilityAssessmentRecord:
        session_id = resolve_latest_session_id(session_id)
        return get_required_session_latest_assessment(store, session_id)

    @app.post('/design-drafts/from-latest-intake', response_model=DesignDraftRecord, status_code=status.HTTP_201_CREATED)
    def create_design_draft_from_latest_intake() -> DesignDraftRecord:
        intake = store.get_latest_intake()
        if intake is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='intake not found')
        latest_interpretation = store.get_latest_interpretation()
        interpretation = latest_interpretation if latest_interpretation and latest_interpretation.intake_id == intake.intake_id else None
        return create_design_draft_for_intake(intake, interpretation=interpretation)

    def create_design_draft_for_intake(
        intake: IntakeRecord,
        *,
        interpretation: InterpretationRecord | None = None,
        source_assessment_id: str | None = None,
        submitted_by: str | None = None,
        workflow_id: str | None = None,
    ) -> DesignDraftRecord:
        intake_for_design = enrich_intake_with_interpretation_context(intake, interpretation)
        preferred_workflow_id = workflow_id or (
            interpretation.preferred_workflow_id if interpretation is not None and interpretation.preferred_workflow_id else None
        )
        workflow = registry.get_workflow(preferred_workflow_id) if preferred_workflow_id else choose_workflow_for_intake(intake_for_design, registry)
        if workflow is None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail='no approved workflow mapping found')
        effective_submitted_by = submitted_by or intake.submitted_by
        record = call_design_agent(
            intake_for_design,
            workflow,
            submitted_by=effective_submitted_by,
            settings=settings,
            source_assessment_id=source_assessment_id,
        )
        source = 'agent'
        if record is None:
            source = 'deterministic'
            record = build_design_draft(
                intake_for_design,
                workflow,
                submitted_by=effective_submitted_by,
                interpretation=interpretation,
                source_assessment_id=source_assessment_id,
            )
        record = refresh_design_method_spec(record, interpretation=interpretation)
        store.save_design_draft(record)
        log_stage_record_source(
            'design',
            source,
            record.design_id,
            intake_id=record.intake_id,
            source_assessment_id=source_assessment_id,
            workflow_id=record.workflow_id,
            submitted_by=record.submitted_by,
        )
        touch_research_session(store, record.session_id, latest_design_id=record.design_id)
        return record

    @app.post('/design-drafts/from-latest-assessment', response_model=DesignDraftRecord, status_code=status.HTTP_201_CREATED)
    def create_design_draft_from_latest_assessment() -> DesignDraftRecord:
        assessment = store.get_latest_replicability_assessment()
        if assessment is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='replicability assessment not found')
        if assessment.status != 'ready_for_design' or assessment.recommendation != 'proceed':
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail='replicability assessment is not ready_for_design')
        if not assessment.recommended_workflow_id:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail='replicability assessment has no recommended workflow')
        intake = store.get_intake(assessment.intake_id)
        if intake is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='intake not found')
        interpretation = store.get_interpretation(assessment.interpretation_id)
        return create_design_draft_for_intake(
            intake,
            interpretation=interpretation,
            source_assessment_id=assessment.assessment_id,
            submitted_by=assessment.submitted_by,
            workflow_id=assessment.recommended_workflow_id,
        )

    @app.post('/research-sessions/latest/skills/design', response_model=DesignDraftRecord, status_code=status.HTTP_201_CREATED)
    def apply_latest_session_design_skill() -> DesignDraftRecord:
        session = store.get_latest_research_session()
        if session is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='no research session has been created yet')
        return apply_session_design_skill(session.session_id)

    @app.post('/research-sessions/{session_id}/skills/design', response_model=DesignDraftRecord, status_code=status.HTTP_201_CREATED)
    def apply_session_design_skill(session_id: str) -> DesignDraftRecord:
        session_id = resolve_latest_session_id(session_id)
        session = get_required_research_session(store, session_id)
        assessment = store.get_replicability_assessment(session.latest_assessment_id or '')
        if assessment is not None:
            if assessment.status == 'ready_for_design' and assessment.recommendation == 'proceed':
                if not assessment.recommended_workflow_id:
                    raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail='research session assessment has no recommended workflow')
                intake = store.get_intake(assessment.intake_id)
                if intake is None:
                    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='intake not found')
                interpretation = store.get_interpretation(assessment.interpretation_id)
                return create_design_draft_for_intake(
                    intake,
                    interpretation=interpretation,
                    source_assessment_id=assessment.assessment_id,
                    submitted_by=assessment.submitted_by,
                    workflow_id=assessment.recommended_workflow_id,
                )
        intake = ensure_session_latest_intake(store, settings, registry, session_id)
        interpretation = store.get_interpretation(session.latest_interpretation_id or '')
        if interpretation is None or interpretation.intake_id != intake.intake_id:
            interpretation = create_interpretation_for_intake(intake)
        return create_design_draft_for_intake(intake, interpretation=interpretation)

    @app.post(
        '/research-sessions/{session_id}/transitions/prepare-current-plan',
        response_model=DesignDraftRecord,
        status_code=status.HTTP_201_CREATED,
    )
    def transition_prepare_current_plan(session_id: str) -> DesignDraftRecord:
        return apply_session_design_skill(session_id)

    @app.post(
        '/research-sessions/latest/transitions/prepare-current-plan',
        response_model=DesignDraftRecord,
        status_code=status.HTTP_201_CREATED,
    )
    def transition_prepare_latest_current_plan() -> DesignDraftRecord:
        return apply_latest_session_design_skill()

    def launch_design_run(design: DesignDraftRecord) -> RunRecord:
        method_spec = getattr(design, 'method_spec', None)
        if method_spec is not None and method_spec.run_readiness != 'ready':
            detail = 'design method_spec is not ready_for_run'
            if method_spec.blocking_reasons:
                detail += ': ' + '; '.join(method_spec.blocking_reasons[:2])
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)
        if design.status != 'ready_for_run':
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail='design draft is not ready_for_run')
        workflow = registry.get_workflow(design.workflow_id)
        if workflow is None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail='workflow registry entry not found')
        run_request = RunCreateRequest(
            workflow_id=design.workflow_id,
            objective=design.objective,
            inputs=filter_run_inputs_for_workflow(enrich_run_inputs_with_method_context(design), workflow),
            models=resolve_run_requested_models(
                (
                    method_spec.candidate_models
                    if method_spec is not None and method_spec.candidate_models
                    else design.candidate_models
                )
                or workflow.allowed_models[:1],
                workflow,
            ),
            resource_profile=design.resource_profile,
            submitted_by=design.submitted_by,
        )
        return create_run_record(
            run_request,
            workflow,
            settings,
            submitter,
            store,
            source_design_id=design.design_id,
            source_intake_id=design.intake_id,
            run_purpose='validation',
            session_id=design.session_id,
        )

    @app.post(
        '/research-sessions/{session_id}/transitions/run-happy-path',
        response_model=ResearchSessionRunCommandResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def transition_run_session_happy_path(session_id: str) -> ResearchSessionRunCommandResponse:
        session_id = resolve_latest_session_id(session_id)
        session = get_required_research_session(store, session_id)
        design = apply_session_design_skill(session_id)
        run = launch_design_run(design)
        session = get_required_research_session(store, session_id)
        return ResearchSessionRunCommandResponse(session=session, design=design, run=run)

    @app.post(
        '/research-sessions/latest/transitions/run-happy-path',
        response_model=ResearchSessionRunCommandResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def transition_run_latest_session_happy_path() -> ResearchSessionRunCommandResponse:
        session = store.get_latest_research_session()
        if session is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='no research session has been created yet')
        return transition_run_session_happy_path(session.session_id)

    @app.get('/design-drafts/latest', response_model=DesignDraftRecord)
    def get_latest_design_draft() -> DesignDraftRecord:
        record = store.get_latest_design_draft()
        if record is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='design draft not found')
        return record

    @app.get('/design-drafts/{design_id}', response_model=DesignDraftRecord)
    def get_design_draft(design_id: str) -> DesignDraftRecord:
        record = store.get_design_draft(design_id)
        if record is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='design draft not found')
        return record

    @app.get('/research-sessions/latest/design', response_model=DesignDraftRecord)
    def get_latest_session_design() -> DesignDraftRecord:
        session = store.get_latest_research_session()
        if session is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='no research session has been created yet')
        return get_session_design(session.session_id)

    @app.get('/research-sessions/{session_id}/design', response_model=DesignDraftRecord)
    def get_session_design(session_id: str) -> DesignDraftRecord:
        session_id = resolve_latest_session_id(session_id)
        return get_required_session_latest_design(store, session_id)

    @app.get('/research-sessions/{session_id}/preflight/current-plan', response_model=ExecutionPreflightResult)
    def get_session_current_plan_preflight(session_id: str) -> ExecutionPreflightResult:
        session_id = resolve_latest_session_id(session_id)
        design = get_required_session_latest_design(store, session_id)
        workflow = registry.get_workflow(design.workflow_id)
        if workflow is None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail='workflow registry entry not found')
        return build_execution_preflight_result(workflow, settings)

    @app.get('/research-sessions/latest/preflight/current-plan', response_model=ExecutionPreflightResult)
    def get_latest_current_plan_preflight() -> ExecutionPreflightResult:
        session = store.get_latest_research_session()
        if session is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='no research session has been created yet')
        return get_session_current_plan_preflight(session.session_id)

    @app.post('/research-sessions/{session_id}/decisions/current', response_model=SessionDecisionResponse)
    def record_session_current_decision(session_id: str, request: SessionDecisionRequest) -> SessionDecisionResponse:
        session_id = resolve_latest_session_id(session_id)
        session = get_required_research_session(store, session_id)
        decision_text = request.decision
        if request.note:
            decision_text = f'{decision_text}: {request.note}'
        updated_session = append_research_session_memory(
            store,
            session_id,
            decision=decision_text,
        )
        operation = record_operation(
            store,
            operation_type='session-decision',
            started_at=datetime.now(timezone.utc),
            status='completed',
            session_id=session.session_id,
            result_detail=f"recorded decision '{request.decision}'",
        )
        return SessionDecisionResponse(session=updated_session, operation=operation.model_dump(mode='json'))

    @app.post('/research-sessions/latest/decisions/current', response_model=SessionDecisionResponse)
    def record_latest_session_current_decision(request: SessionDecisionRequest) -> SessionDecisionResponse:
        return record_session_current_decision('latest', request)

    @app.post('/design-drafts/latest/review', response_model=DesignDraftRecord)
    def review_latest_design_draft(request: DesignDraftReviewRequest) -> DesignDraftRecord:
        record = store.get_latest_design_draft()
        if record is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='design draft not found')
        updated = review_design_draft(record, request)
        store.save_design_draft(updated)
        touch_research_session(store, updated.session_id, latest_design_id=updated.design_id)
        return updated

    @app.post('/design-drafts/{design_id}/review', response_model=DesignDraftRecord)
    def review_existing_design_draft(design_id: str, request: DesignDraftReviewRequest) -> DesignDraftRecord:
        record = store.get_design_draft(design_id)
        if record is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='design draft not found')
        updated = review_design_draft(record, request)
        store.save_design_draft(updated)
        return updated

    @app.post('/paper-pipelines/fresh-paper', response_model=FreshPaperPipelineResponse, status_code=status.HTTP_201_CREATED)
    def create_fresh_paper_pipeline(request: FreshPaperPipelineRequest) -> FreshPaperPipelineResponse:
        warnings: list[str] = []

        intake_request = build_fresh_paper_intake_request(request, settings)
        intake = call_intake_agent(intake_request, settings, registry)
        intake_source = 'agent'
        if intake is None:
            intake_source = 'deterministic'
            now = datetime.now(timezone.utc)
            candidates = [
                workflow_id
                for workflow_id in infer_workflow_candidates(intake_request.raw_request)
                if registry.get_workflow(workflow_id) is not None
            ]
            intake = IntakeRecord(
                intake_id=uuid4().hex,
                created_at=now,
                updated_at=now,
                status='ready_for_design',
                source_type=infer_intake_source_type(intake_request),
                source_refs=intake_request.source_refs,
                raw_request=intake_request.raw_request.strip(),
                normalized_summary=summarize_intake(intake_request.raw_request, intake_request.notes),
                workflow_family_candidates=candidates,
                notes=intake_request.notes,
                submitted_by=intake_request.submitted_by or settings.default_submitted_by,
            )
        intake = reorder_intake_candidates_with_ranker(intake, settings, registry)
        store.save_intake(intake)
        log_stage_record_source(
            'intake',
            intake_source,
            intake.intake_id,
            intake_id=intake.intake_id,
            submitted_by=intake.submitted_by,
        )

        interpretation = call_interpretation_agent(intake, settings, registry, store)
        interpretation_source = 'agent'
        if interpretation is None:
            interpretation_source = 'deterministic'
            interpretation = build_interpretation_record(intake, store)
        store.save_interpretation(interpretation)
        log_stage_record_source(
            'interpretation',
            interpretation_source,
            interpretation.interpretation_id,
            intake_id=interpretation.intake_id,
            submitted_by=interpretation.submitted_by,
        )

        assessment = call_assessment_agent(interpretation, settings, registry)
        assessment_source = 'agent'
        if assessment is None:
            assessment_source = 'deterministic'
            assessment = build_replicability_assessment(interpretation, registry)
        store.save_replicability_assessment(assessment)
        log_stage_record_source(
            'assessment',
            assessment_source,
            assessment.assessment_id,
            intake_id=assessment.intake_id,
            interpretation_id=assessment.interpretation_id,
            submitted_by=assessment.submitted_by,
        )

        workflow = None
        if assessment.recommended_workflow_id:
            workflow = registry.get_workflow(assessment.recommended_workflow_id)
        if workflow is None:
            workflow = choose_workflow_for_intake(intake, registry)
        if workflow is None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail='no approved workflow mapping found')

        design = call_design_agent(
            intake,
            workflow,
            submitted_by=assessment.submitted_by,
            settings=settings,
            source_assessment_id=assessment.assessment_id,
        )
        design_source = 'agent'
        if design is None:
            design_source = 'deterministic'
            design = build_design_draft(
                intake,
                workflow,
                submitted_by=assessment.submitted_by,
                source_assessment_id=assessment.assessment_id,
            )

        resolved_inputs, review_notes = auto_resolve_pipeline_design_inputs(design, intake, interpretation, request, settings)
        if resolved_inputs:
            design = review_design_draft(
                design,
                DesignDraftReviewRequest(
                    resolved_inputs=resolved_inputs,
                    review_notes=review_notes,
                ),
            )
            warnings.extend(review_notes)
        store.save_design_draft(design)
        log_stage_record_source(
            'design',
            design_source,
            design.design_id,
            intake_id=design.intake_id,
            source_assessment_id=assessment.assessment_id,
            workflow_id=design.workflow_id,
            submitted_by=design.submitted_by,
        )

        if design.status != 'ready_for_run':
            if design.unresolved_inputs:
                warnings.append('design still has unresolved inputs after bounded auto-review')
            report_state = build_paper_pipeline_report_state(None, settings, submitter, store)
            return FreshPaperPipelineResponse(
                intake=intake,
                interpretation=interpretation,
                assessment=assessment,
                design=design,
                run=None,
                report_state=report_state,
                warnings=warnings,
                next_action='review-required',
            )

        run_request = RunCreateRequest(
            workflow_id=design.workflow_id,
            objective=design.objective,
            inputs=design.declared_inputs,
            models=design.candidate_models or workflow.allowed_models[:1],
            resource_profile=design.resource_profile,
            submitted_by=design.submitted_by,
        )
        run = create_run_record(
            run_request,
            workflow,
            settings,
            submitter,
            store,
            source_design_id=design.design_id,
            source_intake_id=design.intake_id,
            run_purpose='paper-pipeline',
            session_id=design.session_id,
        )
        if request.wait_for_terminal_state:
            run = wait_for_terminal_run_state(
                run,
                settings,
                submitter,
                timeout_seconds=request.wait_timeout_seconds,
                poll_interval_seconds=request.poll_interval_seconds,
            )
            store.save_run(run)

        report_state = build_paper_pipeline_report_state(run, settings, submitter, store)
        next_action = 'await-run-completion'
        if report_state.terminal and report_state.report_available:
            next_action = 'report-ready'
        elif report_state.terminal:
            next_action = 'inspect-run-state'

        return FreshPaperPipelineResponse(
            intake=intake,
            interpretation=interpretation,
            assessment=assessment,
            design=design,
            run=run,
            report_state=report_state,
            warnings=warnings,
            next_action=next_action,
        )

    register_literature_routes(
        app,
        settings=settings,
        registry=registry,
        store=store,
        create_fresh_paper_pipeline=lambda *args, **kwargs: create_fresh_paper_pipeline(*args, **kwargs),
        call_problem_harvester_plan=lambda *args, **kwargs: call_problem_harvester_plan(*args, **kwargs),
        build_fresh_paper_request_from_problem=lambda *args, **kwargs: build_fresh_paper_request_from_problem(*args, **kwargs),
        build_research_session_record=lambda *args, **kwargs: build_research_session_record(*args, **kwargs),
        build_research_session_context=lambda *args, **kwargs: build_research_session_context(*args, **kwargs),
        build_research_session_literature_digest=lambda *args, **kwargs: build_research_session_literature_digest(*args, **kwargs),
        append_research_session_memory=lambda *args, **kwargs: append_research_session_memory(*args, **kwargs),
        build_research_problem_request_from_session=lambda *args, **kwargs: build_research_problem_request_from_session(*args, **kwargs),
        build_research_problem_record=lambda *args, **kwargs: build_research_problem_record(*args, **kwargs),
        build_research_problem_request_from_record=lambda *args, **kwargs: build_research_problem_request_from_record(*args, **kwargs),
        touch_research_session=lambda *args, **kwargs: touch_research_session(*args, **kwargs),
        build_paper_intake_queue_record=lambda *args, **kwargs: build_paper_intake_queue_record(*args, **kwargs),
        ingest_source_document=lambda *args, **kwargs: ingest_source_document(*args, **kwargs),
        build_intake_request_from_problem_candidate=lambda *args, **kwargs: build_intake_request_from_problem_candidate(*args, **kwargs),
        stage_intake_from_request=lambda *args, **kwargs: stage_intake_from_request(*args, **kwargs),
        record_operation=lambda *args, **kwargs: record_operation(*args, **kwargs),
        search_external_literature=lambda *args, **kwargs: search_external_literature(*args, **kwargs),
    )

    register_source_document_routes(app, store=store)
    register_technique_catalog_routes(app, store=store)

    @app.get('/operations', response_model=list[OperationRecord])
    def list_operations(operation_type: str | None = None) -> list[OperationRecord]:
        return store.list_operations(operation_type=operation_type)

    @app.get('/operations/latest', response_model=OperationRecord)
    def get_latest_operation() -> OperationRecord:
        record = store.get_latest_operation()
        if record is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='operation not found')
        return record

    @app.get('/operations/{operation_id}', response_model=OperationRecord)
    def get_operation(operation_id: str) -> OperationRecord:
        record = store.get_operation(operation_id)
        if record is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='operation not found')
        return record

    launch_generic_experiment_run = register_execution_routes(
        app,
        settings=settings,
        registry=registry,
        store=store,
        submitter=submitter,
        create_run_record=lambda *args, **kwargs: create_run_record(*args, **kwargs),
    )
    register_investigation_routes(
        app,
        settings=settings,
        registry=registry,
        store=store,
        launch_experiment_run=launch_generic_experiment_run,
    )
    register_schedule_routes(
        app,
        settings=settings,
        registry=registry,
        store=store,
        submitter=submitter,
        build_approved_rerun_schedule=lambda *args, **kwargs: build_approved_rerun_schedule(*args, **kwargs),
        execute_due_approved_rerun_schedules=lambda *args, **kwargs: execute_due_approved_rerun_schedules(*args, **kwargs),
    )

    mark_deprecated_api_routes(app)

    return app


def build_artifact_index(run_id: str, required: Iterable[str], optional: Iterable[str]) -> ArtifactsIndex:
    artifacts: list[ArtifactIndexEntry] = []
    for name, is_required in [(item, True) for item in required] + [(item, False) for item in optional]:
        path = f'runs/{run_id}/{name}'
        suffix = '' if name.endswith('/') else name[name.rfind('.'):]
        media_type = 'inode/directory' if name.endswith('/') else MEDIA_TYPES.get(suffix, 'application/octet-stream')
        artifacts.append(
            ArtifactIndexEntry(
                name=name,
                path=path,
                media_type=media_type,
                required=is_required,
                description='Declared by workflow registry',
            )
        )
    return ArtifactsIndex(run_id=run_id, artifacts=artifacts)
