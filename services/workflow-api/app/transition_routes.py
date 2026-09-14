"""HTTP routes for the streamlined research transition workflow.

Provides the "transitions" API surface: promote a paper candidate to an intake
record, create an interpretation (always via the interpretation agent), derive
a methodology/design draft from the interpretation, and launch a validation run
from an approved design. The intake-staging function supports both agent and
deterministic paths, with the deterministic fallback using keyword-based
inference. Imports are intentionally placed mid-file (after helper defs) to
break a circular dependency with stage_design.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable
from uuid import uuid4

from fastapi import FastAPI, HTTPException, status

from .config import Settings
from .job_submission import create_job_submitter
from .persistence import RunStore
from .registry import WorkflowRegistry
from .schemas import (
    CreateInterpretationRequest,
    CreateInterpretationResponse,
    CreateMethodologyDraftRequest,
    CreateMethodologyDraftResponse,
    IntakeCreateRequest,
    IntakeRecord,
    InterpretationRecord,
    PaperIntakeCandidateRecord,
    PaperIntakeQueueRecord,
    PromotePaperToIntakeRequest,
    PromotePaperToIntakeResponse,
    RunCreateRequest,
    RunRecord,
    CreateValidationRunRequest,
    CreateValidationRunResponse,
)
from .session_helpers import touch_research_session


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

    paper_ref = (
        next(iter(build_source_fetch_candidates(candidate.official_page, candidate.pdf_url)), None)
        or candidate.paper_id
    )
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
# Imports below are intentionally placed here to break a circular dependency:
# stage_design calls back into stage_inference functions when building
# design drafts, and transition_routes needs both modules.
from .source_documents import build_source_fetch_candidates
from .stage_design import build_design_draft as build_design_draft_impl
from .stage_inference import (
    build_interpretation_record,
    call_intake_agent,
    build_intake_record_from_agent_draft as build_intake_record_from_agent_draft_impl,
    infer_intake_source_type,
    infer_technique_tags,
    summarize_intake,
    reorder_intake_candidates_with_ranker,
    normalize_unique_strings,
    infer_workflow_candidates,
)
from .stage_interpretation import call_interpretation_agent


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
    touch_research_session(store, record.session_id, latest_intake_id=record.intake_id)
    return record


def register_transitions_routes(
    app: FastAPI,
    *,
    settings: Settings,
    registry: WorkflowRegistry,
    store: RunStore,
    create_run_record_impl: Callable[..., RunRecord],
    build_research_problem_record_impl: Callable[..., Any],
) -> None:
    @app.post('/transitions/promote-paper-to-intake', response_model=PromotePaperToIntakeResponse)
    def promote_paper_to_intake(request: PromotePaperToIntakeRequest) -> PromotePaperToIntakeResponse:
        queue_record = store.get_paper_intake_queue(request.queue_id)
        if queue_record is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='paper intake queue not found')
        
        candidate = next((c for c in queue_record.candidates if c.paper_id == request.paper_id), None)
        if candidate is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='paper candidate not found in queue')
        
        intake_request = build_intake_request_from_problem_candidate(queue_record, candidate)
        intake_record = stage_intake_from_request(intake_request, settings, registry, store, session_id=queue_record.session_id)
        
        return PromotePaperToIntakeResponse(
            intake_id=intake_record.intake_id,
            intake_status=intake_record.status,
            summary=intake_record.normalized_summary,
        )

    @app.post('/transitions/create-interpretation', response_model=CreateInterpretationResponse)
    def create_interpretation(request: CreateInterpretationRequest) -> CreateInterpretationResponse:
        intake_record = store.get_intake(request.intake_id)
        if intake_record is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='intake not found')
        
        interpretation = call_interpretation_agent(intake_record, settings, registry, store)
        if interpretation is None:
            interpretation = build_interpretation_record(intake_record, store)
        
        store.save_interpretation(interpretation)
        touch_research_session(store, interpretation.session_id, latest_interpretation_id=interpretation.interpretation_id)
        
        return CreateInterpretationResponse(
            interpretation_id=interpretation.interpretation_id,
            status=interpretation.status,
            recommended_workflow_id=interpretation.preferred_workflow_id,
        )

    @app.post('/transitions/create-methodology-draft', response_model=CreateMethodologyDraftResponse)
    def create_methodology_draft(request: CreateMethodologyDraftRequest) -> CreateMethodologyDraftResponse:
        interpretation_record = store.get_interpretation(request.interpretation_id)
        if interpretation_record is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='interpretation not found')
        
        workflow = registry.get_workflow(interpretation_record.preferred_workflow_id)
        if workflow is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='preferred workflow not found in registry')
        
        design_record = build_design_draft_impl(
            intake=store.get_intake(interpretation_record.intake_id),
            workflow=workflow,
            submitted_by=interpretation_record.submitted_by,
            interpretation=interpretation_record,
        )
        
        store.save_design_draft(design_record)
        touch_research_session(store, design_record.session_id, latest_design_id=design_record.design_id)
        
        return CreateMethodologyDraftResponse(
            design_id=design_record.design_id,
            status=design_record.status,
            workflow_id=design_record.workflow_id,
        )

    @app.post('/transitions/create-validation-run', response_model=CreateValidationRunResponse)
    def create_validation_run(request: CreateValidationRunRequest) -> CreateValidationRunResponse:
        design_record = store.get_design_draft(request.design_id)
        if design_record is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='design draft not found')
        
        if design_record.status not in {'ready_for_run', 'approved_for_run'}:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f'design status {design_record.status} is not eligible for run creation'
            )
        
        workflow = registry.get_workflow(design_record.workflow_id)
        if workflow is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='workflow not found')
        
        run_request = RunCreateRequest(
            workflow_id=design_record.workflow_id,
            objective=design_record.objective,
            inputs=design_record.declared_inputs,
            models=list(design_record.candidate_models) if design_record.candidate_models else list(workflow.allowed_models or []),
            resource_profile=design_record.resource_profile,
            run_priority='user',
            submitted_by=design_record.submitted_by,
        )
        
        run_record = create_run_record_impl(
            run_request,
            workflow,
            settings,
            create_job_submitter(settings),
            store,
            source_design_id=design_record.design_id,
            source_intake_id=design_record.intake_id,
            run_purpose='validation',
        )
        
        touch_research_session(store, run_record.session_id, latest_run_id=run_record.run_id)
        
        return CreateValidationRunResponse(
            run_id=run_record.run_id,
            run_status=run_record.status.status,
            workflow_id=run_record.workflow_id,
        )
