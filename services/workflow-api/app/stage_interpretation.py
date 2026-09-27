"""Validate and build Interpretation records from external agent draft responses.

Call the interpretation agent for an intake, validate the returned draft
against the workflow registry (no unapproved workflow IDs, required string
fields present), merge catalog-enriched technique knowledge with the agent's
recommendations, and construct the authoritative Interpretation record.
The interpretation_source field tracks whether the agent returned a primary
result, fell back to a secondary model backend, or fell back entirely to
deterministic inference.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request
from uuid import uuid4

from .config import Settings
from .internal_agent_auth import internal_agent_headers
from .persistence import RunStore
from .registry import WorkflowRegistry
from .schemas import IntakeRecord, InterpretationRecord
from .stage_inference import (
    build_interpretation_notes,
    build_method_spec,
    build_technique_knowledge,
    catalog_workflow_ids,
    normalize_unique_strings,
)
from .technique_catalog import enrich_technique_knowledge_from_catalog, match_catalog_records_for_intake

LOGGER = logging.getLogger(__name__)


def _optional_clean_string(value: Any, *, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = ' '.join(value.split()).strip()
    return cleaned[:limit] or None


def validate_interpretation_agent_draft(
    draft: dict[str, Any],
    intake: IntakeRecord,
    registry: WorkflowRegistry,
) -> dict[str, Any]:
    _ = intake
    required_string_fields = ("source_type", "normalized_summary", "extracted_method_summary", "literature_state_summary")
    for field_name in required_string_fields:
        value = draft.get(field_name)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"interpretation agent draft missing valid {field_name}")

    normalized = {
        "source_type": draft["source_type"].strip(),
        "normalized_summary": " ".join(draft["normalized_summary"].split())[:500],
        "extracted_method_summary": " ".join(draft["extracted_method_summary"].split())[:500],
        "literature_state_summary": " ".join(draft["literature_state_summary"].split())[:500],
        "candidate_workflow_families": normalize_unique_strings(list(draft.get("candidate_workflow_families", []))),
        "dataset_hints": normalize_unique_strings(list(draft.get("dataset_hints", []))),
        "evaluation_targets": normalize_unique_strings(list(draft.get("evaluation_targets", []))),
        "extracted_claims": normalize_unique_strings(list(draft.get("extracted_claims", [])))[:3],
        "research_gaps": normalize_unique_strings(list(draft.get("research_gaps", [])))[:4],
        "bounded_experiment_ideas": normalize_unique_strings(list(draft.get("bounded_experiment_ideas", [])))[:3],
        "recommended_method_family": _optional_clean_string(draft.get("recommended_method_family"), limit=120),
        "recommended_datasets": normalize_unique_strings(list(draft.get("recommended_datasets", [])))[:4],
        "recommended_metrics": normalize_unique_strings(list(draft.get("recommended_metrics", [])))[:4],
        "recommended_baselines": normalize_unique_strings(list(draft.get("recommended_baselines", [])))[:4],
        "recommended_architectures": normalize_unique_strings(list(draft.get("recommended_architectures", [])))[:4],
        "recommended_python_packages": normalize_unique_strings(list(draft.get("recommended_python_packages", [])))[:6],
        "preferred_workflow_id": _optional_clean_string(draft.get("preferred_workflow_id"), limit=120),
        "preferred_resource_profile": _optional_clean_string(draft.get("preferred_resource_profile"), limit=120),
        "gpu_required": bool(draft.get("gpu_required", False)),
        "mutation_axes": normalize_unique_strings(list(draft.get("mutation_axes", [])))[:6],
        "unresolved_questions": normalize_unique_strings(list(draft.get("unresolved_questions", []))),
    }

    invalid_workflows = [
        workflow_id for workflow_id in normalized["candidate_workflow_families"]
        if registry.get_workflow(workflow_id) is None
    ]
    if invalid_workflows:
        raise ValueError(f'interpretation agent returned unapproved workflow ids: {", ".join(invalid_workflows)}')

    return normalized


def build_interpretation_record_from_agent_draft(
    intake: IntakeRecord,
    validated_draft: dict[str, Any],
    *,
    store: RunStore | None = None,
    interpretation_source: str = 'agent-primary',
    interpretation_backend: dict[str, Any] | None = None,
    interpretation_warnings: list[str] | None = None,
) -> InterpretationRecord:
    now = datetime.now(timezone.utc)
    unresolved_questions = list(validated_draft["unresolved_questions"])
    recommended_losses: list[str] = []
    technique_knowledge = build_technique_knowledge(
        intake=intake,
        dataset_hints=list(validated_draft.get("recommended_datasets", [])) or list(validated_draft["dataset_hints"]),
        evaluation_targets=list(validated_draft.get("recommended_metrics", [])) or list(validated_draft["evaluation_targets"]),
        recommended_method_family=validated_draft.get("recommended_method_family"),
        recommended_baselines=list(validated_draft.get("recommended_baselines", [])),
        recommended_architectures=list(validated_draft.get("recommended_architectures", [])),
        recommended_losses=recommended_losses,
        recommended_python_packages=list(validated_draft.get("recommended_python_packages", [])),
        mutation_axes=list(validated_draft.get("mutation_axes", [])),
    )
    matched_catalog_records = match_catalog_records_for_intake(intake, store) if store is not None else []
    candidate_workflow_families = normalize_unique_strings(
        [*catalog_workflow_ids(matched_catalog_records), *list(validated_draft["candidate_workflow_families"])]
    )
    technique_knowledge = enrich_technique_knowledge_from_catalog(technique_knowledge, matched_catalog_records)
    recommended_python_packages = normalize_unique_strings(
        [*list(validated_draft.get("recommended_python_packages", [])), *technique_knowledge.python_packages]
    )
    recommended_losses = normalize_unique_strings(
        [*recommended_losses, *technique_knowledge.losses_or_distances]
    )
    recommended_datasets = normalize_unique_strings(
        [*list(validated_draft.get("recommended_datasets", [])), *list(validated_draft["dataset_hints"])]
    )
    recommended_metrics = normalize_unique_strings(
        [*list(validated_draft.get("recommended_metrics", [])), *list(validated_draft["evaluation_targets"]), *technique_knowledge.metrics]
    )
    recommended_architectures = normalize_unique_strings(
        [*list(validated_draft.get("recommended_architectures", [])), *technique_knowledge.model_families]
    )
    recommended_datasets = normalize_unique_strings(
        [*recommended_datasets, *technique_knowledge.dataset_hints]
    )
    default_dataset_uri = next((record.default_dataset_uri for record in matched_catalog_records if record.default_dataset_uri), None)
    default_evaluation_target = next((record.default_evaluation_target for record in matched_catalog_records if record.default_evaluation_target), None)
    default_training_notes = next((record.default_training_notes for record in matched_catalog_records if record.default_training_notes), None)
    default_execution_inputs = next(
        (record.default_execution_inputs for record in matched_catalog_records if record.default_execution_inputs),
        {},
    )
    preferred_workflow_id = validated_draft.get("preferred_workflow_id")
    if matched_catalog_records and catalog_workflow_ids(matched_catalog_records):
        preferred_workflow_id = catalog_workflow_ids(matched_catalog_records)[0]
    elif preferred_workflow_id is None:
        for record in matched_catalog_records:
            if record.workflow_ids:
                preferred_workflow_id = record.workflow_ids[0]
                break
    preferred_resource_profile = validated_draft.get("preferred_resource_profile")
    if matched_catalog_records:
        for record in matched_catalog_records:
            if record.resource_profile:
                preferred_resource_profile = record.resource_profile
                break
    elif preferred_resource_profile is None:
        for record in matched_catalog_records:
            if record.resource_profile:
                preferred_resource_profile = record.resource_profile
                break
    gpu_required = bool(validated_draft.get("gpu_required", False)) or any(record.gpu_required for record in matched_catalog_records)
    method_spec = build_method_spec(
        intake=intake,
        objective=validated_draft["normalized_summary"],
        candidate_workflows=candidate_workflow_families,
        dataset_hints=recommended_datasets or list(validated_draft["dataset_hints"]),
        evaluation_targets=recommended_metrics or list(validated_draft["evaluation_targets"]),
        recommended_method_family=validated_draft.get("recommended_method_family"),
        recommended_baselines=list(validated_draft.get("recommended_baselines", [])),
        recommended_architectures=recommended_architectures,
        recommended_losses=recommended_losses,
        recommended_python_packages=recommended_python_packages,
        preferred_workflow_id=preferred_workflow_id,
        preferred_resource_profile=preferred_resource_profile,
        mutation_axes=list(validated_draft.get("mutation_axes", [])),
        default_dataset_uri=default_dataset_uri,
        default_evaluation_target=default_evaluation_target,
        default_training_notes=default_training_notes,
        default_execution_inputs=default_execution_inputs,
    )
    return InterpretationRecord(
        interpretation_id=uuid4().hex,
        intake_id=intake.intake_id,
        created_at=now,
        updated_at=now,
        status="ready_for_assessment" if not unresolved_questions else "needs_review",
        source_type=validated_draft["source_type"],
        normalized_summary=validated_draft["normalized_summary"],
        extracted_method_summary=validated_draft["extracted_method_summary"],
        literature_state_summary=validated_draft["literature_state_summary"],
        candidate_workflow_families=candidate_workflow_families,
        dataset_hints=validated_draft["dataset_hints"],
        evaluation_targets=validated_draft["evaluation_targets"],
        extracted_claims=validated_draft["extracted_claims"],
        research_gaps=validated_draft["research_gaps"],
        bounded_experiment_ideas=validated_draft["bounded_experiment_ideas"],
        recommended_method_family=validated_draft.get("recommended_method_family"),
        recommended_datasets=recommended_datasets,
        recommended_metrics=recommended_metrics,
        recommended_baselines=list(validated_draft.get("recommended_baselines", [])),
        recommended_architectures=recommended_architectures,
        recommended_python_packages=recommended_python_packages,
        preferred_workflow_id=preferred_workflow_id,
        preferred_resource_profile=preferred_resource_profile,
        gpu_required=gpu_required,
        mutation_axes=list(validated_draft.get("mutation_axes", [])),
        technique_knowledge=technique_knowledge,
        method_spec=method_spec,
        interpretation_source=interpretation_source,
        interpretation_backend=interpretation_backend,
        interpretation_warnings=list(interpretation_warnings or []),
        unresolved_questions=unresolved_questions,
        submitted_by=intake.submitted_by,
        session_id=intake.session_id,
    )


def call_interpretation_agent(
    intake: IntakeRecord,
    settings: Settings,
    registry: WorkflowRegistry,
    store: RunStore,
) -> InterpretationRecord | None:
    if not settings.interpretation_agent_enabled:
        return None

    payload = {
        "request_id": intake.intake_id,
        "intake": {
            "intake_id": intake.intake_id,
            "source_type": intake.source_type,
            "source_refs": intake.source_refs,
            "document_refs": intake.document_refs,
            "raw_request": intake.raw_request,
            "normalized_summary": intake.normalized_summary,
            "workflow_family_candidates": intake.workflow_family_candidates,
            "notes": build_interpretation_notes(intake, store),
            "submitted_by": intake.submitted_by,
        },
    }
    request_obj = urllib_request.Request(
        settings.interpretation_agent_url,
        data=json.dumps(payload).encode("utf-8"),
        headers=internal_agent_headers(settings),
        method="POST",
    )

    try:
        with urllib_request.urlopen(request_obj, timeout=settings.interpretation_agent_timeout_seconds) as response:
            body = json.loads(response.read().decode("utf-8"))
        draft = body.get("draft")
        if not isinstance(draft, dict):
            raise ValueError("interpretation agent response missing draft object")
        backend = body.get("model_backend")
        normalized_backend = backend if isinstance(backend, dict) else None
        warnings = normalize_unique_strings([str(item) for item in body.get("warnings", [])])
        interpretation_source = 'agent-primary'
        if any('used fallback interpretation backend' in warning for warning in warnings):
            interpretation_source = 'agent-fallback'
        # All-backends-failed fallback: the caller should probably re-run
        # with deterministic inference instead of an empty agent result.
        if any('all model backends failed' in warning for warning in warnings):
            interpretation_source = 'agent-deterministic'
        validated_draft = validate_interpretation_agent_draft(draft, intake, registry)
        return build_interpretation_record_from_agent_draft(
            intake,
            validated_draft,
            store=store,
            interpretation_source=interpretation_source,
            interpretation_backend=normalized_backend,
            interpretation_warnings=warnings,
        )
    except (urllib_error.URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        LOGGER.warning("interpretation-agent fallback for intake %s: %s", intake.intake_id, exc)
        return None
