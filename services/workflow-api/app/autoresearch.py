"""Automated iterative experiment campaigns: seed, mutate, launch, score, decide.

Builds methodology drafts from approved designs, generates speculative variants
through mutation axes and technique-catalog records, maps drafts to bounded run
requests, summarizes scored results, and produces keep/discard/escalate decisions.
The campaign is scoped by a max-iteration ceiling and an evaluator contract;
every decision is idempotent (re-running a decision reuses the existing record
unless new evidence makes a stale escalation actionable).
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request
from uuid import uuid4

from fastapi import HTTPException, status

from services.common.schemas import WorkflowRegistryEntry

from .config import Settings
from .job_submission import JobSubmitter
from .persistence import RunStore
from .registry import WorkflowRegistry
from .run_artifacts import artifact_run_dir, resolve_run_status
from .schemas import AutoresearchCampaignCreateRequest, AutoresearchCampaignRecord, AutoresearchCampaignSummaryResponse, AutoresearchDecisionRecord, AutoresearchIterationRecord, AutoresearchSuggestedMutation, DesignDraftRecord, EvaluatorContract, InterpretationRecord, MethodologyDraftRecord, RunCreateRequest, RunRecord
from .session_helpers import get_required_research_session, touch_research_session
from .technique_catalog import match_catalog_records_for_intake


def _dedupe(values: list[str]) -> list[str]:
    # dict insertion order preserves discovery order while dropping duplicates.
    return list(dict.fromkeys([value.strip() for value in values if value and value.strip()]))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _find_latest_design_for_campaign(
    store: RunStore,
    *,
    session_id: str,
    source_design_id: str | None,
) -> DesignDraftRecord:
    if source_design_id:
        design = store.get_design_draft(source_design_id)
        if design is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='design draft not found')
        return design
    session = get_required_research_session(store, session_id)
    design = store.get_design_draft(session.latest_design_id or '')
    if design is None:
        design = store.get_latest_design_draft()
    if design is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='research session has no design draft yet')
    return design


def _methodology_family_for_workflow(workflow_id: str) -> str:
    if workflow_id == 'generic-tabular-benchmark':
        return 'tabular-methodology-validation'
    if workflow_id == 'gpu-experiment':
        return 'gpu-methodology-validation'
    return 'bounded-methodology-validation'


def _extract_metric_hints(design: DesignDraftRecord) -> list[str]:
    metrics: list[str] = []
    joined_notes = ' '.join(design.design_notes).lower()
    for metric in ['accuracy', 'f1', 'roc_auc', 'precision', 'recall', 'rmse']:
        if metric in joined_notes:
            metrics.append(metric)
    if not metrics:
        metrics.append('accuracy')
    return _dedupe(metrics)


def _metric_direction(metric_name: str) -> str:
    lowered = metric_name.lower()
    if lowered in {'loss', 'rmse', 'mae', 'mse', 'error_rate', 'latency', 'peak_vram_gb'} or lowered.endswith('_loss'):
        return 'minimize'
    return 'maximize'


def _default_evaluator_contract_for_design(design: DesignDraftRecord) -> EvaluatorContract | None:
    method_spec = design.method_spec
    if method_spec is not None:
        if method_spec.evaluator_contract is not None:
            return method_spec.evaluator_contract
        metrics = list(method_spec.metrics)
    else:
        metrics = []
    if not metrics:
        metrics = _extract_metric_hints(design)
    metrics = _dedupe(metrics)
    if not metrics:
        return None
    primary_metric = metrics[0]
    return EvaluatorContract(
        evaluator_type=f'{design.workflow_id}-method-spec-v1',
        primary_metric={
            'name': primary_metric,
            'direction': _metric_direction(primary_metric),
            'minimum_effect': 0.0,
        },
    )


def _extract_dataset_hints(design: DesignDraftRecord) -> list[str]:
    candidates = [
        str(design.declared_inputs.get('dataset_name', '')).strip(),
        str(design.declared_inputs.get('dataset_uri', '')).strip(),
    ]
    return _dedupe(candidates)


def _default_validation_inputs(declared_inputs: dict[str, Any]) -> dict[str, Any]:
    updated = dict(declared_inputs)
    updated.setdefault('validation_strategy', 'holdout')
    updated.setdefault('validation_split', '0.2')
    return updated


def _extract_risks(design: DesignDraftRecord) -> list[str]:
    risks = [note for note in design.design_notes if 'risk' in note.lower() or 'unresolved' in note.lower()]
    if design.unresolved_inputs:
        risks.append('Unresolved design inputs remain.')
    if not risks:
        risks.append('Keep methodology within approved workflow bounds.')
    return _dedupe(risks)


def _seed_models_for_design(design: DesignDraftRecord, workflow: WorkflowRegistryEntry) -> list[str]:
    return _dedupe(design.candidate_models or workflow.allowed_models[:2])


def build_seed_methodology_draft(
    campaign: AutoresearchCampaignRecord,
    design: DesignDraftRecord,
    workflow: WorkflowRegistryEntry,
) -> MethodologyDraftRecord:
    now = _now()
    models = _seed_models_for_design(design, workflow)
    objective = campaign.objective or design.objective
    method_spec = getattr(design, 'method_spec', None)
    return MethodologyDraftRecord(
        methodology_draft_id=uuid4().hex,
        campaign_id=campaign.campaign_id,
        session_id=campaign.session_id,
        source_intake_id=design.intake_id,
        source_design_id=design.design_id,
        parent_methodology_draft_id=None,
        created_at=now,
        updated_at=now,
        objective=objective,
        hypothesis='A bounded validation run can reproduce the design intent on the approved template.',
        method_family=_methodology_family_for_workflow(workflow.workflow_id),
        datasets=_extract_dataset_hints(design),
        architectures=models,
        baselines=models[:1],
        metrics=_extract_metric_hints(design),
        risks=_extract_risks(design),
        bounded_experimentability='approved-template-fit',
        status='seed',
        workflow_id=workflow.workflow_id,
        workflow_family=workflow.workflow_family,
        declared_inputs=_default_validation_inputs(
            dict(method_spec.execution_inputs) if method_spec is not None else design.declared_inputs
        ),
        candidate_models=models,
        resource_profile=design.resource_profile,
        approval_tier=design.approval_tier,
        method_spec=method_spec.model_copy(
            update={
                'candidate_models': models,
                'baseline_models': models[:1],
                'resource_profile': design.resource_profile,
                'execution_inputs': _default_validation_inputs(
                    dict(method_spec.execution_inputs)
                ),
            }
        ) if method_spec is not None else None,
        mutation_diff={},
        notes=['seed draft from approved design'],
    )


def _build_variant_specs(seed: MethodologyDraftRecord, workflow: WorkflowRegistryEntry) -> list[dict[str, Any]]:
    models = _dedupe(seed.candidate_models or workflow.allowed_models)
    if not models:
        models = workflow.allowed_models[:1]
    specs: list[dict[str, Any]] = []
    if models:
        specs.append(
            {
                'candidate_models': [models[0]],
                'architectures': [models[0]],
                'baselines': [models[0]],
                'metrics': list(seed.metrics),
                'hypothesis': f'{models[0]} provides a stable bounded baseline on the approved dataset split.',
                'mutation_diff': {'model_family': {'from': models, 'to': [models[0]]}},
                'declared_inputs': dict(seed.declared_inputs),
                'notes': ['single-model baseline variant'],
            }
        )
    if len(models) >= 2:
        specs.append(
            {
                'candidate_models': [models[1]],
                'architectures': [models[1]],
                'baselines': [models[0]],
                'metrics': list(seed.metrics),
                'hypothesis': f'{models[1]} may outperform the baseline under the same approved template.',
                'mutation_diff': {'model_family': {'from': [models[0]], 'to': [models[1]]}},
                'declared_inputs': dict(seed.declared_inputs),
                'notes': ['alternative model variant'],
            }
        )
        specs.append(
            {
                'candidate_models': models[:2],
                'architectures': models[:2],
                'baselines': [models[0]],
                'metrics': list(seed.metrics) + ['accuracy'],
                'hypothesis': 'A side-by-side bounded comparison of the top approved models will clarify the better default.',
                'mutation_diff': {
                    'baseline_inclusion': {'enabled': True},
                    'model_family': {'from': [models[0]], 'to': models[:2]},
                },
                'declared_inputs': dict(seed.declared_inputs),
                'notes': ['pairwise comparison variant'],
            }
        )
    if seed.workflow_id == 'generic-tabular-benchmark':
        split_variant_inputs = dict(seed.declared_inputs)
        split_variant_inputs['validation_strategy'] = 'stratified_holdout'
        split_variant_inputs['validation_split'] = '0.25'
        specs.append(
            {
                'candidate_models': [models[0]],
                'architectures': [models[0]],
                'baselines': [models[0]],
                'metrics': list(seed.metrics),
                'hypothesis': 'A stricter stratified holdout split may reveal overfitting that is hidden under the default split.',
                'mutation_diff': {
                    'validation_strategy': {'from': seed.declared_inputs.get('validation_strategy', 'holdout'), 'to': 'stratified_holdout'},
                    'validation_split': {'from': seed.declared_inputs.get('validation_split', '0.2'), 'to': '0.25'},
                },
                'declared_inputs': split_variant_inputs,
                'notes': ['validation-strategy comparison variant'],
            }
        )
    return specs[:4]


def _build_catalog_variant_specs(
    store: RunStore,
    seed: MethodologyDraftRecord,
) -> list[dict[str, Any]]:
    if not seed.source_intake_id:
        return []
    intake = store.get_intake(seed.source_intake_id)
    if intake is None:
        return []
    records = match_catalog_records_for_intake(intake, store)
    specs: list[dict[str, Any]] = []
    default_baseline = seed.baselines[:1] or seed.candidate_models[:1] or seed.architectures[:1]
    for record in records[:3]:
        candidate_models = _dedupe(record.specific_algorithms or ([record.algorithm_family] if record.algorithm_family else []))
        if not candidate_models:
            continue
        declared_inputs = dict(seed.declared_inputs)
        if record.validation_strategies:
            declared_inputs['validation_strategy'] = record.validation_strategies[0]
            if record.validation_strategies[0] == 'stratified_holdout':
                declared_inputs.setdefault('validation_split', '0.25')
        specs.append(
            {
                'candidate_models': candidate_models[:2],
                'architectures': candidate_models[:2],
                'baselines': default_baseline,
                'metrics': _dedupe(record.primary_metrics or seed.metrics),
                'hypothesis': f"{record.name} should improve the bounded method search using the imported technique knowledge.",
                'mutation_diff': {
                    'technique_card': {'name': record.name, 'technique_id': record.technique_id},
                    'model_family': {'from': seed.candidate_models, 'to': candidate_models[:2]},
                },
                'declared_inputs': declared_inputs,
                'notes': [
                    f'technique-catalog variant from {record.name}',
                    *([f"packages: {', '.join(record.python_packages)}"] if record.python_packages else []),
                ],
                'required_python_packages': list(record.python_packages),
                'loss_or_distance': record.loss_functions[0] if record.loss_functions else None,
                'resource_profile': record.resource_profile or seed.resource_profile,
            }
        )
    return specs


def draft_initial_methodologies(
    store: RunStore,
    campaign: AutoresearchCampaignRecord,
    seed: MethodologyDraftRecord,
    workflow: WorkflowRegistryEntry,
) -> list[MethodologyDraftRecord]:
    drafts: list[MethodologyDraftRecord] = []
    technique_specs = _build_catalog_variant_specs(store, seed)
    baseline_specs = _build_variant_specs(seed, workflow)
    # Technique-catalog variants precede baseline structural variants so
    # catalog knowledge takes priority over generic model-family splits.
    ordered_specs = []
    seen_signatures: set[tuple[str, ...]] = set()
    for spec in [*technique_specs, *baseline_specs]:
        signature = tuple(spec.get('candidate_models', []))
        if signature in seen_signatures:
            continue
        seen_signatures.add(signature)
        ordered_specs.append(spec)
    for spec in ordered_specs[:4]:
        now = _now()
        drafts.append(
            MethodologyDraftRecord(
                methodology_draft_id=uuid4().hex,
                campaign_id=campaign.campaign_id,
                session_id=campaign.session_id,
                source_intake_id=seed.source_intake_id,
                source_design_id=seed.source_design_id,
                parent_methodology_draft_id=seed.methodology_draft_id,
                created_at=now,
                updated_at=now,
                objective=seed.objective,
                hypothesis=spec['hypothesis'],
                method_family=seed.method_family,
                datasets=list(seed.datasets),
                architectures=spec['architectures'],
                baselines=spec['baselines'],
                metrics=_dedupe(spec['metrics']),
                risks=list(seed.risks),
                bounded_experimentability='approved-template-fit',
                status='ready_for_execution',
                workflow_id=seed.workflow_id,
                workflow_family=seed.workflow_family,
                declared_inputs=dict(spec.get('declared_inputs', seed.declared_inputs)),
                candidate_models=spec['candidate_models'],
                resource_profile=spec.get('resource_profile', seed.resource_profile),
                approval_tier=seed.approval_tier,
                method_spec=(
                    seed.method_spec.model_copy(
                        update={
                            'candidate_models': spec['candidate_models'],
                            'baseline_models': spec['baselines'],
                            'metrics': _dedupe(spec['metrics']),
                            'loss_or_distance': spec.get('loss_or_distance', seed.method_spec.loss_or_distance),
                            'required_python_packages': _dedupe(spec.get('required_python_packages', seed.method_spec.required_python_packages)),
                            'resource_profile': spec.get('resource_profile', seed.method_spec.resource_profile),
                            'execution_inputs': dict(spec.get('declared_inputs', seed.declared_inputs)),
                            'mutation_axes': list(seed.method_spec.mutation_axes),
                        }
                    )
                    if seed.method_spec is not None
                    else None
                ),
                mutation_diff=spec['mutation_diff'],
                notes=spec['notes'],
            )
        )
    return drafts


def build_autoresearch_campaign(
    request: AutoresearchCampaignCreateRequest,
    *,
    session_id: str,
    source_design_id: str,
    objective: str,
) -> AutoresearchCampaignRecord:
    now = _now()
    return AutoresearchCampaignRecord(
        campaign_id=uuid4().hex,
        session_id=session_id,
        created_at=now,
        updated_at=now,
        status='created',
        objective=objective,
        source_design_id=source_design_id,
        seed_methodology_draft_ids=[],
        current_best_methodology_draft_id=None,
        latest_iteration_id=None,
        latest_decision_id=None,
        max_iterations=request.max_iterations,
        evaluation_policy=request.evaluation_policy,
        mutation_policy=request.mutation_policy,
        evaluator_contract=request.evaluator_contract,
        budget_contract=request.budget_contract,
        notes=list(request.notes),
    )


def methodology_to_run_request(
    draft: MethodologyDraftRecord,
    workflow: WorkflowRegistryEntry,
) -> RunCreateRequest:
    method_spec = draft.method_spec
    if method_spec is not None and method_spec.run_readiness != 'ready':
        detail = 'methodology draft is not ready for execution'
        if method_spec.blocking_reasons:
            detail += ': ' + '; '.join(method_spec.blocking_reasons[:2])
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)
    inputs = dict(method_spec.execution_inputs) if method_spec is not None else dict(draft.declared_inputs)
    if method_spec is not None:
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
    return RunCreateRequest(
        workflow_id=draft.workflow_id,
        objective=draft.objective,
        inputs=inputs,
        models=resolve_requested_models_for_workflow(
            (method_spec.candidate_models if method_spec is not None and method_spec.candidate_models else draft.candidate_models)
            or draft.architectures
            or draft.baselines,
            workflow,
        ),
        resource_profile=draft.resource_profile,
        run_priority='autonomous',
        submitted_by='glasslab-autoresearch',
    )


def resolve_requested_models_for_workflow(
    requested_models: list[str],
    workflow: WorkflowRegistryEntry,
) -> list[str]:
    allowed = list(workflow.allowed_models or [])
    requested = [str(model).strip() for model in requested_models if str(model).strip()]
    compatible = [model for model in requested if model in allowed]
    if compatible:
        return compatible
    if allowed:
        return allowed[:1]
    return requested[:1]


def get_required_campaign(store: RunStore, campaign_id: str) -> AutoresearchCampaignRecord:
    campaign = store.get_autoresearch_campaign(campaign_id)
    if campaign is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='autoresearch campaign not found')
    return campaign


def get_campaign_methodology_drafts(store: RunStore, campaign_id: str) -> list[MethodologyDraftRecord]:
    return store.list_methodology_drafts(campaign_id)


def get_campaign_iterations(store: RunStore, campaign_id: str) -> list[AutoresearchIterationRecord]:
    return store.list_autoresearch_iterations(campaign_id)


def get_campaign_decisions(store: RunStore, campaign_id: str) -> list[AutoresearchDecisionRecord]:
    return store.list_autoresearch_decisions(campaign_id)


def get_next_launchable_methodology_draft(
    store: RunStore,
    campaign: AutoresearchCampaignRecord,
) -> MethodologyDraftRecord:
    drafts = get_next_launchable_methodology_drafts(store, campaign, limit=1)
    return drafts[0]


def get_next_launchable_methodology_drafts(
    store: RunStore,
    campaign: AutoresearchCampaignRecord,
    *,
    limit: int,
) -> list[MethodologyDraftRecord]:
    iterations = store.list_autoresearch_iterations(campaign.campaign_id)
    launched_ids = {record.child_methodology_draft_id for record in iterations}
    drafts = [
        draft
        for draft in store.list_methodology_drafts(campaign.campaign_id)
        if draft.status == 'ready_for_execution' and draft.methodology_draft_id not in launched_ids
    ]
    drafts.sort(key=lambda record: record.created_at)
    if not drafts:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail='no pending methodology drafts remain')
    remaining = campaign.max_iterations - len(iterations)
    if remaining <= 0:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail='campaign has reached max_iterations')
    return drafts[: max(1, min(limit, remaining))]


def _draft_signature(draft: MethodologyDraftRecord) -> tuple[str, ...]:
    method_spec = draft.method_spec
    return (
        draft.workflow_id,
        draft.resource_profile,
        '|'.join(sorted(draft.candidate_models)),
        '|'.join(sorted(draft.baselines)),
        '|'.join(sorted(draft.metrics)),
        str(draft.declared_inputs.get('validation_strategy', '')),
        str(draft.declared_inputs.get('validation_split', '')),
        str(method_spec.loss_or_distance if method_spec is not None and method_spec.loss_or_distance else ''),
    )


def build_mutated_methodology_draft(
    campaign: AutoresearchCampaignRecord,
    best_draft: MethodologyDraftRecord,
    mutation: AutoresearchSuggestedMutation,
    *,
    workflow: WorkflowRegistryEntry,
) -> MethodologyDraftRecord | None:
    method_spec = best_draft.method_spec
    if method_spec is None:
        return None

    candidate_models = list(best_draft.candidate_models)
    baselines = list(best_draft.baselines)
    metrics = list(best_draft.metrics)
    declared_inputs = dict(best_draft.declared_inputs)
    resource_profile = best_draft.resource_profile
    loss_or_distance = method_spec.loss_or_distance
    task_type = method_spec.task_type
    mutation_updates = dict(mutation.suggested_updates)
    axis = mutation.mutation_axis

    if axis == 'resource_profile':
        suggested_profile = str(mutation_updates.get('resource_profile') or '').strip()
        if suggested_profile and suggested_profile != resource_profile:
            resource_profile = suggested_profile
        else:
            return None
    elif axis == 'validation_strategy':
        changed = False
        for key in ('validation_strategy', 'validation_split'):
            value = mutation_updates.get(key)
            if value is not None and declared_inputs.get(key) != value:
                declared_inputs[key] = value
                changed = True
        if not changed:
            return None
    elif axis == 'loss_or_distance':
        suggested_loss = str(mutation_updates.get('loss_or_distance') or '').strip()
        if not suggested_loss:
            return None
        if suggested_loss == (loss_or_distance or ''):
            suggested_loss = f'{suggested_loss}-variant'
        loss_or_distance = suggested_loss
    elif axis == 'metrics':
        suggested_metrics = _dedupe([str(item) for item in mutation_updates.get('metrics', [])])
        if not suggested_metrics or suggested_metrics == _dedupe(metrics):
            return None
        metrics = suggested_metrics
    elif axis == 'candidate_models':
        suggested_models = _dedupe([str(item) for item in mutation_updates.get('candidate_models', [])])
        if not suggested_models or suggested_models == _dedupe(candidate_models):
            return None
        candidate_models = suggested_models
    elif axis == 'baseline_models':
        suggested_baselines = _dedupe([str(item) for item in mutation_updates.get('baseline_models', [])])
        expanded_candidates = _dedupe(candidate_models + suggested_baselines)
        if expanded_candidates == _dedupe(candidate_models):
            return None
        baselines = suggested_baselines or baselines
        candidate_models = expanded_candidates
    elif axis == 'task_type':
        suggested_task_type = str(mutation_updates.get('task_type') or '').strip()
        suggested_model_family = str(mutation_updates.get('model_family') or '').strip()
        changed = False
        if suggested_task_type and suggested_task_type != (task_type or ''):
            task_type = suggested_task_type
            changed = True
        if suggested_model_family and declared_inputs.get('model_family') != suggested_model_family:
            declared_inputs['model_family'] = suggested_model_family
            changed = True
        if not changed:
            return None
    elif axis == 'evaluation_target':
        suggested_target = str(mutation_updates.get('evaluation_target') or '').strip()
        if not suggested_target or declared_inputs.get('evaluation_target') == suggested_target:
            return None
        declared_inputs['evaluation_target'] = suggested_target
    elif axis == 'runtime_validation':
        if declared_inputs.get('runtime_validation') == 'package-complete':
            return None
        declared_inputs['runtime_validation'] = 'package-complete'
    else:
        return None

    now = _now()
    next_method_spec = method_spec.model_copy(
        update={
            'candidate_models': candidate_models,
            'baseline_models': baselines,
            'metrics': metrics,
            'loss_or_distance': loss_or_distance,
            'resource_profile': resource_profile,
            'task_type': task_type,
            'execution_inputs': declared_inputs,
            'mutation_axes': _dedupe(list(method_spec.mutation_axes) + [axis]),
        }
    )
    return MethodologyDraftRecord(
        methodology_draft_id=uuid4().hex,
        campaign_id=campaign.campaign_id,
        session_id=campaign.session_id,
        source_intake_id=best_draft.source_intake_id,
        source_design_id=best_draft.source_design_id,
        parent_methodology_draft_id=best_draft.methodology_draft_id,
        created_at=now,
        updated_at=now,
        objective=best_draft.objective,
        hypothesis=mutation.summary,
        method_family=best_draft.method_family,
        datasets=list(best_draft.datasets),
        architectures=list(candidate_models),
        baselines=list(baselines),
        metrics=list(metrics),
        risks=list(best_draft.risks),
        bounded_experimentability=best_draft.bounded_experimentability,
        status='ready_for_execution',
        workflow_id=best_draft.workflow_id,
        workflow_family=best_draft.workflow_family,
        declared_inputs=declared_inputs,
        candidate_models=candidate_models,
        resource_profile=resource_profile,
        approval_tier=best_draft.approval_tier,
        method_spec=next_method_spec,
        mutation_diff={
            'auto_follow_on': {
                'source_component': mutation.source_component,
                'mutation_axis': mutation.mutation_axis,
                'suggested_updates': mutation_updates,
            }
        },
        notes=[*best_draft.notes, f'auto-follow-on mutation: {mutation.summary}'],
    )


def ensure_follow_on_methodology_drafts(
    store: RunStore,
    campaign: AutoresearchCampaignRecord,
    *,
    registry: WorkflowRegistry,
    settings: Settings,
    submitter: JobSubmitter,
    limit: int,
) -> list[MethodologyDraftRecord]:
    remaining = campaign.max_iterations - len(store.list_autoresearch_iterations(campaign.campaign_id))
    if remaining <= 0:
        return []
    summary = summarize_campaign(store, campaign, settings=settings, submitter=submitter)
    best_draft = summary.best_methodology_draft
    if best_draft is None:
        return []
    workflow = registry.get_workflow(best_draft.workflow_id)
    if workflow is None or workflow.execution_status != 'ready':
        return []
    existing_signatures = {_draft_signature(draft) for draft in store.list_methodology_drafts(campaign.campaign_id)}
    created: list[MethodologyDraftRecord] = []
    for mutation in summary.proposed_next_mutations:
        draft = build_mutated_methodology_draft(campaign, best_draft, mutation, workflow=workflow)
        if draft is None:
            continue
        signature = _draft_signature(draft)
        if signature in existing_signatures:
            continue
        store.save_methodology_draft(draft)
        created.append(draft)
        existing_signatures.add(signature)
        if len(created) >= min(limit, remaining):
            break
    if created:
        updated_campaign = campaign.model_copy(update={'updated_at': _now(), 'status': 'drafted'})
        store.save_autoresearch_campaign(updated_campaign)
        touch_research_session(
            store,
            campaign.session_id,
            latest_methodology_draft_id=created[-1].methodology_draft_id,
            decision_log=[f'auto-follow-on methodology drafted: {created[-1].methodology_draft_id}'],
        )
    return created


def _load_metrics_payload(settings: Settings, run_id: str) -> dict[str, Any]:
    path = artifact_run_dir(settings, run_id) / 'metrics.json'
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text())
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def resolve_evaluator_contract(
    methodology: MethodologyDraftRecord | None,
    campaign: AutoresearchCampaignRecord | None = None,
) -> EvaluatorContract | None:
    method_spec = methodology.method_spec if methodology is not None else None
    if method_spec is not None and method_spec.evaluator_contract is not None:
        return method_spec.evaluator_contract
    if campaign is not None:
        return campaign.evaluator_contract
    return None


def _extract_primary_metric(
    metrics: dict[str, Any],
    evaluator_contract: EvaluatorContract | None = None,
) -> tuple[str | None, float | None, str]:
    if evaluator_contract is not None and evaluator_contract.primary_metric is not None:
        name = evaluator_contract.primary_metric.name
        value = metrics.get(name)
        if isinstance(value, (int, float)):
            return name, float(value), 'evaluator_contract'
        metric_name = metrics.get('metric_name')
        best_metric = metrics.get('best_metric')
        if metric_name == name and isinstance(best_metric, (int, float)):
            return name, float(best_metric), 'evaluator_contract'
        return name, None, 'evaluator_contract'
    metric_name = metrics.get('metric_name')
    best_metric = metrics.get('best_metric')
    if isinstance(metric_name, str) and isinstance(best_metric, (int, float)):
        return metric_name, float(best_metric), 'legacy_payload'
    preferred = ['accuracy', 'f1', 'roc_auc', 'precision', 'recall']
    for key in preferred:
        value = metrics.get(key)
        if isinstance(value, (int, float)):
            return key, float(value), 'legacy_inferred'
    numeric_items = [(key, float(value)) for key, value in metrics.items() if isinstance(value, (int, float))]
    if not numeric_items:
        return (None, None, 'unavailable')
    numeric_items.sort(key=lambda item: item[0])
    metric_name, metric_value = numeric_items[0]
    return metric_name, metric_value, 'legacy_inferred'


def _evaluate_guardrails(
    metrics: dict[str, Any],
    evaluator_contract: EvaluatorContract | None,
) -> list[dict[str, Any]]:
    if evaluator_contract is None:
        return []
    results: list[dict[str, Any]] = []
    for guardrail in evaluator_contract.guardrails:
        value = metrics.get(guardrail.name)
        result: dict[str, Any] = {
            'metric_name': guardrail.name,
            'direction': guardrail.direction,
            'required': guardrail.required,
            'minimum': guardrail.minimum,
            'maximum': guardrail.maximum,
            'value': value if isinstance(value, (int, float, bool)) else None,
            'passed': True,
            'detail': 'guardrail satisfied',
        }
        if not isinstance(value, (int, float)):
            result['passed'] = not guardrail.required
            result['detail'] = 'required guardrail metric missing' if guardrail.required else 'optional guardrail metric missing'
            results.append(result)
            continue
        numeric_value = float(value)
        result['value'] = numeric_value
        if guardrail.minimum is not None and numeric_value < guardrail.minimum:
            result['passed'] = False
            result['detail'] = f'value is below minimum {guardrail.minimum}'
        if guardrail.maximum is not None and numeric_value > guardrail.maximum:
            result['passed'] = False
            result['detail'] = f'value is above maximum {guardrail.maximum}'
        results.append(result)
    return results


def summarize_iteration_run(
    record: RunRecord,
    *,
    settings: Settings,
    submitter: JobSubmitter,
    evaluator_contract: EvaluatorContract | None = None,
) -> dict[str, Any]:
    resolved = resolve_run_status(record, settings, submitter)
    metrics = _load_metrics_payload(settings, record.run_id)
    metric_name, metric_value, metric_source = _extract_primary_metric(metrics, evaluator_contract)
    direction = (
        evaluator_contract.primary_metric.direction
        if evaluator_contract is not None and evaluator_contract.primary_metric is not None
        else None
    )
    summary: dict[str, Any] = {
        'run_status': resolved.status,
        'run_detail': resolved.detail,
        'primary_metric_name': metric_name,
        'primary_metric_value': metric_value,
        'primary_metric_source': metric_source,
        'primary_metric_direction': direction,
    }
    if evaluator_contract is not None:
        summary['evaluator_type'] = evaluator_contract.evaluator_type
        summary['guardrails'] = _evaluate_guardrails(metrics, evaluator_contract)
    if metrics:
        summary['metrics'] = metrics
    return summary


def build_iteration_comparison(
    child_summary: dict[str, Any],
    parent_summary: dict[str, Any] | None,
    evaluator_contract: EvaluatorContract | None = None,
) -> dict[str, Any]:
    primary_metric = evaluator_contract.primary_metric if evaluator_contract is not None else None
    direction = primary_metric.direction if primary_metric is not None else child_summary.get('primary_metric_direction')
    minimum_effect = primary_metric.minimum_effect if primary_metric is not None else 0.0
    comparison: dict[str, Any] = {
        'baseline_available': parent_summary is not None,
        'metric_name': child_summary.get('primary_metric_name'),
        'direction': direction,
        'minimum_effect': minimum_effect,
        'comparable': False,
    }
    if parent_summary is None:
        comparison['detail'] = 'no prior scored baseline is available'
        return comparison
    if child_summary.get('primary_metric_name') != parent_summary.get('primary_metric_name'):
        comparison['detail'] = 'metric mismatch prevents automatic comparison'
        return comparison
    child_value = child_summary.get('primary_metric_value')
    parent_value = parent_summary.get('primary_metric_value')
    if not isinstance(child_value, (int, float)) or not isinstance(parent_value, (int, float)):
        comparison['detail'] = 'numeric metrics unavailable for automatic comparison'
        return comparison
    raw_delta = float(child_value) - float(parent_value)
    normalized_delta = raw_delta if direction != 'minimize' else -raw_delta
    comparison['baseline_value'] = parent_value
    comparison['candidate_value'] = child_value
    comparison['delta'] = round(raw_delta, 6)
    comparison['normalized_delta'] = round(normalized_delta, 6)
    comparison['comparable'] = True
    comparison['detail'] = 'numeric comparison computed'
    return comparison


def build_decision(
    iteration: AutoresearchIterationRecord,
    child_summary: dict[str, Any],
    comparison_summary: dict[str, Any],
    evaluator_contract: EvaluatorContract | None = None,
) -> tuple[str, str]:
    run_status = str(child_summary.get('run_status', 'unknown'))
    if run_status in {'failed', 'rejected'}:
        return ('discard', f'Run ended in terminal failure state: {run_status}.')
    if evaluator_contract is None or evaluator_contract.primary_metric is None:
        return ('escalate_for_review', 'Automatic scientific decisions require an approved evaluator contract with a primary metric.')
    failed_guardrails = [
        guardrail
        for guardrail in child_summary.get('guardrails', [])
        if isinstance(guardrail, dict) and guardrail.get('passed') is False
    ]
    if failed_guardrails:
        names = ', '.join(str(item.get('metric_name')) for item in failed_guardrails[:3])
        return ('escalate_for_review', f'Candidate violates evaluator guardrail(s): {names}.')
    delta = comparison_summary.get('normalized_delta')
    metric_name = comparison_summary.get('metric_name')
    direction = comparison_summary.get('direction') or evaluator_contract.primary_metric.direction
    minimum_effect = float(comparison_summary.get('minimum_effect') or evaluator_contract.primary_metric.minimum_effect)
    if isinstance(delta, (int, float)) and metric_name:
        if delta >= minimum_effect:
            return ('keep', f'Candidate improved {metric_name} by {delta:.4f} over the baseline under the {direction} contract.')
        if delta <= -minimum_effect:
            return ('discard', f'Candidate regressed {metric_name} by {abs(delta):.4f} relative to the baseline under the {direction} contract.')
        return ('escalate_for_review', f'Candidate changed {metric_name} by {delta:.4f}; minimum effect is {minimum_effect:.4f}.')
    if run_status == 'succeeded' and isinstance(child_summary.get('primary_metric_value'), (int, float)):
        return ('keep', 'Candidate produced the first successful bounded run under the approved evaluator contract.')
    return ('escalate_for_review', 'Insufficient evidence for an automatic keep/discard decision.')


def build_model_comparison_rows(
    drafts: list[MethodologyDraftRecord],
    iterations: list[AutoresearchIterationRecord],
    decisions: list[AutoresearchDecisionRecord],
) -> list[dict[str, Any]]:
    draft_by_id = {draft.methodology_draft_id: draft for draft in drafts}
    decision_by_iteration = {record.iteration_id: record for record in decisions}
    rows: list[dict[str, Any]] = []
    for iteration in iterations:
        draft = draft_by_id.get(iteration.child_methodology_draft_id)
        if draft is None:
            continue
        models = draft.candidate_models or draft.architectures or draft.baselines
        metric_name = iteration.score_summary.get('primary_metric_name')
        metric_value = iteration.score_summary.get('primary_metric_value')
        metric_direction = iteration.score_summary.get('primary_metric_direction')
        metrics_payload = iteration.score_summary.get('metrics', {})
        best_model = metrics_payload.get('best_model') if isinstance(metrics_payload, dict) else None
        technique_components = metrics_payload.get('technique_components') if isinstance(metrics_payload, dict) else None
        readiness_components = metrics_payload.get('readiness_components') if isinstance(metrics_payload, dict) else None
        decision = decision_by_iteration.get(iteration.iteration_id)
        rows.append(
            {
                'iteration_id': iteration.iteration_id,
                'methodology_draft_id': draft.methodology_draft_id,
                'candidate_models': list(models),
                'best_model': best_model,
                'primary_metric_name': metric_name,
                'primary_metric_value': metric_value,
                'primary_metric_direction': metric_direction,
                'technique_components': technique_components if isinstance(technique_components, dict) else {},
                'readiness_components': readiness_components if isinstance(readiness_components, dict) else {},
                'decision': decision.decision_type if decision is not None else iteration.decision,
                'comparison_delta': iteration.comparison_summary.get('delta'),
                'run_id': iteration.run_id,
                'resource_profile': draft.resource_profile,
            }
        )
    rows.sort(
        key=lambda row: (
            0 if row.get('decision') == 'keep' else 1,
            (
                float(row['primary_metric_value'])
                if row.get('primary_metric_direction') == 'minimize' and isinstance(row.get('primary_metric_value'), (int, float))
                else -(float(row['primary_metric_value']) if isinstance(row.get('primary_metric_value'), (int, float)) else -1e9)
            ),
            row['iteration_id'],
        )
    )
    return rows


def refresh_campaign_iterations(
    store: RunStore,
    campaign: AutoresearchCampaignRecord,
    *,
    settings: Settings,
    submitter: JobSubmitter,
) -> list[AutoresearchIterationRecord]:
    refreshed: list[AutoresearchIterationRecord] = []
    changed = False
    for iteration in store.list_autoresearch_iterations(campaign.campaign_id):
        run = store.get_run(iteration.run_id)
        if run is None:
            refreshed.append(iteration)
            continue
        draft = store.get_methodology_draft(iteration.child_methodology_draft_id)
        score_summary = summarize_iteration_run(
            run,
            settings=settings,
            submitter=submitter,
            evaluator_contract=resolve_evaluator_contract(draft, campaign),
        )
        run_status = str(score_summary.get('run_status', 'unknown'))
        next_status = iteration.status
        if iteration.decision is None:
            if run_status == 'succeeded':
                next_status = 'completed'
            elif run_status in {'failed', 'rejected'}:
                next_status = 'needs_review'
            elif run_status in {'running', 'accepted', 'queued'}:
                next_status = 'launched'
        updated_iteration = iteration.model_copy(
            update={
                'updated_at': _now(),
                'status': next_status,
                'score_summary': score_summary,
            }
        )
        if updated_iteration != iteration:
            store.save_autoresearch_iteration(updated_iteration)
            changed = True
        refreshed.append(updated_iteration)
    if changed and campaign.latest_iteration_id:
        latest_iteration = store.get_autoresearch_iteration(campaign.latest_iteration_id)
        if latest_iteration is not None:
            next_campaign_status = campaign.status
            if campaign.latest_decision_id is None:
                if latest_iteration.status == 'completed':
                    next_campaign_status = 'active'
                elif latest_iteration.status == 'needs_review':
                    next_campaign_status = 'needs_review'
            if next_campaign_status != campaign.status:
                campaign = campaign.model_copy(update={'updated_at': _now(), 'status': next_campaign_status})
                store.save_autoresearch_campaign(campaign)
    return refreshed


def select_recommended_model(
    rows: list[dict[str, Any]],
    best_draft: MethodologyDraftRecord | None,
) -> str | None:
    for row in rows:
        best_model = str(row.get('best_model') or '').strip()
        if row.get('decision') == 'keep' and best_model:
            return best_model
    if best_draft is not None:
        models = best_draft.candidate_models or best_draft.architectures or best_draft.baselines
        if len(models) == 1:
            return models[0]
    for row in rows:
        models = list(row.get('candidate_models') or [])
        if row.get('decision') == 'keep' and len(models) == 1:
            return models[0]
    for row in rows:
        models = list(row.get('candidate_models') or [])
        if len(models) == 1:
            return models[0]
    return None


def build_next_variant_suggestions(
    rows: list[dict[str, Any]],
    best_draft: MethodologyDraftRecord | None,
) -> list[str]:
    suggestions, _ = build_next_variant_guidance(rows, best_draft)
    return suggestions


def build_next_variant_guidance(
    rows: list[dict[str, Any]],
    best_draft: MethodologyDraftRecord | None,
) -> tuple[list[str], list[AutoresearchSuggestedMutation]]:
    suggestions: list[str] = []
    mutations: list[AutoresearchSuggestedMutation] = []
    focus_row = next((row for row in rows if row.get('decision') == 'keep'), None)
    if focus_row is None and rows:
        focus_row = rows[0]
    if focus_row is not None:
        weakest_name = ''
        weakest_value = 2.0
        merged_components: dict[str, float] = {}
        for component_map in (focus_row.get('technique_components') or {}, focus_row.get('readiness_components') or {}):
            if not isinstance(component_map, dict):
                continue
            for key, value in component_map.items():
                if isinstance(value, (int, float)):
                    merged_components[str(key)] = float(value)
        for key, value in merged_components.items():
            if value < weakest_value:
                weakest_name = key
                weakest_value = value
        if best_draft is not None:
            execution_inputs = dict(best_draft.declared_inputs)
            method_spec = best_draft.method_spec
        else:
            execution_inputs = {}
            method_spec = None
        guidance_by_component: dict[str, tuple[str, AutoresearchSuggestedMutation]] = {
            'objective_contract': (
                'Run an explicit objective/loss variant and compare it against the current winner.',
                AutoresearchSuggestedMutation(
                    source_component='objective_contract',
                    mutation_axis='loss_or_distance',
                    summary='Try an alternate bounded objective or loss variant.',
                    suggested_updates={
                        'loss_or_distance': (method_spec.loss_or_distance if method_spec is not None and method_spec.loss_or_distance else 'alternate-approved-objective'),
                        'candidate_models': list(best_draft.candidate_models) if best_draft is not None else [],
                    },
                ),
            ),
            'metric_contract': (
                'Run a metric-emphasis variant aligned to the primary evaluation target.',
                AutoresearchSuggestedMutation(
                    source_component='metric_contract',
                    mutation_axis='metrics',
                    summary='Tighten the metric set around the primary evaluation target.',
                    suggested_updates={
                        'metrics': list(method_spec.metrics) if method_spec is not None else [],
                        'evaluation_target': execution_inputs.get('evaluation_target'),
                    },
                ),
            ),
            'candidate_contract': (
                'Run the next candidate-model comparison against the current kept method.',
                AutoresearchSuggestedMutation(
                    source_component='candidate_contract',
                    mutation_axis='candidate_models',
                    summary='Expand or rotate the bounded candidate model set.',
                    suggested_updates={
                        'candidate_models': list(best_draft.candidate_models) if best_draft is not None else [],
                        'baseline_models': list(best_draft.baselines) if best_draft is not None else [],
                    },
                ),
            ),
            'task_contract': (
                'Run a task-framing variant that tightens the declared methodology scope.',
                AutoresearchSuggestedMutation(
                    source_component='task_contract',
                    mutation_axis='task_type',
                    summary='Tighten the bounded task framing for the next iteration.',
                    suggested_updates={
                        'task_type': method_spec.task_type if method_spec is not None else None,
                        'model_family': execution_inputs.get('model_family'),
                    },
                ),
            ),
            'package_stack': (
                'Run a package-stack validation variant on the same method to confirm runtime coverage.',
                AutoresearchSuggestedMutation(
                    source_component='package_stack',
                    mutation_axis='runtime_validation',
                    summary='Validate the same method on an explicitly package-complete runner stack.',
                    suggested_updates={
                        'required_python_packages': list(method_spec.required_python_packages) if method_spec is not None else [],
                    },
                ),
            ),
            'runtime_stack': (
                'Run the kept method on a stronger resource profile or GPU-capable node class.',
                AutoresearchSuggestedMutation(
                    source_component='runtime_stack',
                    mutation_axis='resource_profile',
                    summary='Repeat the kept method with stronger runtime backing.',
                    suggested_updates={
                        'resource_profile': best_draft.resource_profile if best_draft is not None else None,
                        'node_selector': 'gpu-candidate',
                    },
                ),
            ),
            'split_contract': (
                'Run a stricter validation-split variant to probe overfitting risk.',
                AutoresearchSuggestedMutation(
                    source_component='split_contract',
                    mutation_axis='validation_strategy',
                    summary='Tighten the validation split policy on the same method.',
                    suggested_updates={
                        'validation_strategy': 'stratified_holdout',
                        'validation_split': '0.25',
                    },
                ),
            ),
            'target_alignment': (
                'Run a variant with a more explicit evaluation target and score contract.',
                AutoresearchSuggestedMutation(
                    source_component='target_alignment',
                    mutation_axis='evaluation_target',
                    summary='Make the evaluation target more explicit for the next run.',
                    suggested_updates={
                        'evaluation_target': execution_inputs.get('evaluation_target'),
                    },
                ),
            ),
        }
        if weakest_name:
            guidance = guidance_by_component.get(weakest_name)
            if guidance:
                suggestion, mutation = guidance
                suggestions.append(suggestion)
                mutations.append(mutation)

    if best_draft is not None:
        if len(best_draft.candidate_models) == 1:
            suggestions.append('Compare the current kept model against the next approved baseline.')
        suggestions.append('Run a bounded baseline-inclusion ablation on the same approved dataset split.')
        mutations.append(
            AutoresearchSuggestedMutation(
                source_component='baseline_inclusion',
                mutation_axis='baseline_models',
                summary='Add or preserve a bounded baseline comparison alongside the kept method.',
                suggested_updates={
                    'candidate_models': list(best_draft.candidate_models),
                    'baseline_models': list(best_draft.baselines),
                },
            )
        )

    deduped_mutations: list[AutoresearchSuggestedMutation] = []
    seen_mutations: set[tuple[str, str, str]] = set()
    for mutation in mutations:
        signature = (mutation.source_component, mutation.mutation_axis, json.dumps(mutation.suggested_updates, sort_keys=True))
        if signature in seen_mutations:
            continue
        seen_mutations.add(signature)
        deduped_mutations.append(mutation)

    return _dedupe(suggestions), deduped_mutations


def summarize_campaign(
    store: RunStore,
    campaign: AutoresearchCampaignRecord,
    *,
    settings: Settings | None = None,
    submitter: JobSubmitter | None = None,
) -> AutoresearchCampaignSummaryResponse:
    drafts = store.list_methodology_drafts(campaign.campaign_id)
    iterations = store.list_autoresearch_iterations(campaign.campaign_id)
    if settings is not None and submitter is not None:
        iterations = refresh_campaign_iterations(
            store,
            campaign,
            settings=settings,
            submitter=submitter,
        )
        campaign = store.get_autoresearch_campaign(campaign.campaign_id) or campaign
    decisions = store.list_autoresearch_decisions(campaign.campaign_id)
    best_draft = store.get_methodology_draft(campaign.current_best_methodology_draft_id or '')
    latest_run = None
    if campaign.latest_iteration_id:
        latest_iteration = store.get_autoresearch_iteration(campaign.latest_iteration_id)
        if latest_iteration is not None:
            latest_run = store.get_run(latest_iteration.run_id)
    model_comparison = build_model_comparison_rows(drafts, iterations, decisions)
    recommended_model = select_recommended_model(model_comparison, best_draft)
    proposed_next_variants, proposed_next_mutations = build_next_variant_guidance(model_comparison, best_draft)
    return AutoresearchCampaignSummaryResponse(
        campaign=campaign,
        methodology_drafts=drafts,
        iterations=iterations,
        decisions=decisions,
        best_methodology_draft=best_draft,
        latest_run=latest_run,
        recommended_model=recommended_model,
        model_comparison=model_comparison,
        proposed_next_variants=proposed_next_variants,
        proposed_next_mutations=proposed_next_mutations,
    )


def markdown_cell(lines: list[str]) -> dict[str, Any]:
    return {
        'cell_type': 'markdown',
        'metadata': {},
        'source': [line + '\n' for line in lines],
    }


def code_cell(lines: list[str]) -> dict[str, Any]:
    return {
        'cell_type': 'code',
        'execution_count': None,
        'metadata': {},
        'outputs': [],
        'source': [line + '\n' for line in lines],
    }


def build_autoresearch_notebook(
    campaign: AutoresearchCampaignRecord,
    methodology: MethodologyDraftRecord,
    workflow: WorkflowRegistryEntry | None = None,
) -> dict[str, Any]:
    runtime_requirements = workflow.runtime_requirements if workflow is not None else {}
    required_python_packages = runtime_requirements.get('required_python_packages', [])
    training_stack = runtime_requirements.get('training_stack', [])
    cells: list[dict[str, Any]] = [
        markdown_cell(
            [
                f'# Glasslab Autoresearch Campaign {campaign.campaign_id[:8]}',
                '',
                f'- objective: `{campaign.objective}`',
                f'- workflow: `{methodology.workflow_id}`',
                f'- methodology draft: `{methodology.methodology_draft_id}`',
                f'- method family: `{methodology.method_family}`',
            ]
        ),
        markdown_cell(
            [
                '## Hypothesis',
                '',
                methodology.hypothesis,
            ]
        ),
        markdown_cell(
            [
                '## Structured methodology',
                '',
                f'- datasets: `{", ".join(methodology.datasets) or "unknown"}`',
                f'- architectures: `{", ".join(methodology.architectures) or "unknown"}`',
                f'- baselines: `{", ".join(methodology.baselines) or "unknown"}`',
                f'- metrics: `{", ".join(methodology.metrics) or "unknown"}`',
                f'- resource profile: `{methodology.resource_profile}`',
                f'- bounded experimentability: `{methodology.bounded_experimentability}`',
                f'- preferred Python packages: `{", ".join(required_python_packages) or "unspecified"}`',
                f'- training stack: `{", ".join(training_stack) or "unspecified"}`',
            ]
        ),
        code_cell(
            [
                'import json',
                'from pprint import pprint',
                '',
                'methodology_draft = ' + json.dumps(methodology.model_dump(mode='json'), indent=2),
                '',
                'pprint(methodology_draft)',
            ]
        ),
        markdown_cell(
            [
                '## Mutation diff',
                '',
                'This notebook is a reviewable scaffold derived from the bounded methodology draft. It is not an executable authority by itself.',
            ]
        ),
        code_cell(
            [
                'mutation_diff = ' + json.dumps(methodology.mutation_diff, indent=2),
                'mutation_diff',
            ]
        ),
        markdown_cell(
            [
                '## Next checks',
                '',
                '- confirm the approved workflow inputs are still correct',
                '- confirm the required Python packages are available in the chosen runner image',
                '- confirm GPU scheduling only when the preferred workflow and resource profile require it',
                '- confirm the selected model family fits the bounded template',
                '- confirm the metric emphasis matches the research objective',
                '- only then launch the bounded validation run',
            ]
        ),
    ]
    return {
        'cells': cells,
        'metadata': {
            'kernelspec': {
                'display_name': 'Python 3',
                'language': 'python',
                'name': 'python3',
            },
            'language_info': {
                'name': 'python',
                'version': '3.11',
            },
            'glasslab': {
                'campaign_id': campaign.campaign_id,
                'methodology_draft_id': methodology.methodology_draft_id,
                'workflow_id': methodology.workflow_id,
                'kind': 'autoresearch-notebook-draft',
            },
        },
        'nbformat': 4,
        'nbformat_minor': 5,
    }


def write_autoresearch_notebook_draft(
    settings: Settings,
    campaign: AutoresearchCampaignRecord,
    methodology: MethodologyDraftRecord,
    workflow: WorkflowRegistryEntry | None = None,
    *,
    notebook: dict[str, Any] | None = None,
    filename: str = 'analysis_notebook.ipynb',
) -> tuple[str, dict[str, Any]]:
    notebook = notebook or build_autoresearch_notebook(campaign, methodology, workflow=workflow)
    target_dir = Path(settings.artifacts_mount_path) / 'workflow-api' / 'notebook-drafts' / campaign.campaign_id
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / filename
    path.write_text(json.dumps(notebook, indent=2), encoding='utf-8')
    return (path.as_uri(), notebook)


def build_coding_notebook_refinement_payload(
    campaign: AutoresearchCampaignRecord,
    methodology: MethodologyDraftRecord,
    workflow: WorkflowRegistryEntry | None,
    notebook: dict[str, Any],
    settings: Settings,
    *,
    design: DesignDraftRecord | None = None,
    interpretation: InterpretationRecord | None = None,
) -> dict[str, Any]:
    runtime_requirements = workflow.runtime_requirements if workflow is not None else {}
    return {
        'model': settings.coding_notebook_model,
        'stream': False,
        'format': 'json',
        'messages': [
            {
                'role': 'system',
                'content': (
                    'You refine Glasslab Jupyter notebooks for bounded, reviewable methodology validation. '
                    'Do not change the research objective, do not introduce unrestricted shell/code mutation, '
                    'and do not invent execution manifests. '
                    'Return only valid JSON with top-level keys "notebook" and optional "warnings".'
                ),
            },
            {
                'role': 'user',
                'content': json.dumps(
                    {
                        'request_id': methodology.methodology_draft_id,
                        'campaign': campaign.model_dump(mode='json'),
                        'methodology_draft': methodology.model_dump(mode='json'),
                        'workflow': workflow.model_dump(mode='json') if workflow is not None else None,
                        'design_draft': design.model_dump(mode='json') if design is not None else None,
                        'interpretation': interpretation.model_dump(mode='json') if interpretation is not None else None,
                        'runtime_requirements': runtime_requirements,
                        'notebook': notebook,
                        'instructions': [
                            'Refine this Glasslab notebook without changing its bounded research objective.',
                            'Keep the notebook reviewable and tied to the approved workflow template.',
                            'Add concrete but bounded cells for dataset loading, package requirements, metrics, experiment checks, and result interpretation where helpful.',
                            'Prefer one additional code cell for dataset or artifact loading and one additional markdown cell for runtime or evaluation checks.',
                            'Only mention Python packages that are already required by or clearly compatible with the workflow runtime requirements.',
                            'Preserve nbformat metadata and return the full notebook object.',
                        ],
                    },
                    indent=2,
                ),
            },
        ],
    }


def _extract_json_object(text: str) -> dict[str, Any]:
    text = text.strip()
    if not text:
        raise ValueError('coding notebook model returned empty content')
    try:
        payload = json.loads(text)
        if isinstance(payload, dict):
            return payload
    except json.JSONDecodeError:
        pass
    match = re.search(r'\{.*\}', text, re.DOTALL)
    if not match:
        raise ValueError('coding notebook model response did not contain a JSON object')
    payload = json.loads(match.group(0))
    if not isinstance(payload, dict):
        raise ValueError('coding notebook model response JSON was not an object')
    return payload


def _validate_notebook_payload(notebook: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(notebook, dict):
        raise ValueError('coding notebook model response missing notebook object')
    if not isinstance(notebook.get('cells'), list):
        raise ValueError('refined notebook is missing cells list')
    if notebook.get('nbformat') != 4:
        notebook['nbformat'] = 4
    notebook.setdefault('nbformat_minor', 5)
    metadata = notebook.get('metadata')
    if not isinstance(metadata, dict):
        notebook['metadata'] = {}
    return notebook


def _extract_imported_python_packages(notebook: dict[str, Any]) -> list[str]:
    packages: list[str] = []
    for cell in notebook.get('cells', []):
        if not isinstance(cell, dict) or cell.get('cell_type') != 'code':
            continue
        source = ''.join(cell.get('source', []))
        for raw_line in source.splitlines():
            line = raw_line.strip()
            if line.startswith('import '):
                targets = line.removeprefix('import ').split(',')
                for target in targets:
                    package = target.strip().split(' as ')[0].split('.')[0].strip()
                    if package:
                        packages.append(package)
            elif line.startswith('from '):
                package = line.removeprefix('from ').split(' import ')[0].split('.')[0].strip()
                if package:
                    packages.append(package)
    return _dedupe(packages)


def _validate_notebook_runtime_contract(
    notebook: dict[str, Any],
    workflow: WorkflowRegistryEntry | None,
) -> list[str]:
    if workflow is None:
        return []
    runtime_requirements = workflow.runtime_requirements or {}
    allowed_packages = _dedupe([str(item) for item in runtime_requirements.get('required_python_packages', [])])
    if not allowed_packages:
        return []
    imported_packages = _extract_imported_python_packages(notebook)
    extras = [package for package in imported_packages if package not in allowed_packages and package not in {'json', 'pathlib', 'pprint'}]
    if not extras:
        return []
    return [
        'refined notebook imports packages outside the approved workflow runtime: '
        + ', '.join(extras)
        + '; review before execution'
    ]


def call_coding_notebook_agent(
    campaign: AutoresearchCampaignRecord,
    methodology: MethodologyDraftRecord,
    workflow: WorkflowRegistryEntry | None,
    notebook: dict[str, Any],
    settings: Settings,
    *,
    design: DesignDraftRecord | None = None,
    interpretation: InterpretationRecord | None = None,
) -> tuple[dict[str, Any] | None, list[str]]:
    if not settings.coding_notebook_agent_enabled:
        return None, ['coding notebook agent is disabled; using deterministic notebook scaffold']

    payload = build_coding_notebook_refinement_payload(
        campaign,
        methodology,
        workflow,
        notebook,
        settings,
        design=design,
        interpretation=interpretation,
    )
    request_obj = urllib_request.Request(
        settings.coding_notebook_agent_url,
        data=json.dumps(payload).encode('utf-8'),
        headers={'Content-Type': 'application/json'},
        method='POST',
    )
    try:
        with urllib_request.urlopen(request_obj, timeout=settings.coding_notebook_agent_timeout_seconds) as response:
            body = json.loads(response.read().decode('utf-8'))
        content = (
            body.get('message', {}).get('content')
            if isinstance(body.get('message'), dict)
            else None
        )
        if not isinstance(content, str):
            raise ValueError('coding notebook model response missing message.content')
        parsed = _extract_json_object(content)
        refined = _validate_notebook_payload(parsed.get('notebook'))
        warnings = [str(item) for item in parsed.get('warnings', []) if isinstance(item, str)]
        warnings.extend(_validate_notebook_runtime_contract(refined, workflow))
        return refined, warnings
    except (urllib_error.URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        return None, [f'coding notebook agent fallback: {exc}']


def build_campaign_and_seed(
    store: RunStore,
    registry: WorkflowRegistry,
    request: AutoresearchCampaignCreateRequest,
) -> tuple[AutoresearchCampaignRecord, MethodologyDraftRecord]:
    session_id = request.session_id
    if session_id is None:
        session = store.get_latest_research_session()
        if session is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='no research session has been created yet')
        session_id = session.session_id
    design = _find_latest_design_for_campaign(store, session_id=session_id, source_design_id=request.source_design_id)
    workflow = registry.get_workflow(design.workflow_id)
    if workflow is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail='workflow registry entry not found')
    if workflow.execution_status != 'ready':
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail='workflow is not approved for autoresearch execution')
    objective = request.objective or design.objective
    campaign = build_autoresearch_campaign(
        request,
        session_id=session_id,
        source_design_id=design.design_id,
        objective=objective,
    )
    if campaign.evaluator_contract is None:
        campaign = campaign.model_copy(update={'evaluator_contract': _default_evaluator_contract_for_design(design)})
    seed = build_seed_methodology_draft(campaign, design, workflow)
    campaign = campaign.model_copy(
        update={
            'seed_methodology_draft_ids': [seed.methodology_draft_id],
            'current_best_methodology_draft_id': seed.methodology_draft_id,
        }
    )
    store.save_autoresearch_campaign(campaign)
    store.save_methodology_draft(seed)
    touch_research_session(
        store,
        session_id,
        latest_methodology_draft_id=seed.methodology_draft_id,
        latest_autoresearch_campaign_id=campaign.campaign_id,
        decision_log=[f'autoresearch campaign created: {campaign.campaign_id}'],
    )
    return (campaign, seed)
