"""FastAPI surface for the design stage-agent.

Exposes POST /draft-design plus /healthz. Drafts come from deterministic
scaffold logic in build_design_draft (the future model call would replace it),
so the endpoint always returns the same warnings block. Inputs that cannot be
resolved deterministically are emitted as UNRESOLVED_ sentinels rather than
guessed, keeping operator-review obligations explicit. Caller is workflow-api.
"""

from __future__ import annotations

import os

from fastapi import Depends, FastAPI

from .internal_auth import require_internal_token
from .models import DesignDraft, DesignRequest, DesignResponse, HealthResponse, ModelBackendMetadata

UNRESOLVED_PREFIX = 'UNRESOLVED_'

# Backend metadata is read from env once at import time and echoed on every
# response; defaults point at the shared mlx Qwen endpoint a live implementation
# would call. Kept out of the request path so it is immutable per process.
MODEL_BACKEND = ModelBackendMetadata(
    provider=os.getenv('GLASSLAB_DESIGN_AGENT_PROVIDER_API', 'openai-compatible').strip() or 'openai-compatible',
    base_url=os.getenv('GLASSLAB_DESIGN_AGENT_PROVIDER_BASE_URL', 'http://192.168.1.21:52415').strip(),
    model=os.getenv('GLASSLAB_DESIGN_AGENT_MODEL', 'mlx-community/Qwen3-Coder-Next-4bit').strip()
    or 'mlx-community/Qwen3-Coder-Next-4bit',
    timeout_seconds=float(os.getenv('GLASSLAB_DESIGN_AGENT_TIMEOUT_SECONDS', '120').strip() or '120'),
)


def derive_design_from_intake(request: DesignRequest) -> tuple[dict[str, object], list[str], list[str]]:
    intake = request.intake
    workflow = request.workflow
    lowered = ' '.join([intake.raw_request, intake.normalized_summary, *intake.notes, *intake.source_refs]).lower()
    declared_inputs: dict[str, object] = {}
    design_notes: list[str] = []

    if workflow.workflow_id == 'generic-tabular-benchmark':
        # Only the Titanic benchmark has a deterministic dataset binding; any
        # other tabular request leaves all four inputs explicitly unresolved.
        if 'titanic' in lowered:
            declared_inputs = {
                'dataset_name': 'titanic',
                'train_uri': 's3://datasets/titanic/train.csv',
                'test_uri': 's3://datasets/titanic/test.csv',
                'target_column': 'Survived',
            }
            design_notes.append('Resolved approved Titanic benchmark inputs deterministically.')
        else:
            declared_inputs = {
                'dataset_name': 'UNRESOLVED_DATASET_NAME',
                'train_uri': 'UNRESOLVED_TRAIN_URI',
                'test_uri': 'UNRESOLVED_TEST_URI',
                'target_column': 'UNRESOLVED_TARGET_COLUMN',
            }
            design_notes.append('Dataset-specific benchmark inputs still require operator review.')
    elif workflow.workflow_id == 'literature-to-experiment':
        paper_id = intake.source_refs[0] if intake.source_refs else 'UNRESOLVED_PAPER_ID'
        source_notes = '\n'.join(intake.notes).strip() or intake.normalized_summary
        declared_inputs = {
            'paper_id': paper_id,
            'source_notes': source_notes,
            'dataset_uri': 'UNRESOLVED_DATASET_URI',
        }
        design_notes.append('Source paper metadata was normalized from the intake record.')
        if intake.document_refs:
            design_notes.append(f'Stored source documents are available: {", ".join(intake.document_refs[:2])}.')
        design_notes.append('Dataset selection remains unresolved for literature-derived experiments.')
    else:
        # Fallback for any workflow without a deterministic mapping: every
        # execution-critical input stays an explicit sentinel for operator
        # review instead of being silently defaulted.
        paper_id = intake.source_refs[0] if intake.source_refs else 'UNRESOLVED_PAPER_ID'
        declared_inputs = {
            'paper_id': paper_id,
            'repository_url': 'UNRESOLVED_REPOSITORY_URL',
            'dataset_uri': 'UNRESOLVED_DATASET_URI',
            'evaluation_target': 'UNRESOLVED_EVALUATION_TARGET',
        }
        design_notes.append('Replication targets require explicit repository and evaluation inputs.')

    # Sentinel scan: any declared input still carrying the UNRESOLVED_ prefix is
    # reported by field name, so the caller sees exactly what needs operator
    # input without parsing values.
    unresolved_inputs = [
        name for name, value in declared_inputs.items() if isinstance(value, str) and value.startswith(UNRESOLVED_PREFIX)
    ]
    return declared_inputs, unresolved_inputs, design_notes


def build_design_draft(request: DesignRequest) -> DesignDraft:
    intake = request.intake
    workflow = request.workflow
    declared_inputs, unresolved_inputs, design_notes = derive_design_from_intake(request)
    # These prefixes are the interpretation agent's note format; only notes
    # carrying them are propagated into the draft.
    literature_state_notes = [note for note in intake.notes if note.startswith('Literature state: ')]
    bounded_idea_notes = [note for note in intake.notes if note.startswith('Bounded experiment ideas: ')]
    design_notes.extend(literature_state_notes[:1])
    design_notes.extend(bounded_idea_notes[:1])
    if bounded_idea_notes:
        design_notes.append('Design draft is grounded in bounded experiment ideas derived upstream from interpretation.')
    if workflow.approval_tier != 'tier-2-approved-execution':
        design_notes.append(f'Approval tier {workflow.approval_tier} requires operator review before run creation.')
    return DesignDraft(
        workflow_id=workflow.workflow_id,
        workflow_family=workflow.workflow_family,
        # Cap the objective so a bloated intake summary never overflows
        # downstream context windows or logs.
        objective=f'Derived from intake: {intake.normalized_summary}'[:500],
        declared_inputs=declared_inputs,
        unresolved_inputs=unresolved_inputs,
        # The draft proposes at most two candidates even when the registry
        # allows more, keeping the bounded-design contract small.
        candidate_models=workflow.allowed_models[:2],
        resource_profile=workflow.resource_profile_name,
        expected_artifacts=workflow.expected_artifacts,
        approval_tier=workflow.approval_tier,
        design_notes=design_notes,
    )


app = FastAPI(title='glasslab-design-agent', version='0.1.0')


@app.get('/healthz', response_model=HealthResponse)
def healthz() -> HealthResponse:
    return HealthResponse(status='ok', model_backend=MODEL_BACKEND.model_dump())


@app.post('/draft-design', response_model=DesignResponse, dependencies=[Depends(require_internal_token)])
def draft_design(request: DesignRequest) -> DesignResponse:
    return DesignResponse(
        request_id=request.request_id,
        draft=build_design_draft(request),
        model_backend=MODEL_BACKEND,
        warnings=[
            'current implementation is deterministic scaffold logic; live model integration is not enabled yet',
        ],
    )
