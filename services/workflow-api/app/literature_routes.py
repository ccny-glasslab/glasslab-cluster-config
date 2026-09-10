"""HTTP routes for research sessions, literature pipelines, and paper intake.

Covers the full front-door lifecycle: create research sessions from problem
statements or staged research problems, bootstrap sessions, run literature
searches (both internal harvester and external API), manage paper intake
queues, fetch source documents, stage intakes from queued candidates, and
append session memory. Every route that mutates session state also touches
the session timestamp for liveness tracking.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import re
from typing import Any, Callable
from urllib import error as urllib_error

from fastapi import FastAPI, HTTPException, status

from .config import Settings
from .persistence import RunStore
from .registry import WorkflowRegistry
from .session_helpers import create_research_session_from_problem
from .schemas import (
    FreshPaperPipelineResponse,
    IntakeRecord,
    LiteratureDigestResponse,
    OperationRecord,
    PaperIntakeCandidateRecord,
    ManualPaperCandidateCreateRequest,
    PaperIntakeQueueCreateRequest,
    PaperIntakeQueueRecord,
    ResearchSessionBootstrapStatusResponse,
    ResearchSessionBootstrapResponse,
    ResearchSessionContextResponse,
    ResearchSessionCreateRequest,
    ResearchSessionMemoryAppendRequest,
    ResearchSessionRecord,
    ResearchProblemPaperCandidate,
    ResearchProblemRecord,
    ResearchProblemPipelineRequest,
    ResearchProblemPipelineResponse,
    StartLiteratureSearchRequest,
    StartLiteratureSearchResponse,
    SourceDocumentRecord,
)
from .external_literature import ExternalLiteratureResult
from .source_documents import build_source_fetch_candidates, derive_arxiv_pdf_url


def register_literature_routes(
    app: FastAPI,
    *,
    settings: Settings,
    registry: WorkflowRegistry,
    store: RunStore,
    create_fresh_paper_pipeline: Callable[[Any], FreshPaperPipelineResponse],
    call_problem_harvester_plan: Callable[[ResearchProblemPipelineRequest, Settings], dict[str, Any]],
    build_fresh_paper_request_from_problem: Callable[[ResearchProblemPipelineRequest, ResearchProblemPaperCandidate, list[str]], Any],
    build_research_session_record: Callable[[ResearchSessionCreateRequest, Settings], ResearchSessionRecord],
    build_research_session_context: Callable[[ResearchSessionRecord, RunStore], ResearchSessionContextResponse],
    build_research_session_literature_digest: Callable[[ResearchSessionRecord, RunStore], LiteratureDigestResponse],
    append_research_session_memory: Callable[..., ResearchSessionRecord],
    build_research_problem_request_from_session: Callable[[ResearchSessionRecord, Settings], ResearchProblemPipelineRequest],
    build_research_problem_record: Callable[..., ResearchProblemRecord],
    build_research_problem_request_from_record: Callable[[ResearchProblemRecord, Settings], ResearchProblemPipelineRequest],
    touch_research_session: Callable[..., ResearchSessionRecord | None],
    build_paper_intake_queue_record: Callable[..., PaperIntakeQueueRecord],
    ingest_source_document: Callable[..., SourceDocumentRecord],
    build_intake_request_from_problem_candidate: Callable[..., Any],
    stage_intake_from_request: Callable[..., IntakeRecord],
    record_operation: Callable[..., OperationRecord],
    search_external_literature: Callable[..., ExternalLiteratureResult],
) -> None:
    def _normalize_goal_statement(value: str | None) -> str:
        if not value:
            return ''
        return re.sub(r'\s+', ' ', value).strip().casefold()

    @app.post('/paper-pipelines/from-research-problem', response_model=ResearchProblemPipelineResponse, status_code=status.HTTP_201_CREATED)
    async def create_pipeline_from_research_problem(request: ResearchProblemPipelineRequest) -> ResearchProblemPipelineResponse:
        try:
            plan_payload = call_problem_harvester_plan(request, settings)
        except (urllib_error.URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f'problem harvester unavailable: {exc}')

        selected_track_ids = [
            str(item.get('track_id', '')).strip()
            for item in plan_payload.get('selected_tracks', [])
            if isinstance(item, dict) and str(item.get('track_id', '')).strip()
        ]
        selected_queries = [
            query.strip()
            for item in plan_payload.get('selected_queries', [])
            if isinstance(item, dict)
            for query in item.get('queries', [])
            if isinstance(query, str) and query.strip()
        ]
        selected_papers = [
            ResearchProblemPaperCandidate.model_validate(item)
            for item in plan_payload.get('selected_papers', [])
            if isinstance(item, dict)
        ]
        coverage_summary = plan_payload.get('coverage_summary', {})
        if not isinstance(coverage_summary, dict):
            coverage_summary = {}
        warnings = [
            warning.strip()
            for warning in plan_payload.get('warnings', [])
            if isinstance(warning, str) and warning.strip()
        ]
        if not selected_papers:
            return ResearchProblemPipelineResponse(
                problem_statement=request.problem_statement,
                selected_tracks=selected_track_ids,
                selected_queries=selected_queries,
                selected_papers=[],
                chosen_paper_id=None,
                pipeline=None,
                warnings=warnings,
                next_action='no-paper-candidates',
            )

        chosen_paper = selected_papers[0]
        fresh_request = build_fresh_paper_request_from_problem(request, chosen_paper, selected_track_ids)
        pipeline = await create_fresh_paper_pipeline(fresh_request)
        return ResearchProblemPipelineResponse(
            problem_statement=request.problem_statement,
            selected_tracks=selected_track_ids,
            selected_queries=selected_queries,
            selected_papers=selected_papers,
            chosen_paper_id=chosen_paper.paper_id,
            pipeline=pipeline,
            warnings=warnings,
            next_action=pipeline.next_action,
        )

    @app.post('/research-sessions', response_model=ResearchSessionRecord, status_code=status.HTTP_201_CREATED)
    def create_research_session(request: ResearchSessionCreateRequest) -> ResearchSessionRecord:
        record = build_research_session_record(request, settings)
        store.save_research_session(record)
        return record

    @app.get('/research-sessions', response_model=list[ResearchSessionRecord])
    def list_research_sessions() -> list[ResearchSessionRecord]:
        return store.list_research_sessions()

    @app.get('/research-sessions/latest', response_model=ResearchSessionRecord)
    def get_latest_research_session() -> ResearchSessionRecord:
        record = store.get_latest_research_session()
        if record is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='no research session has been created yet')
        return record

    @app.get('/research-sessions/bootstrap-status', response_model=ResearchSessionBootstrapStatusResponse)
    def get_research_session_bootstrap_status() -> ResearchSessionBootstrapStatusResponse:
        session = store.get_latest_research_session()
        problem = store.get_latest_research_problem()
        if session is not None:
            return ResearchSessionBootstrapStatusResponse(
                active_session=session,
                staged_research_problem=problem,
                recommended_next_action='apply-session-skills',
                can_create_session_from_latest_problem=problem is not None and problem.session_id is None,
                can_apply_session_skills=True,
                detail='an active research session exists; continue with session skills',
            )
        if problem is not None:
            return ResearchSessionBootstrapStatusResponse(
                active_session=None,
                staged_research_problem=problem,
                recommended_next_action='create-session-from-latest-problem',
                can_create_session_from_latest_problem=True,
                can_apply_session_skills=False,
                detail='a research problem is staged, but no active session exists yet',
            )
        return ResearchSessionBootstrapStatusResponse(
            active_session=None,
            staged_research_problem=None,
            recommended_next_action='create-session-manually',
            can_create_session_from_latest_problem=False,
            can_apply_session_skills=False,
            detail='no active research session or staged research problem exists yet',
        )

    @app.post('/research-sessions/bootstrap', response_model=ResearchSessionBootstrapResponse)
    def bootstrap_research_session() -> ResearchSessionBootstrapResponse:
        session = store.get_latest_research_session()
        problem = store.get_latest_research_problem()
        if session is not None:
            return ResearchSessionBootstrapResponse(
                bootstrap_action='reuse-active-session',
                session=session,
                staged_research_problem=problem,
                detail='an active research session already exists; reuse it and continue with session skills',
            )
        if problem is not None:
            session = create_research_session_from_problem(problem, settings, build_research_session_record)
            store.save_research_session(session)
            problem = problem.model_copy(update={'session_id': session.session_id, 'updated_at': datetime.now(timezone.utc)})
            store.save_research_problem(problem)
            touch_research_session(store, session.session_id, latest_problem_id=problem.problem_id)
            return ResearchSessionBootstrapResponse(
                bootstrap_action='created-session-from-latest-problem',
                session=store.get_research_session(session.session_id) or session,
                staged_research_problem=problem,
                detail='created a research session from the current staged research problem',
            )
        return ResearchSessionBootstrapResponse(
            bootstrap_action='create-session-manually',
            session=None,
            staged_research_problem=None,
            detail='no active research session or staged research problem exists yet',
        )

    @app.post('/research-sessions/start-literature-search', response_model=StartLiteratureSearchResponse, status_code=status.HTTP_201_CREATED)
    def start_literature_search(request: StartLiteratureSearchRequest) -> StartLiteratureSearchResponse:
        started_at = datetime.now(timezone.utc)
        action_parts: list[str] = []

        session = store.get_latest_research_session()
        if session is None:
            problem = store.get_latest_research_problem()
            if problem is not None and problem.session_id is None:
                session = create_research_session_from_problem(problem, settings, build_research_session_record)
                store.save_research_session(session)
                problem = problem.model_copy(update={'session_id': session.session_id, 'updated_at': datetime.now(timezone.utc)})
                store.save_research_problem(problem)
                touch_research_session(store, session.session_id, latest_problem_id=problem.problem_id)
                action_parts.append('created-session-from-latest-problem')
            elif request.goal_statement:
                session_request = ResearchSessionCreateRequest(
                    title=None,
                    goal_statement=request.goal_statement,
                    priorities=request.priorities,
                    submitted_by=request.submitted_by or 'openclaw-operator',
                )
                session = build_research_session_record(session_request, settings)
                store.save_research_session(session)
                action_parts.append('created-session-from-goal')
            else:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail='no active research session or staged research problem exists yet',
                )
        else:
            requested_goal = _normalize_goal_statement(request.goal_statement)
            active_goal = _normalize_goal_statement(session.goal_statement)
            if requested_goal and requested_goal != active_goal:
                session_request = ResearchSessionCreateRequest(
                    title=None,
                    goal_statement=request.goal_statement or session.goal_statement,
                    priorities=request.priorities,
                    submitted_by=request.submitted_by or 'openclaw-operator',
                )
                session = build_research_session_record(session_request, settings)
                store.save_research_session(session)
                problem = None
                action_parts.append('created-session-from-new-goal')
            else:
                problem = store.get_research_problem(session.latest_problem_id or '')
                action_parts.append('reused-active-session')

        if problem is None:
            problem_request = build_research_problem_request_from_session(session, settings)
            problem = build_research_problem_record(problem_request, settings, session_id=session.session_id)
            store.save_research_problem(problem)
            session = touch_research_session(store, session.session_id, latest_problem_id=problem.problem_id) or session
            action_parts.append('staged-research-problem')

        queue = store.get_paper_intake_queue(session.latest_queue_id or '')
        queue_matches_problem = False
        if queue is not None:
            queue_matches_problem = (
                queue.session_id == session.session_id
                and _normalize_goal_statement(queue.problem_statement) == _normalize_goal_statement(problem.problem_statement)
            )

        if queue is None or not queue_matches_problem:
            queue_request = PaperIntakeQueueCreateRequest(
                problem_statement=problem.problem_statement,
                max_candidate_papers=min(problem.max_candidate_papers, 25),
                priorities=problem.priorities,
                submitted_by=problem.submitted_by,
            )
            queue = create_paper_intake_queue_from_research_problem(queue_request)
            if queue.session_id != session.session_id:
                queue = queue.model_copy(update={'session_id': session.session_id, 'updated_at': datetime.now(timezone.utc)})
                store.save_paper_intake_queue(queue)
            session = touch_research_session(store, session.session_id, latest_queue_id=queue.queue_id) or session
            action_parts.append('started-literature-harvest')
            if queue is not None and not queue_matches_problem:
                action_parts.append('replaced-stale-queue')
        else:
            action_parts.append('reused-existing-queue')

        operation = record_operation(
            store,
            operation_type='literature-search-start',
            started_at=started_at,
            status='completed',
            session_id=session.session_id,
            queue_id=queue.queue_id,
            result_detail='started or resumed literature search for research session',
        )
        return StartLiteratureSearchResponse(
            action='; '.join(action_parts),
            session=session,
            research_problem=problem,
            paper_intake_queue=queue,
            operation=operation,
        )

    @app.get('/research-sessions/{session_id}', response_model=ResearchSessionRecord)
    def get_research_session(session_id: str) -> ResearchSessionRecord:
        record = store.get_research_session(session_id)
        if record is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='research session not found')
        return record

    @app.post('/research-sessions/{session_id}/notes', response_model=ResearchSessionRecord)
    def append_research_session_note(session_id: str, request: ResearchSessionMemoryAppendRequest) -> ResearchSessionRecord:
        if not any((request.working_note, request.decision, request.experiment_idea)):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='at least one memory field must be provided')
        return append_research_session_memory(
            store,
            session_id,
            working_note=request.working_note,
            decision=request.decision,
            experiment_idea=request.experiment_idea,
        )

    @app.post('/research-sessions/latest/memory', response_model=ResearchSessionRecord)
    def append_latest_research_session_note(request: ResearchSessionMemoryAppendRequest) -> ResearchSessionRecord:
        session = store.get_latest_research_session()
        if session is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='no research session has been created yet')
        return append_research_session_note(session.session_id, request)

    @app.get('/research-sessions/latest/context', response_model=ResearchSessionContextResponse)
    def get_latest_research_session_context() -> ResearchSessionContextResponse:
        session = store.get_latest_research_session()
        if session is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='no research session has been created yet')
        return build_research_session_context(session, store)

    @app.get('/research-sessions/{session_id}/context', response_model=ResearchSessionContextResponse)
    def get_research_session_context(session_id: str) -> ResearchSessionContextResponse:
        session = store.get_research_session(session_id)
        if session is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='research session not found')
        return build_research_session_context(session, store)

    @app.get('/research-sessions/latest/source-documents', response_model=list[SourceDocumentRecord])
    def list_latest_session_source_documents() -> list[SourceDocumentRecord]:
        session = store.get_latest_research_session()
        if session is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='no research session has been created yet')
        return list_session_source_documents(session.session_id)

    @app.get('/research-sessions/{session_id}/source-documents', response_model=list[SourceDocumentRecord])
    def list_session_source_documents(session_id: str) -> list[SourceDocumentRecord]:
        session = store.get_research_session(session_id)
        if session is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='research session not found')
        records = [
            record
            for record in store.list_source_documents()
            if record.session_id == session.session_id
        ]
        records.sort(key=lambda record: record.created_at, reverse=True)
        return records

    @app.get('/research-sessions/latest/literature-digest', response_model=LiteratureDigestResponse)
    def get_latest_research_session_literature_digest() -> LiteratureDigestResponse:
        session = store.get_latest_research_session()
        if session is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='no research session has been created yet')
        return build_research_session_literature_digest(session, store)

    @app.get('/research-sessions/{session_id}/literature-digest', response_model=LiteratureDigestResponse)
    def get_research_session_literature_digest(session_id: str) -> LiteratureDigestResponse:
        session = store.get_research_session(session_id)
        if session is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='research session not found')
        return build_research_session_literature_digest(session, store)

    @app.get('/research-sessions/latest/research-problem', response_model=ResearchProblemRecord)
    def get_latest_session_research_problem() -> ResearchProblemRecord:
        session = store.get_latest_research_session()
        if session is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='no research session has been created yet')
        return get_session_research_problem(session.session_id)

    @app.get('/research-sessions/{session_id}/research-problem', response_model=ResearchProblemRecord)
    def get_session_research_problem(session_id: str) -> ResearchProblemRecord:
        session = store.get_research_session(session_id)
        if session is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='research session not found')
        record = store.get_research_problem(session.latest_problem_id or '')
        if record is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='research session has no staged research problem yet')
        return record

    @app.get('/research-sessions/latest/paper-intake-queue', response_model=PaperIntakeQueueRecord)
    def get_latest_session_paper_intake_queue() -> PaperIntakeQueueRecord:
        session = store.get_latest_research_session()
        if session is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='no research session has been created yet')
        return get_session_paper_intake_queue(session.session_id)

    @app.get('/research-sessions/{session_id}/paper-intake-queue', response_model=PaperIntakeQueueRecord)
    def get_session_paper_intake_queue(session_id: str) -> PaperIntakeQueueRecord:
        session = store.get_research_session(session_id)
        if session is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='research session not found')
        record = store.get_paper_intake_queue(session.latest_queue_id or '')
        if record is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='research session has no paper-intake queue yet')
        return record

    @app.get('/research-sessions/latest/source-document', response_model=SourceDocumentRecord)
    def get_latest_session_source_document() -> SourceDocumentRecord:
        session = store.get_latest_research_session()
        if session is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='no research session has been created yet')
        return get_session_source_document(session.session_id)

    @app.get('/research-sessions/{session_id}/source-document', response_model=SourceDocumentRecord)
    def get_session_source_document(session_id: str) -> SourceDocumentRecord:
        session = store.get_research_session(session_id)
        if session is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='research session not found')
        record = store.get_source_document(session.latest_document_id or '')
        if record is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='research session has no source document yet')
        return record

    @app.post('/research-sessions/{session_id}/research-problems/from-session-goal', response_model=ResearchProblemRecord, status_code=status.HTTP_201_CREATED)
    def stage_research_problem_from_session_goal(session_id: str) -> ResearchProblemRecord:
        session = store.get_research_session(session_id)
        if session is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='research session not found')
        request = build_research_problem_request_from_session(session, settings)
        record = build_research_problem_record(request, settings, session_id=session.session_id)
        store.save_research_problem(record)
        touch_research_session(store, session.session_id, latest_problem_id=record.problem_id)
        return record

    @app.post('/research-sessions/{session_id}/skills/research-problem', response_model=ResearchProblemRecord, status_code=status.HTTP_201_CREATED)
    def apply_session_research_problem_skill(session_id: str) -> ResearchProblemRecord:
        return stage_research_problem_from_session_goal(session_id)

    @app.post('/research-sessions/latest/research-problems/from-session-goal', response_model=ResearchProblemRecord, status_code=status.HTTP_201_CREATED)
    def stage_research_problem_from_latest_session_goal() -> ResearchProblemRecord:
        session = store.get_latest_research_session()
        if session is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='no research session has been created yet')
        return stage_research_problem_from_session_goal(session.session_id)

    @app.post('/research-sessions/latest/skills/research-problem', response_model=ResearchProblemRecord, status_code=status.HTTP_201_CREATED)
    def apply_latest_session_research_problem_skill() -> ResearchProblemRecord:
        return stage_research_problem_from_latest_session_goal()

    @app.post('/research-sessions/from-latest-research-problem', response_model=ResearchSessionRecord, status_code=status.HTTP_201_CREATED)
    def create_research_session_from_latest_problem() -> ResearchSessionRecord:
        problem = store.get_latest_research_problem()
        if problem is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='no research problem has been staged yet')
        session = create_research_session_from_problem(problem, settings, build_research_session_record)
        store.save_research_session(session)
        problem = problem.model_copy(update={'session_id': session.session_id, 'updated_at': datetime.now(timezone.utc)})
        store.save_research_problem(problem)
        touch_research_session(store, session.session_id, latest_problem_id=problem.problem_id)
        return store.get_research_session(session.session_id) or session

    @app.post('/research-problems', response_model=ResearchProblemRecord, status_code=status.HTTP_201_CREATED)
    def stage_research_problem(request: ResearchProblemPipelineRequest) -> ResearchProblemRecord:
        record = build_research_problem_record(request, settings)
        store.save_research_problem(record)
        touch_research_session(store, record.session_id, latest_problem_id=record.problem_id)
        return record

    @app.get('/research-problems/latest', response_model=ResearchProblemRecord)
    def get_latest_research_problem() -> ResearchProblemRecord:
        record = store.get_latest_research_problem()
        if record is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='no research problem has been staged yet')
        return record

    @app.post('/paper-pipelines/from-latest-research-problem', response_model=ResearchProblemPipelineResponse, status_code=status.HTTP_201_CREATED)
    async def create_pipeline_from_latest_research_problem() -> ResearchProblemPipelineResponse:
        record = store.get_latest_research_problem()
        if record is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='no research problem has been staged yet')
        request = build_research_problem_request_from_record(record, settings)
        return await create_pipeline_from_research_problem(request)

    @app.post('/research-sessions/{session_id}/paper-intake-queues/from-latest-problem', response_model=PaperIntakeQueueRecord, status_code=status.HTTP_201_CREATED)
    def create_paper_intake_queue_from_session_latest_problem(session_id: str) -> PaperIntakeQueueRecord:
        session = store.get_research_session(session_id)
        if session is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='research session not found')
        problem = store.get_research_problem(session.latest_problem_id or '')
        if problem is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='research session has no staged research problem yet')
        request = PaperIntakeQueueCreateRequest(
            problem_statement=problem.problem_statement,
            max_candidate_papers=min(problem.max_candidate_papers, 25),
            priorities=problem.priorities,
            submitted_by=problem.submitted_by,
        )
        record = create_paper_intake_queue_from_research_problem(request)
        if record.session_id != session.session_id:
            record = record.model_copy(update={'session_id': session.session_id, 'updated_at': datetime.now(timezone.utc)})
            store.save_paper_intake_queue(record)
        touch_research_session(store, session.session_id, latest_queue_id=record.queue_id)
        return record

    @app.post('/research-sessions/{session_id}/skills/literature-harvest', response_model=PaperIntakeQueueRecord, status_code=status.HTTP_201_CREATED)
    def apply_session_literature_harvest_skill(session_id: str) -> PaperIntakeQueueRecord:
        return create_paper_intake_queue_from_session_latest_problem(session_id)

    @app.post('/research-sessions/{session_id}/skills/external-literature-search', response_model=PaperIntakeQueueRecord, status_code=status.HTTP_201_CREATED)
    def apply_session_external_literature_search_skill(session_id: str) -> PaperIntakeQueueRecord:
        session = store.get_research_session(session_id)
        if session is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='research session not found')
        problem = store.get_research_problem(session.latest_problem_id or '')
        if problem is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='research session has no staged research problem yet')
        if not settings.external_literature_enabled:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail='external literature search is disabled')

        started_at = datetime.now(timezone.utc)
        try:
            result = search_external_literature(
                problem_statement=problem.problem_statement,
                priorities=problem.priorities,
                max_candidate_papers=min(problem.max_candidate_papers, 25),
                settings=settings,
            )
        except Exception as exc:
            record_operation(
                store,
                operation_type='external-literature-search',
                started_at=started_at,
                status='failed',
                session_id=session.session_id,
                result_detail='external literature search failed',
                error_detail=str(exc),
            )
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f'external literature search failed: {exc}')

        queue_request = PaperIntakeQueueCreateRequest(
            problem_statement=problem.problem_statement,
            max_candidate_papers=min(problem.max_candidate_papers, 25),
            priorities=problem.priorities,
            submitted_by=problem.submitted_by,
        )
        record = build_paper_intake_queue_record(
            queue_request,
            result.selected_tracks,
            result.selected_queries,
            result.selected_papers,
            result.coverage_summary,
            result.warnings,
            settings,
            session_id=session.session_id,
        )
        store.save_paper_intake_queue(record)
        touch_research_session(store, session.session_id, latest_queue_id=record.queue_id)
        record_operation(
            store,
            operation_type='external-literature-search',
            started_at=started_at,
            status='completed',
            session_id=session.session_id,
            queue_id=record.queue_id,
            result_detail=f'created external literature queue with {len(record.candidates)} candidates',
        )
        return record

    @app.post('/research-sessions/latest/paper-intake-queues/from-latest-problem', response_model=PaperIntakeQueueRecord, status_code=status.HTTP_201_CREATED)
    def create_paper_intake_queue_from_latest_session_problem() -> PaperIntakeQueueRecord:
        session = store.get_latest_research_session()
        if session is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='no research session has been created yet')
        return create_paper_intake_queue_from_session_latest_problem(session.session_id)

    @app.post('/research-sessions/latest/skills/literature-harvest', response_model=PaperIntakeQueueRecord, status_code=status.HTTP_201_CREATED)
    def apply_latest_session_literature_harvest_skill() -> PaperIntakeQueueRecord:
        return create_paper_intake_queue_from_latest_session_problem()

    @app.post('/research-sessions/latest/skills/external-literature-search', response_model=PaperIntakeQueueRecord, status_code=status.HTTP_201_CREATED)
    def apply_latest_session_external_literature_search_skill() -> PaperIntakeQueueRecord:
        session = store.get_latest_research_session()
        if session is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='no research session has been created yet')
        return apply_session_external_literature_search_skill(session.session_id)

    @app.post('/paper-intake-queues/from-research-problem', response_model=PaperIntakeQueueRecord, status_code=status.HTTP_201_CREATED)
    def create_paper_intake_queue_from_research_problem(request: PaperIntakeQueueCreateRequest) -> PaperIntakeQueueRecord:
        started_at = datetime.now(timezone.utc)
        harvester_request = ResearchProblemPipelineRequest(
            problem_statement=request.problem_statement,
            max_candidate_papers=request.max_candidate_papers,
            priorities=request.priorities,
            submitted_by=request.submitted_by,
            wait_for_terminal_state=False,
        )
        try:
            plan_payload = call_problem_harvester_plan(harvester_request, settings)
        except (urllib_error.URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
            record_operation(
                store,
                operation_type='literature-harvest',
                started_at=started_at,
                status='failed',
                session_id=None,
                result_detail='problem harvester failed',
                error_detail=str(exc),
            )
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f'problem harvester unavailable: {exc}')

        selected_track_ids = [
            str(item.get('track_id', '')).strip()
            for item in plan_payload.get('selected_tracks', [])
            if isinstance(item, dict) and str(item.get('track_id', '')).strip()
        ]
        selected_queries = [
            query.strip()
            for item in plan_payload.get('selected_queries', [])
            if isinstance(item, dict)
            for query in item.get('queries', [])
            if isinstance(query, str) and query.strip()
        ]
        selected_papers = [
            ResearchProblemPaperCandidate.model_validate(item)
            for item in plan_payload.get('selected_papers', [])
            if isinstance(item, dict)
        ]
        coverage_summary = plan_payload.get('coverage_summary', {})
        if not isinstance(coverage_summary, dict):
            coverage_summary = {}
        warnings = [
            warning.strip()
            for warning in plan_payload.get('warnings', [])
            if isinstance(warning, str) and warning.strip()
        ]
        record = build_paper_intake_queue_record(
            request,
            selected_track_ids,
            selected_queries,
            selected_papers,
            coverage_summary,
            warnings,
            settings,
        )
        store.save_paper_intake_queue(record)
        touch_research_session(store, record.session_id, latest_queue_id=record.queue_id)
        record_operation(
            store,
            operation_type='literature-harvest',
            started_at=started_at,
            status='completed',
            session_id=record.session_id,
            queue_id=record.queue_id,
            result_detail=f'created paper intake queue with {len(record.candidates)} candidates',
        )
        return record

    @app.get('/paper-intake-queues', response_model=list[PaperIntakeQueueRecord])
    def list_paper_intake_queues() -> list[PaperIntakeQueueRecord]:
        return store.list_paper_intake_queues()

    @app.get('/paper-intake-queues/latest', response_model=PaperIntakeQueueRecord)
    def get_latest_paper_intake_queue() -> PaperIntakeQueueRecord:
        record = store.get_latest_paper_intake_queue()
        if record is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='paper intake queue not found')
        return record

    @app.get('/paper-intake-queues/{queue_id}', response_model=PaperIntakeQueueRecord)
    def get_paper_intake_queue(queue_id: str) -> PaperIntakeQueueRecord:
        record = store.get_paper_intake_queue(queue_id)
        if record is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='paper intake queue not found')
        return record

    @app.post('/paper-intake-queues/{queue_id}/stage-next-intake', response_model=IntakeRecord, status_code=status.HTTP_201_CREATED)
    def stage_next_intake_from_queue(queue_id: str) -> IntakeRecord:
        started_at = datetime.now(timezone.utc)
        queue = store.get_paper_intake_queue(queue_id)
        if queue is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='paper intake queue not found')

        next_candidate = next((candidate for candidate in queue.candidates if candidate.intake_status == 'pending'), None)
        if next_candidate is None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail='paper intake queue is exhausted')

        paper_ref = (
            next(iter(build_source_fetch_candidates(next_candidate.official_page, next_candidate.pdf_url)), None)
            or next_candidate.paper_id
        )
        document_refs: list[str] = []
        intake_notes: list[str] = []
        if paper_ref:
            source_document = None
            source_fetch_candidates = build_source_fetch_candidates(next_candidate.official_page, next_candidate.pdf_url)
            if not source_fetch_candidates:
                source_fetch_candidates = [paper_ref]
            for source_candidate in source_fetch_candidates:
                source_document = ingest_source_document(
                    source_candidate,
                    submitted_by=queue.submitted_by,
                    settings=settings,
                    store=store,
                    session_id=queue.session_id,
                    expected_title=next_candidate.title,
                )
                if source_document.validation_status == 'mismatch':
                    intake_notes.append(
                        'Fetched source did not match expected paper title: '
                        + '; '.join(source_document.validation_notes)
                    )
                if source_document.status == 'fetched' and source_document.validation_status != 'mismatch':
                    break
            assert source_document is not None
            store.save_source_document(source_document)
            record_operation(
                store,
                operation_type='source-document-fetch',
                started_at=started_at,
                status='completed' if source_document.status == 'fetched' and source_document.validation_status != 'mismatch' else 'failed',
                session_id=queue.session_id,
                queue_id=queue.queue_id,
                document_id=source_document.document_id,
                result_detail=(
                    'source document fetched'
                    if source_document.status == 'fetched' and source_document.validation_status != 'mismatch'
                    else 'source document fetch failed or did not match expected paper'
                ),
                error_detail=source_document.fetch_error or '; '.join(source_document.validation_notes),
            )
            if source_document.status == 'fetched' and source_document.validation_status != 'mismatch':
                document_refs.append(source_document.document_id)
            touch_research_session(store, queue.session_id, latest_document_id=source_document.document_id)

        intake_request = build_intake_request_from_problem_candidate(
            queue,
            next_candidate,
            document_refs=document_refs,
            extra_notes=intake_notes,
        )
        intake = stage_intake_from_request(intake_request, settings, registry, store, session_id=queue.session_id)

        updated_candidates: list[PaperIntakeCandidateRecord] = []
        for candidate in queue.candidates:
            if candidate.paper_id == next_candidate.paper_id:
                updated_candidates.append(
                    candidate.model_copy(
                        update={
                            'intake_status': 'staged',
                            'staged_intake_id': intake.intake_id,
                        }
                    )
                )
            else:
                updated_candidates.append(candidate)

        queue_status = 'ready' if any(candidate.intake_status == 'pending' for candidate in updated_candidates) else 'exhausted'
        updated_queue = queue.model_copy(
            update={
                'updated_at': datetime.now(timezone.utc),
                'status': queue_status,
                'candidates': updated_candidates,
            }
        )
        store.save_paper_intake_queue(updated_queue)
        touch_research_session(store, queue.session_id, latest_queue_id=updated_queue.queue_id, latest_intake_id=intake.intake_id)
        record_operation(
            store,
            operation_type='paper-intake',
            started_at=started_at,
            status='completed',
            session_id=queue.session_id,
            queue_id=updated_queue.queue_id,
            intake_id=intake.intake_id,
            result_detail='staged next intake from queue',
        )
        return intake

    @app.post('/research-sessions/{session_id}/skills/paper-intake', response_model=IntakeRecord, status_code=status.HTTP_201_CREATED)
    def apply_session_paper_intake_skill(session_id: str) -> IntakeRecord:
        session = store.get_latest_research_session() if session_id == 'latest' else store.get_research_session(session_id)
        if session is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='research session not found')
        queue_id = session.latest_queue_id or ''
        if not queue_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='research session has no paper intake queue yet')
        return stage_next_intake_from_queue(queue_id)

    @app.post('/research-sessions/latest/paper-intake-queues/stage-next-intake', response_model=IntakeRecord, status_code=status.HTTP_201_CREATED)
    def stage_next_intake_from_latest_session_queue() -> IntakeRecord:
        session = store.get_latest_research_session()
        if session is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='no research session has been created yet')
        queue_id = session.latest_queue_id or ''
        if not queue_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='current research session has no paper intake queue yet')
        return stage_next_intake_from_queue(queue_id)

    @app.post('/research-sessions/{session_id}/paper-intake-queues/stage-next-intake', response_model=IntakeRecord, status_code=status.HTTP_201_CREATED)
    def stage_next_intake_from_session_queue(session_id: str) -> IntakeRecord:
        session = store.get_latest_research_session() if session_id == 'latest' else store.get_research_session(session_id)
        if session is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='research session not found')
        queue_id = session.latest_queue_id or ''
        if not queue_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='research session has no paper intake queue yet')
        return stage_next_intake_from_queue(queue_id)

    @app.post('/research-sessions/latest/skills/paper-intake', response_model=IntakeRecord, status_code=status.HTTP_201_CREATED)
    def apply_latest_session_paper_intake_skill() -> IntakeRecord:
        return stage_next_intake_from_latest_session_queue()

    @app.post(
        '/research-sessions/{session_id}/transitions/promote-paper-to-intake',
        response_model=IntakeRecord,
        status_code=status.HTTP_201_CREATED,
    )
    def transition_promote_paper_to_intake(session_id: str) -> IntakeRecord:
        return apply_session_paper_intake_skill(session_id)

    @app.post(
        '/research-sessions/latest/transitions/promote-paper-to-intake',
        response_model=IntakeRecord,
        status_code=status.HTTP_201_CREATED,
    )
    def transition_promote_latest_paper_to_intake() -> IntakeRecord:
        return apply_latest_session_paper_intake_skill()

    @app.post('/research-sessions/{session_id}/paper-intake-queue/manual-paper', response_model=PaperIntakeQueueRecord, status_code=status.HTTP_201_CREATED)
    def add_manual_paper_to_session_queue(session_id: str, request: ManualPaperCandidateCreateRequest) -> PaperIntakeQueueRecord:
        session = store.get_latest_research_session() if session_id == 'latest' else store.get_research_session(session_id)
        if session is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='research session not found')

        queue = store.get_paper_intake_queue(session.latest_queue_id or '')
        if queue is None:
            problem = store.get_research_problem(session.latest_problem_id or '')
            queue = PaperIntakeQueueRecord(
                queue_id=f'manual-{datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")}',
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
                status='ready',
                problem_statement=(problem.problem_statement if problem is not None else session.goal_statement),
                selected_tracks=['manual'],
                selected_queries=[],
                coverage_summary={'mode': 'manual'},
                warnings=[],
                candidates=[],
                submitted_by=request.submitted_by or session.submitted_by,
                session_id=session.session_id,
            )

        candidate = PaperIntakeCandidateRecord(
            paper_id=(request.official_page or request.pdf_url or request.title).strip(),
            title=request.title.strip(),
            year=request.year,
            venue=request.venue.strip() or 'manual',
            priority='P1',
            tracks=list(dict.fromkeys(['manual', *request.tags])),
            bounded_job_fit=3,
            replication_complexity=3,
            official_page=request.official_page.strip() if request.official_page else None,
            pdf_url=(
                request.pdf_url.strip()
                if request.pdf_url
                else derive_arxiv_pdf_url(request.official_page)
            ),
            why_seed='Manually added by the operator.',
            first_jobs=request.notes[:3],
            tags=list(dict.fromkeys(['manual', *request.tags])),
            match_score=0,
            match_reasons=['manual candidate'],
        )

        updated_queue = queue.model_copy(
            update={
                'updated_at': datetime.now(timezone.utc),
                'status': 'ready',
                'selected_tracks': list(dict.fromkeys([*queue.selected_tracks, 'manual'])),
                'coverage_summary': {**queue.coverage_summary, 'manual': True},
                'candidates': [*queue.candidates, candidate],
            }
        )
        store.save_paper_intake_queue(updated_queue)
        touch_research_session(store, session.session_id, latest_queue_id=updated_queue.queue_id)
        record_operation(
            store,
            operation_type='manual-paper-add',
            started_at=datetime.now(timezone.utc),
            status='completed',
            session_id=session.session_id,
            queue_id=updated_queue.queue_id,
            result_detail=f"added manual paper candidate '{candidate.title}'",
        )
        return updated_queue

    @app.post('/research-sessions/latest/paper-intake-queue/manual-paper', response_model=PaperIntakeQueueRecord, status_code=status.HTTP_201_CREATED)
    def add_manual_paper_to_latest_session_queue(request: ManualPaperCandidateCreateRequest) -> PaperIntakeQueueRecord:
        session = store.get_latest_research_session()
        if session is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='no research session has been created yet')
        return add_manual_paper_to_session_queue(session.session_id, request)
