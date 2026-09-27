"""FastAPI surface for the assessment stage-agent.

Exposes POST /assess-interpretation plus /healthz. Drafts are produced by
deterministic scaffold logic in build_assessment_draft (the future model call
would replace it), so the endpoint always returns the same warnings block.
Caller is workflow-api.
"""

from __future__ import annotations

import os

from fastapi import Depends, FastAPI

from .internal_auth import require_internal_token
from .models import (
    AssessmentDraft,
    AssessmentRequest,
    AssessmentResponse,
    HealthResponse,
    ModelBackendMetadata,
)

# Backend metadata is read from env once at import time and echoed on every
# response; defaults point at the shared mlx Qwen endpoint a live implementation
# would call. Kept out of the request path so it is immutable per process.
MODEL_BACKEND = ModelBackendMetadata(
    provider=os.getenv('GLASSLAB_ASSESSMENT_AGENT_PROVIDER_API', 'openai-compatible').strip()
    or 'openai-compatible',
    base_url=os.getenv('GLASSLAB_ASSESSMENT_AGENT_PROVIDER_BASE_URL', 'http://192.168.1.21:52415').strip(),
    model=os.getenv('GLASSLAB_ASSESSMENT_AGENT_MODEL', 'mlx-community/Qwen3-Coder-Next-4bit').strip()
    or 'mlx-community/Qwen3-Coder-Next-4bit',
    timeout_seconds=float(os.getenv('GLASSLAB_ASSESSMENT_AGENT_TIMEOUT_SECONDS', '120').strip() or '120'),
)


def build_assessment_draft(request: AssessmentRequest) -> AssessmentDraft:
    interpretation = request.interpretation
    # Callers supply only registry-approved workflows, so a candidate that is
    # missing here was filtered out upstream and is simply skipped.
    workflows = {workflow.workflow_id: workflow for workflow in request.available_workflows}

    recommended_workflow = None
    for workflow_id in interpretation.candidate_workflow_families:
        workflow = workflows.get(workflow_id)
        if workflow is None:
            continue
        if recommended_workflow is None:
            recommended_workflow = workflow
        # Titanic maps deterministically to the generic tabular benchmark and
        # outranks any earlier family candidate.
        if workflow.workflow_id == 'generic-tabular-benchmark' and 'titanic' in interpretation.dataset_hints:
            recommended_workflow = workflow
            break

    unresolved_fields = list(interpretation.unresolved_questions)
    blocking_reasons: list[str] = []
    assessment_notes: list[str] = []
    approval_tier = recommended_workflow.approval_tier if recommended_workflow is not None else None

    if interpretation.research_gaps:
        assessment_notes.append(
            'Interpretation surfaced research gaps: ' + '; '.join(interpretation.research_gaps[:2])
        )
    if interpretation.bounded_experiment_ideas:
        assessment_notes.append(
            'Bounded experiment ideas: ' + '; '.join(interpretation.bounded_experiment_ideas[:2])
        )

    if recommended_workflow is None:
        # No approved workflow matched: a hard reject, not needs_review, because
        # there is nothing yet for a reviewer to approve.
        return AssessmentDraft(
            recommendation='reject',
            recommended_workflow_id=None,
            candidate_workflow_families=interpretation.candidate_workflow_families,
            unresolved_fields=unresolved_fields,
            blocking_reasons=['No approved workflow family could be mapped from the interpretation.'],
            approval_tier=None,
            assessment_notes=['No approved workflow mapping was found in the current registry view.'],
            status='rejected',
        )

    assessment_notes.append(f'Best current approved workflow match is {recommended_workflow.workflow_id}.')
    assessment_notes.append(interpretation.literature_state_summary[:240])
    if recommended_workflow.approval_tier != 'tier-2-approved-execution':
        unresolved_fields.append(
            f'Approval tier {recommended_workflow.approval_tier} requires human review before execution.'
        )
        blocking_reasons.append('Approval tier requires explicit review.')

    if unresolved_fields:
        assessment_notes.append('Interpretation still contains unresolved execution-critical fields.')
        return AssessmentDraft(
            recommendation='needs_review',
            recommended_workflow_id=recommended_workflow.workflow_id,
            candidate_workflow_families=interpretation.candidate_workflow_families,
            unresolved_fields=unresolved_fields,
            blocking_reasons=blocking_reasons,
            approval_tier=approval_tier,
            assessment_notes=assessment_notes,
            status='needs_review',
        )

    assessment_notes.append('Interpretation can proceed toward design drafting.')
    return AssessmentDraft(
        recommendation='proceed',
        recommended_workflow_id=recommended_workflow.workflow_id,
        candidate_workflow_families=interpretation.candidate_workflow_families,
        unresolved_fields=[],
        blocking_reasons=blocking_reasons,
        approval_tier=approval_tier,
        assessment_notes=assessment_notes,
        status='ready_for_design',
    )


app = FastAPI(title='glasslab-assessment-agent', version='0.1.0')


@app.get('/healthz', response_model=HealthResponse)
def healthz() -> HealthResponse:
    return HealthResponse(status='ok', model_backend=MODEL_BACKEND.model_dump())


@app.post('/assess-interpretation', response_model=AssessmentResponse, dependencies=[Depends(require_internal_token)])
def assess_interpretation(request: AssessmentRequest) -> AssessmentResponse:
    return AssessmentResponse(
        request_id=request.request_id,
        draft=build_assessment_draft(request),
        model_backend=MODEL_BACKEND,
        warnings=[
            'current implementation is deterministic scaffold logic; live model integration is not enabled yet',
        ],
    )
