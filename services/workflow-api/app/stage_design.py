"""Transform an interpretation into a concrete, bounded design draft.

Builds ReplicabilityAssessment records (deterministic or via an assessment
agent), selects a workflow from the registry, derives declared inputs and
unresolved slots from the intake context, constructs a MethodSpec with
execution inputs, blocking reasons, and run readiness, and produces a
DesignDraft ready for human review or direct run creation. Unresolved
inputs are marked with the UNRESOLVED_ prefix so downstream validation
can distinguish them from approved defaults.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import re
from typing import Any
from urllib.parse import urlparse
from urllib import error as urllib_error
from urllib import request as urllib_request
from uuid import uuid4

from services.common.schemas import WorkflowRegistryEntry

from .config import Settings
from .internal_agent_auth import internal_agent_headers
from .registry import WorkflowRegistry
from .schemas import DesignDraftRecord, IntakeRecord, InterpretationRecord, MethodSpecRecord, ReplicabilityAssessmentRecord
from .stage_inference import normalize_unique_strings

LOGGER = logging.getLogger(__name__)
UNRESOLVED_PREFIX = 'UNRESOLVED_'


def looks_like_dataset_uri(value: str) -> bool:
    text = value.strip()
    lowered = text.lower()
    if lowered.startswith(('s3://', 'file://', '/mnt/')):
        return True
    if not lowered.startswith(('http://', 'https://')):
        return False
    return bool(re.search(r'\.(csv|tsv|jsonl|json|parquet|arrow|feather|npy|npz)(?:[?#].*)?$', lowered))


def build_replicability_assessment(
    interpretation: InterpretationRecord,
    registry: WorkflowRegistry,
) -> ReplicabilityAssessmentRecord:
    now = datetime.now(timezone.utc)
    recommended_workflow = None
    approval_tier = None
    for workflow_id in interpretation.candidate_workflow_families:
        workflow = registry.get_workflow(workflow_id)
        if workflow is None:
            continue
        if recommended_workflow is None:
            recommended_workflow = workflow
        if workflow.workflow_id == 'generic-tabular-benchmark' and 'titanic' in interpretation.dataset_hints:
            recommended_workflow = workflow
            break

    unresolved_fields = list(dict.fromkeys(interpretation.unresolved_questions))
    blocking_reasons: list[str] = []
    recommendation = 'needs_review'
    status_value = 'needs_review'
    assessment_notes: list[str] = []

    if interpretation.research_gaps:
        assessment_notes.append(
            'Interpretation surfaced research gaps: ' + '; '.join(interpretation.research_gaps[:2])
        )
    if interpretation.bounded_experiment_ideas:
        assessment_notes.append(
            'Bounded experiment ideas: ' + '; '.join(interpretation.bounded_experiment_ideas[:2])
        )

    if recommended_workflow is not None:
        approval_tier = recommended_workflow.approval_tier
        assessment_notes.append(
            f"Best current approved workflow match is {recommended_workflow.workflow_id}."
        )
        assessment_notes.append(interpretation.literature_state_summary[:240])
        if recommended_workflow.approval_tier != 'tier-2-approved-execution':
            unresolved_fields.append(
                f'Approval tier {recommended_workflow.approval_tier} requires human review before execution.'
            )
            blocking_reasons.append('Approval tier requires explicit review.')
        if unresolved_fields:
            recommendation = 'needs_review'
            status_value = 'needs_review'
            assessment_notes.append('Interpretation still contains unresolved execution-critical fields.')
        else:
            recommendation = 'proceed'
            status_value = 'ready_for_design'
            assessment_notes.append('Interpretation can proceed toward design drafting.')
    # If no workflow matched, return 'reject' instead of 'needs_review' so
    # the pipeline does not silently retry an unmappable interpretation.
    else:
        recommendation = 'reject'
        status_value = 'rejected'
        blocking_reasons.append('No approved workflow family could be mapped from the interpretation.')
        assessment_notes.append('No approved workflow mapping was found in the current registry.')

    return ReplicabilityAssessmentRecord(
        assessment_id=uuid4().hex,
        interpretation_id=interpretation.interpretation_id,
        intake_id=interpretation.intake_id,
        created_at=now,
        updated_at=now,
        status=status_value,
        recommendation=recommendation,
        recommended_workflow_id=recommended_workflow.workflow_id if recommended_workflow is not None else None,
        candidate_workflow_families=interpretation.candidate_workflow_families,
        unresolved_fields=unresolved_fields,
        blocking_reasons=blocking_reasons,
        approval_tier=approval_tier,
        assessment_notes=assessment_notes,
        submitted_by=interpretation.submitted_by,
        session_id=interpretation.session_id,
    )


def validate_assessment_agent_draft(
    draft: dict[str, Any],
    registry: WorkflowRegistry,
) -> dict[str, Any]:
    status_value = draft.get('status')
    recommendation = draft.get('recommendation')
    if not isinstance(status_value, str) or not status_value.strip():
        raise ValueError('assessment agent draft missing valid status')
    if not isinstance(recommendation, str) or not recommendation.strip():
        raise ValueError('assessment agent draft missing valid recommendation')

    recommended_workflow_id = draft.get('recommended_workflow_id')
    if recommended_workflow_id is not None:
        if not isinstance(recommended_workflow_id, str) or not recommended_workflow_id.strip():
            raise ValueError('assessment agent draft has invalid recommended_workflow_id')
        if registry.get_workflow(recommended_workflow_id) is None:
            raise ValueError(f'assessment agent returned unapproved workflow id: {recommended_workflow_id}')
        recommended_workflow_id = recommended_workflow_id.strip()

    normalized = {
        'status': status_value.strip(),
        'recommendation': recommendation.strip(),
        'recommended_workflow_id': recommended_workflow_id,
        'candidate_workflow_families': normalize_unique_strings(list(draft.get('candidate_workflow_families', []))),
        'unresolved_fields': normalize_unique_strings(list(draft.get('unresolved_fields', [])))[:6],
        'blocking_reasons': normalize_unique_strings(list(draft.get('blocking_reasons', []))),
        'approval_tier': draft.get('approval_tier').strip() if isinstance(draft.get('approval_tier'), str) else None,
        'assessment_notes': normalize_unique_strings(list(draft.get('assessment_notes', []))),
    }

    invalid_candidates = [
        workflow_id for workflow_id in normalized['candidate_workflow_families']
        if registry.get_workflow(workflow_id) is None
    ]
    if invalid_candidates:
        raise ValueError(f'assessment agent returned unapproved candidate ids: {", ".join(invalid_candidates)}')

    return normalized


def build_replicability_assessment_from_agent_draft(
    interpretation: InterpretationRecord,
    validated_draft: dict[str, Any],
) -> ReplicabilityAssessmentRecord:
    now = datetime.now(timezone.utc)
    return ReplicabilityAssessmentRecord(
        assessment_id=uuid4().hex,
        interpretation_id=interpretation.interpretation_id,
        intake_id=interpretation.intake_id,
        created_at=now,
        updated_at=now,
        status=validated_draft['status'],
        recommendation=validated_draft['recommendation'],
        recommended_workflow_id=validated_draft['recommended_workflow_id'],
        candidate_workflow_families=validated_draft['candidate_workflow_families'],
        unresolved_fields=validated_draft['unresolved_fields'],
        blocking_reasons=validated_draft['blocking_reasons'],
        approval_tier=validated_draft['approval_tier'],
        assessment_notes=validated_draft['assessment_notes'],
        submitted_by=interpretation.submitted_by,
        session_id=interpretation.session_id,
    )


def call_assessment_agent(
    interpretation: InterpretationRecord,
    settings: Settings,
    registry: WorkflowRegistry,
) -> ReplicabilityAssessmentRecord | None:
    if not settings.assessment_agent_enabled:
        return None

    available_workflows = []
    for workflow_id in interpretation.candidate_workflow_families:
        workflow = registry.get_workflow(workflow_id)
        if workflow is None:
            continue
        available_workflows.append(
            {
                'workflow_id': workflow.workflow_id,
                'approval_tier': workflow.approval_tier,
            }
        )

    payload = {
        'request_id': interpretation.interpretation_id,
        'interpretation': {
            'interpretation_id': interpretation.interpretation_id,
            'intake_id': interpretation.intake_id,
            'source_type': interpretation.source_type,
            'normalized_summary': interpretation.normalized_summary,
            'extracted_method_summary': interpretation.extracted_method_summary,
            'literature_state_summary': interpretation.literature_state_summary,
            'candidate_workflow_families': interpretation.candidate_workflow_families,
            'dataset_hints': interpretation.dataset_hints,
            'evaluation_targets': interpretation.evaluation_targets,
            'extracted_claims': interpretation.extracted_claims,
            'research_gaps': interpretation.research_gaps,
            'bounded_experiment_ideas': interpretation.bounded_experiment_ideas,
            'unresolved_questions': interpretation.unresolved_questions,
            'submitted_by': interpretation.submitted_by,
        },
        'available_workflows': available_workflows,
    }
    request_obj = urllib_request.Request(
        settings.assessment_agent_url,
        data=json.dumps(payload).encode('utf-8'),
        headers=internal_agent_headers(settings),
        method='POST',
    )

    try:
        with urllib_request.urlopen(request_obj, timeout=settings.assessment_agent_timeout_seconds) as response:
            body = json.loads(response.read().decode('utf-8'))
        draft = body.get('draft')
        if not isinstance(draft, dict):
            raise ValueError('assessment agent response missing draft object')
        validated_draft = validate_assessment_agent_draft(draft, registry)
        return build_replicability_assessment_from_agent_draft(interpretation, validated_draft)
    except (urllib_error.URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        LOGGER.warning('assessment-agent fallback for interpretation %s: %s', interpretation.interpretation_id, exc)
        return None


def choose_workflow_for_intake(intake: IntakeRecord, registry: WorkflowRegistry) -> WorkflowRegistryEntry | None:
    lowered = ' '.join([intake.raw_request, intake.normalized_summary, *intake.notes, *intake.source_refs]).lower()
    candidate_ids = list(intake.workflow_family_candidates)

    if 'titanic' in lowered and 'generic-tabular-benchmark' in candidate_ids:
        candidate_ids = ['generic-tabular-benchmark', *[item for item in candidate_ids if item != 'generic-tabular-benchmark']]

    # Iterate candidate IDs in priority order; 'generic-tabular-benchmark'
    # takes precedence when Titanic-like keywords are present.
    for workflow_id in candidate_ids:
        workflow = registry.get_workflow(workflow_id)
        if workflow is not None:
            return workflow
    return None


def derive_design_from_intake(intake: IntakeRecord, workflow: WorkflowRegistryEntry) -> tuple[dict[str, Any], list[str], list[str]]:
    lowered = ' '.join([intake.raw_request, intake.normalized_summary, *intake.notes, *intake.source_refs]).lower()
    declared_inputs: dict[str, Any] = {}
    design_notes: list[str] = []

    explicit_dataset_uri = next(
        (
            note.split(':', 1)[1].strip()
            for note in intake.notes
            if isinstance(note, str) and note.startswith('Dataset URI:')
        ),
        None,
    )
    concrete_dataset_match = re.search(r'(s3://\S+|file://\S+|/mnt/\S+)', intake.raw_request)
    if explicit_dataset_uri is None and concrete_dataset_match:
        explicit_dataset_uri = concrete_dataset_match.group(1).rstrip('.,);')
    explicit_dataset_uri = explicit_dataset_uri or next(
        (
            ref
            for ref in intake.source_refs
            if isinstance(ref, str)
            and ref.strip()
            and looks_like_dataset_uri(ref)
        ),
        None,
    )
    explicit_dataset_name = next(
        (
            note.split(':', 1)[1].strip()
            for note in intake.notes
            if isinstance(note, str) and note.startswith('Selected dataset:')
        ),
        None,
    )
    if not explicit_dataset_name and explicit_dataset_uri:
        parsed = urlparse(explicit_dataset_uri)
        explicit_dataset_name = parsed.path.rsplit('/', 1)[-1] or parsed.netloc or 'dataset'

    if workflow.workflow_id == 'generic-tabular-benchmark':
        if explicit_dataset_uri:
            declared_inputs = {
                'dataset_name': explicit_dataset_name or ('titanic' if 'titanic' in lowered else 'dataset'),
                'train_uri': explicit_dataset_uri,
                'test_uri': explicit_dataset_uri.replace('/train.csv', '/test.csv') if explicit_dataset_uri.endswith('/train.csv') else explicit_dataset_uri,
                'validation_strategy': 'holdout',
                'validation_split': '0.2',
                'target_column': 'Survived' if 'titanic' in lowered else 'label',
            }
            design_notes.append('Resolved benchmark inputs from the explicitly attached dataset.')
            design_notes.append('Declared holdout validation strategy with a 0.2 validation split to help guard against overfitting.')
        elif 'titanic' in lowered:
            declared_inputs = {
                'dataset_name': 'titanic',
                'train_uri': 's3://datasets/titanic/train.csv',
                'test_uri': 's3://datasets/titanic/test.csv',
                'validation_strategy': 'holdout',
                'validation_split': '0.2',
                'target_column': 'Survived',
            }
            design_notes.append('Resolved approved Titanic benchmark inputs deterministically.')
            design_notes.append('Declared holdout validation strategy with a 0.2 validation split to help guard against overfitting.')
        else:
            declared_inputs = {
                'dataset_name': 'UNRESOLVED_DATASET_NAME',
                'train_uri': 'UNRESOLVED_TRAIN_URI',
                'test_uri': 'UNRESOLVED_TEST_URI',
                'validation_strategy': 'UNRESOLVED_VALIDATION_STRATEGY',
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
    elif workflow.workflow_id == 'gpu-experiment':
        declared_inputs = {
            'dataset_uri': 'UNRESOLVED_DATASET_URI',
            'model_family': 'UNRESOLVED_MODEL_FAMILY',
            'training_notes': intake.normalized_summary[:500],
        }
        design_notes.append('GPU experiment targets require explicit dataset and model-family inputs.')
    else:
        paper_id = intake.source_refs[0] if intake.source_refs else 'UNRESOLVED_PAPER_ID'
        declared_inputs = {
            'paper_id': paper_id,
            'repository_url': 'UNRESOLVED_REPOSITORY_URL',
            'dataset_uri': 'UNRESOLVED_DATASET_URI',
            'evaluation_target': 'UNRESOLVED_EVALUATION_TARGET',
        }
        design_notes.append('Replication targets require explicit repository and evaluation inputs.')

    unresolved_inputs = [
        name for name, value in declared_inputs.items() if isinstance(value, str) and value.startswith(UNRESOLVED_PREFIX)
    ]
    return declared_inputs, unresolved_inputs, design_notes


def build_design_draft(
    intake: IntakeRecord,
    workflow: WorkflowRegistryEntry,
    submitted_by: str,
    interpretation: InterpretationRecord | None = None,
    source_assessment_id: str | None = None,
) -> DesignDraftRecord:
    now = datetime.now(timezone.utc)
    design_id = uuid4().hex
    declared_inputs, unresolved_inputs, design_notes = derive_design_from_intake(intake, workflow)
    bounded_idea_notes = [note for note in intake.notes if note.startswith('Bounded experiment ideas: ')]
    literature_state_notes = [note for note in intake.notes if note.startswith('Literature state: ')]
    design_notes.extend(bounded_idea_notes[:1])
    design_notes.extend(literature_state_notes[:1])
    candidate_models = workflow.allowed_models[:2]
    objective = f'Derived from intake: {intake.normalized_summary}'[:500]
    method_spec = build_design_method_spec(
        workflow_id=workflow.workflow_id,
        objective=objective,
        declared_inputs=declared_inputs,
        unresolved_inputs=unresolved_inputs,
        candidate_models=candidate_models,
        resource_profile=workflow.resource_profile.profile_name,
        interpretation=interpretation,
    )
    declared_inputs = dict(method_spec.execution_inputs)
    unresolved_inputs = compute_method_spec_unresolved_inputs(method_spec)
    status_value = 'ready_for_run'
    if unresolved_inputs or method_spec.run_readiness != 'ready':
        status_value = 'needs_review'
    if workflow.approval_tier != 'tier-2-approved-execution':
        status_value = 'needs_review'
        design_notes.append(f'Approval tier {workflow.approval_tier} requires operator review before run creation.')
    if method_spec.blocking_reasons:
        design_notes.append('Method spec blockers: ' + '; '.join(method_spec.blocking_reasons[:3]))

    return DesignDraftRecord(
        design_id=design_id,
        intake_id=intake.intake_id,
        source_assessment_id=source_assessment_id,
        created_at=now,
        updated_at=now,
        status=status_value,
        workflow_id=workflow.workflow_id,
        workflow_family=workflow.workflow_family,
        objective=objective,
        declared_inputs=declared_inputs,
        unresolved_inputs=unresolved_inputs,
        candidate_models=workflow.allowed_models[:2],
        resource_profile=workflow.resource_profile.profile_name,
        expected_artifacts=workflow.expected_artifacts.model_dump(mode='json'),
        approval_tier=workflow.approval_tier,
        design_notes=design_notes,
        method_spec=method_spec,
        submitted_by=submitted_by,
        session_id=intake.session_id,
    )


def validate_design_agent_draft(
    draft: dict[str, Any],
    workflow: WorkflowRegistryEntry,
) -> dict[str, Any]:
    required_string_fields = ('workflow_id', 'workflow_family', 'objective', 'resource_profile', 'approval_tier')
    for field_name in required_string_fields:
        value = draft.get(field_name)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f'design agent draft missing valid {field_name}')

    if draft['workflow_id'].strip() != workflow.workflow_id:
        raise ValueError('design agent draft changed workflow_id')
    if draft['workflow_family'].strip() != workflow.workflow_family:
        raise ValueError('design agent draft changed workflow_family')
    if draft['resource_profile'].strip() != workflow.resource_profile.profile_name:
        raise ValueError('design agent draft changed resource_profile')
    if draft['approval_tier'].strip() != workflow.approval_tier:
        raise ValueError('design agent draft changed approval_tier')

    candidate_models = normalize_unique_strings(list(draft.get('candidate_models', [])))
    invalid_models = [model for model in candidate_models if model not in workflow.allowed_models]
    if invalid_models:
        raise ValueError(f'design agent returned disallowed models: {", ".join(invalid_models)}')

    declared_inputs = draft.get('declared_inputs', {})
    expected_artifacts = draft.get('expected_artifacts', {})
    if not isinstance(declared_inputs, dict):
        raise ValueError('design agent draft missing valid declared_inputs')
    if not isinstance(expected_artifacts, dict):
        raise ValueError('design agent draft missing valid expected_artifacts')

    return {
        'workflow_id': draft['workflow_id'].strip(),
        'workflow_family': draft['workflow_family'].strip(),
        'objective': ' '.join(draft['objective'].split())[:500],
        'declared_inputs': declared_inputs,
        'unresolved_inputs': normalize_unique_strings(list(draft.get('unresolved_inputs', []))),
        'candidate_models': candidate_models,
        'resource_profile': draft['resource_profile'].strip(),
        'expected_artifacts': expected_artifacts,
        'approval_tier': draft['approval_tier'].strip(),
        'design_notes': normalize_unique_strings(list(draft.get('design_notes', []))),
    }


def build_design_draft_from_agent_draft(
    intake: IntakeRecord,
    workflow: WorkflowRegistryEntry,
    submitted_by: str,
    validated_draft: dict[str, Any],
    source_assessment_id: str | None = None,
) -> DesignDraftRecord:
    now = datetime.now(timezone.utc)
    method_spec = build_design_method_spec(
        workflow_id=workflow.workflow_id,
        objective=validated_draft['objective'],
        declared_inputs=validated_draft['declared_inputs'],
        unresolved_inputs=list(validated_draft['unresolved_inputs']),
        candidate_models=list(validated_draft['candidate_models']),
        resource_profile=validated_draft['resource_profile'],
        interpretation=None,
    )
    return DesignDraftRecord(
        design_id=uuid4().hex,
        intake_id=intake.intake_id,
        source_assessment_id=source_assessment_id,
        created_at=now,
        updated_at=now,
        status='ready_for_run' if not compute_method_spec_unresolved_inputs(method_spec) and method_spec.run_readiness == 'ready' and workflow.approval_tier == 'tier-2-approved-execution' else 'needs_review',
        workflow_id=validated_draft['workflow_id'],
        workflow_family=validated_draft['workflow_family'],
        objective=validated_draft['objective'],
        declared_inputs=method_spec.execution_inputs,
        unresolved_inputs=compute_method_spec_unresolved_inputs(method_spec),
        candidate_models=validated_draft['candidate_models'],
        resource_profile=validated_draft['resource_profile'],
        expected_artifacts=validated_draft['expected_artifacts'],
        approval_tier=validated_draft['approval_tier'],
        design_notes=validated_draft['design_notes'],
        method_spec=method_spec,
        submitted_by=submitted_by,
        session_id=intake.session_id,
    )


def compute_method_spec_unresolved_inputs(method_spec: MethodSpecRecord) -> list[str]:
    unresolved: list[str] = []
    for name, value in method_spec.execution_inputs.items():
        if isinstance(value, str) and value.startswith(UNRESOLVED_PREFIX):
            unresolved.append(name)
    if method_spec.dataset_uri is None and 'dataset_uri' in method_spec.execution_inputs:
        unresolved.append('dataset_uri')
    return list(dict.fromkeys(unresolved))


def build_design_method_spec(
    *,
    workflow_id: str,
    objective: str,
    declared_inputs: dict[str, Any],
    unresolved_inputs: list[str],
    candidate_models: list[str],
    resource_profile: str,
    interpretation: InterpretationRecord | None,
) -> MethodSpecRecord:
    execution_inputs = dict(declared_inputs)
    # Interpretation-backed defaults fill unresolvable slots so the method
    # spec benefits from previously inferred knowledge without silently
    # overwriting concrete declared inputs.
    if interpretation is not None and interpretation.method_spec is not None:
        for key, value in interpretation.method_spec.execution_inputs.items():
            if key not in execution_inputs or (
                isinstance(execution_inputs.get(key), str) and str(execution_inputs.get(key)).startswith(UNRESOLVED_PREFIX)
            ):
                execution_inputs[key] = value
    dataset_hints = list(interpretation.recommended_datasets) if interpretation is not None else []
    metrics = list(interpretation.recommended_metrics) if interpretation is not None else []
    baseline_models = list(interpretation.recommended_baselines) if interpretation is not None else []
    required_python_packages = list(interpretation.recommended_python_packages) if interpretation is not None else []
    mutation_axes = list(interpretation.mutation_axes) if interpretation is not None else []
    task_type = interpretation.recommended_method_family if interpretation is not None else workflow_id
    loss_or_distance = None
    if interpretation is not None and interpretation.technique_knowledge.losses_or_distances:
        loss_or_distance = interpretation.technique_knowledge.losses_or_distances[0]
    split_strategy = None
    if 'validation_strategy' in execution_inputs and str(execution_inputs.get('validation_strategy', '')).strip():
        split_strategy = str(execution_inputs['validation_strategy']).strip()
    elif interpretation is not None and interpretation.technique_knowledge.split_strategies:
        split_strategy = interpretation.technique_knowledge.split_strategies[0]
        execution_inputs.setdefault('validation_strategy', split_strategy)
    if 'validation_split' not in execution_inputs and workflow_id in {'generic-tabular-benchmark', 'literature-to-experiment', 'gpu-experiment'}:
        execution_inputs['validation_split'] = '0.2'
    if 'validation_strategy' not in execution_inputs and workflow_id in {'generic-tabular-benchmark', 'literature-to-experiment'}:
        execution_inputs['validation_strategy'] = split_strategy or 'holdout'
        split_strategy = execution_inputs['validation_strategy']

    recomputed_unresolved = [
        name for name, value in execution_inputs.items() if isinstance(value, str) and value.startswith(UNRESOLVED_PREFIX)
    ]
    blocking_reasons = [
        f'{name} is unresolved' for name in list(dict.fromkeys([*unresolved_inputs, *recomputed_unresolved]))
    ]
    if workflow_id == 'gpu-experiment':
        if not str(execution_inputs.get('dataset_uri', '')).strip():
            blocking_reasons.append('dataset_uri is unresolved for gpu-experiment')
        if not str(execution_inputs.get('model_family', '')).strip():
            blocking_reasons.append('model_family is unresolved for gpu-experiment')
        if not str(execution_inputs.get('training_notes', '')).strip():
            execution_inputs['training_notes'] = objective[:500]
    if workflow_id == 'literature-to-experiment' and not str(execution_inputs.get('source_notes', '')).strip():
        execution_inputs['source_notes'] = objective[:500]
    run_readiness = 'ready' if not blocking_reasons else 'needs_review'
    dataset_uri = str(execution_inputs.get('dataset_uri', '')).strip() or None
    return MethodSpecRecord(
        objective=objective[:500],
        workflow_id=workflow_id,
        task_type=task_type,
        candidate_models=list(candidate_models),
        baseline_models=list(baseline_models),
        dataset_hints=dataset_hints,
        dataset_uri=dataset_uri,
        split_strategy=split_strategy,
        metrics=metrics,
        loss_or_distance=loss_or_distance,
        required_python_packages=required_python_packages,
        resource_profile=resource_profile,
        execution_inputs=execution_inputs,
        mutation_axes=mutation_axes,
        run_readiness=run_readiness,
        blocking_reasons=blocking_reasons,
    )


def refresh_design_method_spec(
    design: DesignDraftRecord,
    *,
    interpretation: InterpretationRecord | None = None,
) -> DesignDraftRecord:
    method_spec = build_design_method_spec(
        workflow_id=design.workflow_id,
        objective=design.objective,
        declared_inputs=design.declared_inputs,
        unresolved_inputs=list(design.unresolved_inputs),
        candidate_models=list(design.candidate_models),
        resource_profile=design.resource_profile,
        interpretation=interpretation,
    )
    unresolved_inputs = compute_method_spec_unresolved_inputs(method_spec)
    status_value = 'ready_for_run'
    if unresolved_inputs or method_spec.run_readiness != 'ready' or design.approval_tier != 'tier-2-approved-execution':
        status_value = 'needs_review'
    return design.model_copy(
        update={
            'declared_inputs': method_spec.execution_inputs,
            'unresolved_inputs': unresolved_inputs,
            'status': status_value,
            'method_spec': method_spec,
            'updated_at': datetime.now(timezone.utc),
        }
    )


def call_design_agent(
    intake: IntakeRecord,
    workflow: WorkflowRegistryEntry,
    submitted_by: str,
    settings: Settings,
    source_assessment_id: str | None = None,
) -> DesignDraftRecord | None:
    if not settings.design_agent_enabled:
        return None

    payload = {
        'request_id': intake.intake_id,
        'intake': {
            'intake_id': intake.intake_id,
            'source_type': intake.source_type,
            'source_refs': intake.source_refs,
            'document_refs': intake.document_refs,
            'raw_request': intake.raw_request,
            'normalized_summary': intake.normalized_summary,
            'workflow_family_candidates': intake.workflow_family_candidates,
            'notes': intake.notes,
            'submitted_by': intake.submitted_by,
        },
        'workflow': {
            'workflow_id': workflow.workflow_id,
            'workflow_family': workflow.workflow_family,
            'allowed_models': workflow.allowed_models,
            'expected_artifacts': workflow.expected_artifacts.model_dump(mode='json'),
            'resource_profile_name': workflow.resource_profile.profile_name,
            'approval_tier': workflow.approval_tier,
        },
    }
    request_obj = urllib_request.Request(
        settings.design_agent_url,
        data=json.dumps(payload).encode('utf-8'),
        headers=internal_agent_headers(settings),
        method='POST',
    )

    try:
        with urllib_request.urlopen(request_obj, timeout=settings.design_agent_timeout_seconds) as response:
            body = json.loads(response.read().decode('utf-8'))
        draft = body.get('draft')
        if not isinstance(draft, dict):
            raise ValueError('design agent response missing draft object')
        validated_draft = validate_design_agent_draft(draft, workflow)
        return build_design_draft_from_agent_draft(
            intake,
            workflow,
            submitted_by=submitted_by,
            validated_draft=validated_draft,
            source_assessment_id=source_assessment_id,
        )
    except (urllib_error.URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        LOGGER.warning('design-agent fallback for intake %s workflow %s: %s', intake.intake_id, workflow.workflow_id, exc)
        return None
