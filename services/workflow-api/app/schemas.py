"""Pydantic request/response models and data records for the workflow API.

Every model uses extra='forbid' so unknown fields fail validation instead of
silently propagating. Models are split into three categories: create requests
(input validation), response/resource records (returned by endpoints), and
internal pipeline records (intake, interpretation, assessment, design, run).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from services.common.schemas import ArtifactsIndex, ExpectedArtifactsSpec, RunManifest, RunStatus


class RunCreateRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')

    workflow_id: str = Field(min_length=3)
    objective: str = Field(min_length=5)
    inputs: dict[str, Any] = Field(default_factory=dict)
    models: list[str] = Field(min_length=1)
    resource_profile: str | None = None
    run_priority: Literal['user', 'autonomous'] = 'user'
    submitted_by: str | None = None
    trace_id: str | None = None

    @field_validator('models')
    @classmethod
    def validate_unique_models(cls, value: list[str]) -> list[str]:
        deduped = list(dict.fromkeys(value))
        if len(deduped) != len(value):
            raise ValueError('models must be unique')
        return value


class IntakeCreateRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')

    raw_request: str = Field(min_length=10)
    source_refs: list[str] = Field(default_factory=list)
    document_refs: list[str] = Field(default_factory=list)
    technique_tags: list[str] = Field(default_factory=list)
    source_type: str | None = None
    notes: list[str] = Field(default_factory=list)
    submitted_by: str | None = None
    trace_id: str | None = None

    @field_validator('source_refs', 'document_refs', 'technique_tags', 'notes')
    @classmethod
    def validate_non_empty_unique_strings(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value if item.strip()]
        deduped = list(dict.fromkeys(cleaned))
        if len(deduped) != len(cleaned):
            raise ValueError('list entries must be unique')
        return deduped


class SourceDocumentIngestRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')

    source_url: str = Field(min_length=8)
    expected_title: str | None = None
    submitted_by: str | None = None


class FreshPaperPipelineRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')

    paper_ref: str = Field(min_length=8)
    raw_request: str | None = None
    notes: list[str] = Field(default_factory=list)
    dataset_uri: str | None = None
    submitted_by: str | None = None
    wait_for_terminal_state: bool = True
    wait_timeout_seconds: float = Field(default=45.0, ge=1.0, le=300.0)
    poll_interval_seconds: float = Field(default=2.0, ge=0.5, le=30.0)

    @field_validator('notes')
    @classmethod
    def validate_unique_notes(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value if item.strip()]
        deduped = list(dict.fromkeys(cleaned))
        if len(deduped) != len(cleaned):
            raise ValueError('notes entries must be unique')
        return deduped


class ResearchProblemPipelineRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')

    problem_statement: str = Field(min_length=12)
    max_candidate_papers: int = Field(default=3, ge=1, le=10)
    priorities: list[str] = Field(default_factory=list)
    submitted_by: str | None = None
    wait_for_terminal_state: bool = True
    wait_timeout_seconds: float = Field(default=45.0, ge=1.0, le=300.0)
    poll_interval_seconds: float = Field(default=2.0, ge=0.5, le=30.0)

    @field_validator('priorities')
    @classmethod
    def validate_unique_priorities(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value if item.strip()]
        deduped = list(dict.fromkeys(cleaned))
        if len(deduped) != len(cleaned):
            raise ValueError('priorities entries must be unique')
        return deduped


class PaperIntakeQueueCreateRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')

    problem_statement: str = Field(min_length=12)
    max_candidate_papers: int = Field(default=3, ge=1, le=25)
    priorities: list[str] = Field(default_factory=list)
    submitted_by: str | None = None

    @field_validator('priorities')
    @classmethod
    def validate_unique_priorities(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value if item.strip()]
        deduped = list(dict.fromkeys(cleaned))
        if len(deduped) != len(cleaned):
            raise ValueError('priorities entries must be unique')
        return deduped


class ManualPaperCandidateCreateRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')

    title: str = Field(min_length=6)
    official_page: str | None = None
    pdf_url: str | None = None
    year: int = Field(default=2026, ge=1900, le=2100)
    venue: str = 'manual'
    notes: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    submitted_by: str | None = None

    @field_validator('notes', 'tags')
    @classmethod
    def validate_unique_manual_paper_lists(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value if item.strip()]
        return list(dict.fromkeys(cleaned))


class ResearchSessionCreateRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')

    title: str | None = None
    goal_statement: str = Field(min_length=12)
    priorities: list[str] = Field(default_factory=list)
    submitted_by: str | None = None

    @field_validator('priorities')
    @classmethod
    def validate_unique_session_priorities(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value if item.strip()]
        deduped = list(dict.fromkeys(cleaned))
        if len(deduped) != len(cleaned):
            raise ValueError('priorities entries must be unique')
        return deduped


class ResearchSessionRecord(BaseModel):
    model_config = ConfigDict(extra='forbid')

    session_id: str
    created_at: datetime
    updated_at: datetime
    status: str
    title: str
    goal_statement: str
    priorities: list[str] = Field(default_factory=list)
    submitted_by: str
    working_notes: list[str] = Field(default_factory=list)
    decision_log: list[str] = Field(default_factory=list)
    next_experiment_ideas: list[str] = Field(default_factory=list)
    latest_problem_id: str | None = None
    latest_queue_id: str | None = None
    dataset_ids: list[str] = Field(default_factory=list)
    latest_dataset_id: str | None = None
    latest_document_id: str | None = None
    latest_intake_id: str | None = None
    latest_interpretation_id: str | None = None
    latest_assessment_id: str | None = None
    latest_design_id: str | None = None
    latest_run_id: str | None = None
    latest_methodology_draft_id: str | None = None
    latest_autoresearch_campaign_id: str | None = None
    latest_autoresearch_iteration_id: str | None = None
    latest_autoresearch_decision_id: str | None = None


class ResearchSessionMemoryAppendRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')

    working_note: str | None = None
    decision: str | None = None
    experiment_idea: str | None = None

    @field_validator('working_note', 'decision', 'experiment_idea')
    @classmethod
    def validate_optional_memory_entry(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = ' '.join(value.split()).strip()
        if not cleaned:
            raise ValueError('memory entries must not be empty')
        return cleaned


class SessionIntakeRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')

    source_url: str | None = None
    document_url: str | None = None
    note: str | None = None
    dataset_uri: str | None = None
    baseline_name: str | None = None
    submitted_by: str | None = None

    @field_validator('source_url', 'document_url', 'note', 'dataset_uri', 'baseline_name')
    @classmethod
    def validate_optional_session_intake_values(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = ' '.join(value.split()).strip()
        return cleaned or None


class SessionIntakeResponse(BaseModel):
    model_config = ConfigDict(extra='forbid')

    session: ResearchSessionRecord
    record_type: Literal['source_document', 'dataset', 'note', 'baseline']
    source_document: SourceDocumentRecord | None = None
    dataset: DatasetRecord | None = None
    recorded_value: str | None = None
    current_plan_status: str | None = None


class SessionDecisionRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')

    decision: Literal['keep', 'discard', 'revise']
    note: str | None = None
    submitted_by: str | None = None

    @field_validator('note')
    @classmethod
    def validate_optional_decision_note(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = ' '.join(value.split()).strip()
        return cleaned or None


class SessionDecisionResponse(BaseModel):
    model_config = ConfigDict(extra='forbid')

    session: ResearchSessionRecord
    operation: OperationRecord


class GenericExperimentRunRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')

    objective: str = Field(min_length=5)
    experiment_type: str = Field(min_length=3)
    workload_id: str = Field(min_length=3)
    parent_run_id: str | None = None
    campaign_id: str | None = None
    config_payload: dict[str, Any] = Field(default_factory=dict)
    dataset_bindings: dict[str, str] = Field(default_factory=dict)
    budget: dict[str, Any] = Field(default_factory=dict)
    artifact_contract: ExpectedArtifactsSpec | None = None
    metric_contract: dict[str, Any] = Field(default_factory=dict)
    submitted_by: str | None = None
    run_priority: Literal['user', 'autonomous'] = 'user'
    session_id: str | None = None

    @field_validator('dataset_bindings')
    @classmethod
    def validate_dataset_bindings(cls, value: dict[str, str]) -> dict[str, str]:
        cleaned: dict[str, str] = {}
        for key, raw in value.items():
            normalized_key = ' '.join(str(key).split()).strip()
            normalized_value = ' '.join(str(raw).split()).strip()
            if normalized_key and normalized_value:
                cleaned[normalized_key] = normalized_value
        return cleaned


class GenericExperimentResultIngestRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')

    terminal_status: Literal['succeeded', 'failed', 'rejected']
    metrics: dict[str, Any] = Field(default_factory=dict)
    artifact_refs: dict[str, str] = Field(default_factory=dict)
    runtime: dict[str, Any] = Field(default_factory=dict)
    detail: str | None = None

    @field_validator('artifact_refs')
    @classmethod
    def validate_artifact_refs(cls, value: dict[str, str]) -> dict[str, str]:
        cleaned: dict[str, str] = {}
        for key, raw in value.items():
            normalized_key = ' '.join(str(key).split()).strip()
            normalized_value = ' '.join(str(raw).split()).strip()
            if normalized_key and normalized_value:
                cleaned[normalized_key] = normalized_value
        return cleaned


class GenericExperimentCompareRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')

    run_ids: list[str] = Field(min_length=2)
    comparison_type: str = 'generic-experiment'
    evaluator_type: str | None = None
    metric_name: str | None = None
    higher_is_better: bool = True
    baseline_run_id: str | None = None
    session_id: str | None = None
    campaign_id: str | None = None
    workload_id: str | None = None
    workflow_id: str | None = None
    notes: list[str] = Field(default_factory=list)

    @field_validator('run_ids', 'notes')
    @classmethod
    def validate_unique_strings(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value if item.strip()]
        return list(dict.fromkeys(cleaned))


class ResearchProblemRecord(BaseModel):
    model_config = ConfigDict(extra='forbid')

    problem_id: str
    created_at: datetime
    updated_at: datetime
    status: str
    problem_statement: str
    max_candidate_papers: int = Field(default=3, ge=1, le=10)
    priorities: list[str] = Field(default_factory=list)
    submitted_by: str
    session_id: str | None = None


class IntakeRecord(BaseModel):
    model_config = ConfigDict(extra='forbid')

    intake_id: str
    created_at: datetime
    updated_at: datetime
    status: str
    source_type: str
    source_refs: list[str] = Field(default_factory=list)
    document_refs: list[str] = Field(default_factory=list)
    technique_tags: list[str] = Field(default_factory=list)
    raw_request: str
    normalized_summary: str
    workflow_family_candidates: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    submitted_by: str
    session_id: str | None = None


class DatasetCreateRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')

    name: str | None = None
    uri: str = Field(min_length=8)
    modality: str | None = None
    task_type: str | None = None
    label_field: str | None = None
    image_field: str | None = None
    split_strategy: str | None = None
    provenance_notes: list[str] = Field(default_factory=list)
    submitted_by: str | None = None

    @field_validator('provenance_notes')
    @classmethod
    def validate_unique_dataset_notes(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value if item.strip()]
        return list(dict.fromkeys(cleaned))

    @field_validator('name', 'modality', 'task_type', 'label_field', 'image_field', 'split_strategy')
    @classmethod
    def validate_optional_dataset_strings(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = ' '.join(value.split()).strip()
        return cleaned or None


class SessionDatasetAttachRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')

    dataset_id: str = Field(min_length=4)

    @field_validator('dataset_id')
    @classmethod
    def validate_dataset_id(cls, value: str) -> str:
        cleaned = ' '.join(value.split()).strip()
        if not cleaned:
            raise ValueError('dataset_id must not be empty')
        return cleaned


class DatasetRecord(BaseModel):
    model_config = ConfigDict(extra='forbid')

    dataset_id: str
    created_at: datetime
    updated_at: datetime
    status: str
    name: str
    uri: str
    modality: str | None = None
    task_type: str | None = None
    label_field: str | None = None
    image_field: str | None = None
    split_strategy: str | None = None
    provenance_notes: list[str] = Field(default_factory=list)
    submitted_by: str


class TechniqueCatalogImportCard(BaseModel):
    model_config = ConfigDict(extra='forbid')

    name: str = Field(min_length=3)
    aliases: list[str] = Field(default_factory=list)
    summary: str | None = None
    problem_types: list[str] = Field(default_factory=list)
    algorithm_family: str | None = None
    specific_algorithms: list[str] = Field(default_factory=list)
    automl_frameworks: list[str] = Field(default_factory=list)
    preprocessing_steps: list[str] = Field(default_factory=list)
    loss_functions: list[str] = Field(default_factory=list)
    optimizers: list[str] = Field(default_factory=list)
    hyperparameter_optimization: list[str] = Field(default_factory=list)
    validation_strategies: list[str] = Field(default_factory=list)
    primary_metrics: list[str] = Field(default_factory=list)
    uncertainty_quantification: list[str] = Field(default_factory=list)
    python_packages: list[str] = Field(default_factory=list)
    gpu_required: bool = False
    resource_profile: str | None = None
    workflow_ids: list[str] = Field(default_factory=list)
    template_compatibility: list[str] = Field(default_factory=list)
    dataset_hints: list[str] = Field(default_factory=list)
    default_dataset_uri: str | None = None
    default_evaluation_target: str | None = None
    default_training_notes: str | None = None
    default_execution_inputs: dict[str, str] = Field(default_factory=dict)
    common_failure_modes: list[str] = Field(default_factory=list)
    source_refs: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    @field_validator(
        'aliases',
        'problem_types',
        'specific_algorithms',
        'automl_frameworks',
        'preprocessing_steps',
        'loss_functions',
        'optimizers',
        'hyperparameter_optimization',
        'validation_strategies',
        'primary_metrics',
        'uncertainty_quantification',
        'python_packages',
        'workflow_ids',
        'template_compatibility',
        'dataset_hints',
        'common_failure_modes',
        'source_refs',
        'notes',
    )
    @classmethod
    def validate_unique_card_lists(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value if item.strip()]
        return list(dict.fromkeys(cleaned))

    @field_validator('summary')
    @classmethod
    def validate_summary(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = ' '.join(value.split()).strip()
        return cleaned or None

    @field_validator('default_dataset_uri', 'default_evaluation_target', 'default_training_notes')
    @classmethod
    def validate_optional_card_strings(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = ' '.join(value.split()).strip()
        return cleaned or None

    @field_validator('default_execution_inputs')
    @classmethod
    def validate_default_execution_inputs(cls, value: dict[str, str]) -> dict[str, str]:
        cleaned: dict[str, str] = {}
        for key, raw in value.items():
            normalized_key = ' '.join(str(key).split()).strip()
            normalized_value = ' '.join(str(raw).split()).strip()
            if not normalized_key or not normalized_value:
                continue
            cleaned[normalized_key] = normalized_value
        return cleaned


class TechniqueCatalogImportRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')

    cards: list[TechniqueCatalogImportCard] = Field(min_length=1)
    import_source: str = 'notebooklm-manual-export'
    replace_existing: bool = False


class TechniqueCatalogRecord(BaseModel):
    model_config = ConfigDict(extra='forbid')

    technique_id: str
    created_at: datetime
    updated_at: datetime
    name: str
    aliases: list[str] = Field(default_factory=list)
    summary: str | None = None
    problem_types: list[str] = Field(default_factory=list)
    algorithm_family: str | None = None
    specific_algorithms: list[str] = Field(default_factory=list)
    automl_frameworks: list[str] = Field(default_factory=list)
    preprocessing_steps: list[str] = Field(default_factory=list)
    loss_functions: list[str] = Field(default_factory=list)
    optimizers: list[str] = Field(default_factory=list)
    hyperparameter_optimization: list[str] = Field(default_factory=list)
    validation_strategies: list[str] = Field(default_factory=list)
    primary_metrics: list[str] = Field(default_factory=list)
    uncertainty_quantification: list[str] = Field(default_factory=list)
    python_packages: list[str] = Field(default_factory=list)
    gpu_required: bool = False
    resource_profile: str | None = None
    workflow_ids: list[str] = Field(default_factory=list)
    template_compatibility: list[str] = Field(default_factory=list)
    dataset_hints: list[str] = Field(default_factory=list)
    default_dataset_uri: str | None = None
    default_evaluation_target: str | None = None
    default_training_notes: str | None = None
    default_execution_inputs: dict[str, str] = Field(default_factory=dict)
    common_failure_modes: list[str] = Field(default_factory=list)
    source_refs: list[str] = Field(default_factory=list)
    import_source: str = 'notebooklm-manual-export'
    notes: list[str] = Field(default_factory=list)


class TechniqueKnowledgeRecord(BaseModel):
    model_config = ConfigDict(extra='forbid')

    model_families: list[str] = Field(default_factory=list)
    baseline_families: list[str] = Field(default_factory=list)
    metrics: list[str] = Field(default_factory=list)
    losses_or_distances: list[str] = Field(default_factory=list)
    split_strategies: list[str] = Field(default_factory=list)
    python_packages: list[str] = Field(default_factory=list)
    dataset_hints: list[str] = Field(default_factory=list)
    failure_modes: list[str] = Field(default_factory=list)
    catalog_technique_ids: list[str] = Field(default_factory=list)
    source_scope: str = 'paper'


class PrimaryMetricContract(BaseModel):
    model_config = ConfigDict(extra='forbid')

    name: str = Field(min_length=1)
    direction: Literal['maximize', 'minimize'] = 'maximize'
    minimum_effect: float = Field(default=0.0, ge=0.0)


class GuardrailMetricContract(BaseModel):
    model_config = ConfigDict(extra='forbid')

    name: str = Field(min_length=1)
    direction: Literal['maximize', 'minimize'] = 'maximize'
    minimum: float | None = None
    maximum: float | None = None
    required: bool = False


class EvaluatorContract(BaseModel):
    model_config = ConfigDict(extra='forbid')

    evaluator_type: str = Field(default='generic', min_length=1)
    primary_metric: PrimaryMetricContract | None = None
    guardrails: list[GuardrailMetricContract] = Field(default_factory=list)
    uncertainty: dict[str, Any] = Field(default_factory=dict)


class BudgetContract(BaseModel):
    model_config = ConfigDict(extra='forbid')

    budget_mode: Literal['wallclock', 'training_exposure', 'fixed_steps', 'manual'] = 'manual'
    max_wallclock_minutes: int | None = Field(default=None, ge=1)
    max_samples_seen: int | None = Field(default=None, ge=1)
    max_optimizer_steps: int | None = Field(default=None, ge=1)


class MethodSpecRecord(BaseModel):
    model_config = ConfigDict(extra='forbid')

    objective: str
    workflow_id: str | None = None
    task_type: str | None = None
    candidate_models: list[str] = Field(default_factory=list)
    baseline_models: list[str] = Field(default_factory=list)
    dataset_hints: list[str] = Field(default_factory=list)
    dataset_uri: str | None = None
    split_strategy: str | None = None
    metrics: list[str] = Field(default_factory=list)
    loss_or_distance: str | None = None
    required_python_packages: list[str] = Field(default_factory=list)
    resource_profile: str | None = None
    execution_inputs: dict[str, Any] = Field(default_factory=dict)
    mutation_axes: list[str] = Field(default_factory=list)
    evaluator_contract: EvaluatorContract | None = None
    budget_contract: BudgetContract | None = None
    run_readiness: Literal['ready', 'needs_review', 'blocked'] = 'needs_review'
    blocking_reasons: list[str] = Field(default_factory=list)


class InterpretationRecord(BaseModel):
    model_config = ConfigDict(extra='forbid')

    interpretation_id: str
    intake_id: str
    created_at: datetime
    updated_at: datetime
    status: str
    source_type: str
    normalized_summary: str
    extracted_method_summary: str
    literature_state_summary: str
    candidate_workflow_families: list[str] = Field(default_factory=list)
    dataset_hints: list[str] = Field(default_factory=list)
    evaluation_targets: list[str] = Field(default_factory=list)
    extracted_claims: list[str] = Field(default_factory=list)
    research_gaps: list[str] = Field(default_factory=list)
    bounded_experiment_ideas: list[str] = Field(default_factory=list)
    recommended_method_family: str | None = None
    recommended_datasets: list[str] = Field(default_factory=list)
    recommended_metrics: list[str] = Field(default_factory=list)
    recommended_baselines: list[str] = Field(default_factory=list)
    recommended_architectures: list[str] = Field(default_factory=list)
    recommended_python_packages: list[str] = Field(default_factory=list)
    preferred_workflow_id: str | None = None
    preferred_resource_profile: str | None = None
    gpu_required: bool = False
    mutation_axes: list[str] = Field(default_factory=list)
    technique_knowledge: TechniqueKnowledgeRecord = Field(default_factory=TechniqueKnowledgeRecord)
    method_spec: MethodSpecRecord | None = None
    interpretation_source: str = 'deterministic'
    interpretation_backend: dict[str, Any] | None = None
    interpretation_warnings: list[str] = Field(default_factory=list)
    unresolved_questions: list[str] = Field(default_factory=list)
    submitted_by: str
    session_id: str | None = None


class ReplicabilityAssessmentRecord(BaseModel):
    model_config = ConfigDict(extra='forbid')

    assessment_id: str
    interpretation_id: str
    intake_id: str
    created_at: datetime
    updated_at: datetime
    status: str
    recommendation: str
    recommended_workflow_id: str | None = None
    candidate_workflow_families: list[str] = Field(default_factory=list)
    unresolved_fields: list[str] = Field(default_factory=list)
    blocking_reasons: list[str] = Field(default_factory=list)
    approval_tier: str | None = None
    assessment_notes: list[str] = Field(default_factory=list)
    submitted_by: str
    session_id: str | None = None


class DesignDraftRecord(BaseModel):
    model_config = ConfigDict(extra='forbid')

    design_id: str
    intake_id: str
    source_assessment_id: str | None = None
    created_at: datetime
    updated_at: datetime
    status: str
    workflow_id: str
    workflow_family: str
    objective: str
    declared_inputs: dict[str, Any] = Field(default_factory=dict)
    unresolved_inputs: list[str] = Field(default_factory=list)
    candidate_models: list[str] = Field(default_factory=list)
    resource_profile: str
    expected_artifacts: dict[str, list[str]]
    approval_tier: str
    design_notes: list[str] = Field(default_factory=list)
    method_spec: MethodSpecRecord | None = None
    submitted_by: str
    session_id: str | None = None


class MethodologyDraftRecord(BaseModel):
    model_config = ConfigDict(extra='forbid')

    methodology_draft_id: str
    campaign_id: str
    session_id: str
    source_intake_id: str | None = None
    source_design_id: str | None = None
    parent_methodology_draft_id: str | None = None
    created_at: datetime
    updated_at: datetime
    objective: str
    hypothesis: str
    method_family: str
    datasets: list[str] = Field(default_factory=list)
    architectures: list[str] = Field(default_factory=list)
    baselines: list[str] = Field(default_factory=list)
    metrics: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    bounded_experimentability: str
    status: Literal['seed', 'ready_for_execution', 'launched', 'kept', 'discarded', 'needs_review']
    workflow_id: str
    workflow_family: str
    declared_inputs: dict[str, Any] = Field(default_factory=dict)
    candidate_models: list[str] = Field(default_factory=list)
    resource_profile: str
    approval_tier: str
    method_spec: MethodSpecRecord | None = None
    mutation_diff: dict[str, Any] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)


class AutoresearchCampaignRecord(BaseModel):
    model_config = ConfigDict(extra='forbid')

    campaign_id: str
    session_id: str
    created_at: datetime
    updated_at: datetime
    status: Literal['created', 'drafted', 'active', 'needs_review', 'completed']
    objective: str
    source_design_id: str | None = None
    seed_methodology_draft_ids: list[str] = Field(default_factory=list)
    current_best_methodology_draft_id: str | None = None
    latest_iteration_id: str | None = None
    latest_decision_id: str | None = None
    max_iterations: int = Field(default=3, ge=1, le=25)
    evaluation_policy: str
    mutation_policy: str
    evaluator_contract: EvaluatorContract | None = None
    budget_contract: BudgetContract | None = None
    notes: list[str] = Field(default_factory=list)


class AutoresearchIterationRecord(BaseModel):
    model_config = ConfigDict(extra='forbid')

    iteration_id: str
    campaign_id: str
    parent_methodology_draft_id: str | None = None
    child_methodology_draft_id: str
    run_id: str
    created_at: datetime
    updated_at: datetime
    status: Literal['launched', 'completed', 'decided', 'needs_review']
    score_summary: dict[str, Any] = Field(default_factory=dict)
    comparison_summary: dict[str, Any] = Field(default_factory=dict)
    decision: Literal['keep', 'discard', 'escalate_for_review'] | None = None


class AutoresearchDecisionRecord(BaseModel):
    model_config = ConfigDict(extra='forbid')

    decision_id: str
    campaign_id: str
    iteration_id: str
    created_at: datetime
    decision_type: Literal['keep', 'discard', 'escalate_for_review']
    rationale: str
    evidence_refs: list[str] = Field(default_factory=list)


class DesignDraftReviewRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')

    resolved_inputs: dict[str, Any] = Field(default_factory=dict)
    review_notes: list[str] = Field(default_factory=list)

    @field_validator('review_notes')
    @classmethod
    def validate_unique_review_notes(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value if item.strip()]
        deduped = list(dict.fromkeys(cleaned))
        if len(deduped) != len(cleaned):
            raise ValueError('review_notes entries must be unique')
        return deduped


class DigestScheduleCreateRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')

    cron_expr: str = Field(min_length=5)
    digest_kind: str = Field(min_length=3)
    scope_filter: dict[str, Any] = Field(default_factory=dict)
    owner: str | None = None


class ApprovedRerunScheduleCreateRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')

    cron_expr: str = Field(min_length=5)
    owner: str | None = None


class ScheduledOperationRecord(BaseModel):
    model_config = ConfigDict(extra='forbid')

    schedule_id: str
    created_at: datetime
    updated_at: datetime
    status: str
    operation_type: str
    approval_tier: str
    owner: str
    cron_expr: str
    scope_filter: dict[str, Any] = Field(default_factory=dict)
    digest_kind: str | None = None
    source_design_id: str | None = None
    source_run_id: str | None = None
    workflow_id: str | None = None
    allowed_dataset_uri: str | None = None
    allowed_model_ids: list[str] = Field(default_factory=list)
    allowed_runner_image: str | None = None
    resource_profile: str | None = None
    last_execution_at: datetime | None = None
    last_result_status: str | None = None
    last_result_detail: str | None = None


class ScheduledExecutionRecord(BaseModel):
    model_config = ConfigDict(extra='forbid')

    execution_id: str
    schedule_id: str
    operation_type: str
    started_at: datetime
    finished_at: datetime
    result_status: str
    result_detail: str
    produced_run_ids: list[str] = Field(default_factory=list)
    digest_payload: dict[str, Any] = Field(default_factory=dict)


class OperationRecord(BaseModel):
    model_config = ConfigDict(extra='forbid')

    operation_id: str
    operation_type: str
    status: Literal['completed', 'failed']
    started_at: datetime
    finished_at: datetime
    session_id: str | None = None
    queue_id: str | None = None
    document_id: str | None = None
    intake_id: str | None = None
    result_detail: str
    error_detail: str | None = None


class ComparisonRecord(BaseModel):
    model_config = ConfigDict(extra='forbid')

    comparison_id: str
    created_at: datetime
    updated_at: datetime
    status: Literal['pending', 'completed', 'failed']
    comparison_type: str
    evaluator_type: str
    session_id: str | None = None
    campaign_id: str | None = None
    workload_id: str | None = None
    workflow_id: str | None = None
    run_ids: list[str] = Field(default_factory=list)
    baseline_run_id: str | None = None
    candidate_run_ids: list[str] = Field(default_factory=list)
    summary_metrics: dict[str, Any] = Field(default_factory=dict)
    artifact_refs: dict[str, str] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)

    @field_validator('run_ids', 'candidate_run_ids', 'notes')
    @classmethod
    def validate_unique_non_empty_strings(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value if item.strip()]
        return list(dict.fromkeys(cleaned))


class ValidationIssue(BaseModel):
    model_config = ConfigDict(extra='forbid')

    field: str
    message: str


class JobSubmissionReceipt(BaseModel):
    model_config = ConfigDict(extra='forbid')

    job_name: str
    namespace: str
    accepted_at: datetime
    status: str
    detail: str


class WorkflowFamilySummary(BaseModel):
    model_config = ConfigDict(extra='forbid')

    workflow_id: str
    display_name: str
    workflow_family: str
    description: str
    allowed_models: list[str]
    resource_profile: str
    approval_tier: str
    execution_status: str
    submission_backend: str
    execution_blockers: list[str] = Field(default_factory=list)


class ExecutionPreflightResult(BaseModel):
    model_config = ConfigDict(extra='forbid')

    workflow_id: str
    runner_image: str
    resource_profile: str
    resource_requests: dict[str, str] = Field(default_factory=dict)
    resource_limits: dict[str, str] = Field(default_factory=dict)
    node_selector: dict[str, str] = Field(default_factory=dict)
    job_submission_mode: str
    execution_status: str
    submission_backend: str
    runtime_requirements: dict[str, Any] = Field(default_factory=dict)
    ready: bool
    eligible_nodes: list[str] = Field(default_factory=list)
    blocking_issues: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class RunRecord(BaseModel):
    model_config = ConfigDict(extra='forbid')

    run_id: str
    workflow_id: str
    created_at: datetime
    updated_at: datetime
    manifest: RunManifest
    status: RunStatus
    job_submission: JobSubmissionReceipt
    source_design_id: str | None = None
    source_intake_id: str | None = None
    run_purpose: str | None = None
    run_priority: Literal['user', 'autonomous'] = 'user'
    validation_issues: list[ValidationIssue] = Field(default_factory=list)
    session_id: str | None = None
    reported_metrics: dict[str, Any] = Field(default_factory=dict)
    artifact_refs: dict[str, str] = Field(default_factory=dict)
    runtime_summary: dict[str, Any] = Field(default_factory=dict)
    investigation_id: str | None = None
    source_plan_id: str | None = None
    source_approval_id: str | None = None
    source_execution_id: str | None = None
    plan_sha256: str | None = Field(default=None, min_length=64, max_length=64)
    artifact_bundle_verified: bool = False


class InvestigationHypothesisRecord(BaseModel):
    model_config = ConfigDict(extra='forbid')

    hypothesis_id: str
    statement: str = Field(min_length=8)
    created_at: datetime
    submitted_by: str


class ImmutableAssetReference(BaseModel):
    model_config = ConfigDict(extra='forbid')

    uri: str = Field(min_length=3)
    sha256: str = Field(min_length=64, max_length=64, pattern=r'^[0-9a-f]{64}$')
    media_type: str | None = None

    @field_validator('uri')
    @classmethod
    def normalize_asset_uri(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError('asset reference values must not be blank')
        prefixes = (
            's3://datasets/',
            's3://glasslab-datasets/',
            's3://artifacts/',
        )
        prefix = next(
            (candidate for candidate in prefixes if cleaned.startswith(candidate)),
            None,
        )
        if prefix is None:
            raise ValueError(
                'immutable investigation assets must use an approved data or '
                'artifact plane'
            )
        relative = cleaned.removeprefix(prefix)
        path = PurePosixPath(relative)
        if (
            not relative
            or path.as_posix() == '.'
            or path.is_absolute()
            or '..' in path.parts
        ):
            raise ValueError('immutable investigation asset path is invalid')
        return cleaned

    @field_validator('media_type')
    @classmethod
    def normalize_asset_media_type(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            raise ValueError('asset reference values must not be blank')
        return cleaned


class InvestigationDatasetBinding(BaseModel):
    model_config = ConfigDict(extra='forbid')

    name: str = Field(min_length=1)
    asset: ImmutableAssetReference
    role: Literal['train', 'validation', 'test', 'reference', 'labels', 'input']
    contains_labels: bool = False
    access_scopes: list[Literal['solve', 'train', 'validate', 'evaluate']] = Field(
        default_factory=lambda: ['solve', 'train', 'validate', 'evaluate'],
        min_length=1,
    )

    @field_validator('name')
    @classmethod
    def normalize_dataset_binding_text(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError('dataset binding values must not be blank')
        return cleaned

    @field_validator('access_scopes')
    @classmethod
    def validate_unique_access_scopes(
        cls,
        value: list[Literal['solve', 'train', 'validate', 'evaluate']],
    ) -> list[Literal['solve', 'train', 'validate', 'evaluate']]:
        if len(set(value)) != len(value):
            raise ValueError('dataset access_scopes must be unique')
        return value


class InvestigationWorkspaceSpec(BaseModel):
    model_config = ConfigDict(extra='forbid')

    task_bundle: ImmutableAssetReference
    source_bundle: ImmutableAssetReference
    working_directory: str = '.'
    command: list[str] = Field(min_length=1)
    output_directory: Literal['/outputs'] = '/outputs'
    network_policy: Literal['none', 'approved-egress'] = 'none'

    @field_validator('working_directory')
    @classmethod
    def validate_working_directory(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError('working_directory must not be blank')
        if cleaned.startswith('/'):
            raise ValueError('working_directory must be relative to the frozen workspace')
        if '..' in cleaned.split('/'):
            raise ValueError('working_directory must stay inside the frozen workspace')
        return cleaned

    @field_validator('command')
    @classmethod
    def validate_workspace_command(cls, value: list[str]) -> list[str]:
        cleaned = [str(item).strip() for item in value]
        if not cleaned or any(not item for item in cleaned):
            raise ValueError('workspace command entries must not be blank')
        if len(cleaned) > 64 or any(len(item) > 1024 for item in cleaned):
            raise ValueError('workspace command exceeds the bounded argv contract')
        if cleaned[0] not in {'python3'}:
            raise ValueError('workspace command executable is not approved')
        if any(any(ord(character) < 32 for character in item) for item in cleaned):
            raise ValueError('workspace command entries must not contain control characters')
        return cleaned


class InvestigationExecutionSpec(BaseModel):
    model_config = ConfigDict(extra='forbid')

    execution_id: str = Field(min_length=1)
    objective: str = Field(min_length=8)
    experiment_type: str = Field(min_length=3)
    workload_id: str = Field(min_length=3)
    data_access_scope: Literal['solve', 'train', 'validate', 'evaluate']
    depends_on: list[str] = Field(default_factory=list)
    workspace: InvestigationWorkspaceSpec
    dataset_bindings: list[InvestigationDatasetBinding] = Field(default_factory=list)
    config_payload: dict[str, Any] = Field(default_factory=dict)
    budget: BudgetContract
    artifact_contract: ExpectedArtifactsSpec
    evaluator_contract: EvaluatorContract

    @field_validator('execution_id', 'objective', 'experiment_type', 'workload_id')
    @classmethod
    def normalize_execution_text(cls, value: str) -> str:
        cleaned = ' '.join(value.split()).strip()
        if not cleaned:
            raise ValueError('execution values must not be blank')
        return cleaned

    @field_validator('depends_on')
    @classmethod
    def validate_unique_dependencies(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value if item.strip()]
        if len(set(cleaned)) != len(cleaned):
            raise ValueError('execution depends_on entries must be unique')
        return cleaned

    @model_validator(mode='after')
    def validate_execution_contract(self) -> 'InvestigationExecutionSpec':
        if self.budget.max_wallclock_minutes is None:
            raise ValueError('budget.max_wallclock_minutes is required for an executable plan')
        if self.execution_id in self.depends_on:
            raise ValueError('execution cannot depend on itself')
        names = [binding.name for binding in self.dataset_bindings]
        if len(set(names)) != len(names):
            raise ValueError('dataset binding names must be unique')
        return self


class InvestigationPlanRecord(BaseModel):
    model_config = ConfigDict(extra='forbid')

    plan_id: str
    revision: int = Field(ge=1)
    created_at: datetime
    submitted_by: str
    title: str = Field(min_length=3)
    rationale: str = Field(min_length=8)
    hypothesis_ids: list[str] = Field(min_length=1)
    executions: list[InvestigationExecutionSpec] = Field(min_length=1)

    @model_validator(mode='after')
    def validate_execution_graph(self) -> 'InvestigationPlanRecord':
        execution_ids = [execution.execution_id for execution in self.executions]
        if len(set(execution_ids)) != len(execution_ids):
            raise ValueError('plan execution_ids must be unique')
        known_ids = set(execution_ids)
        for execution in self.executions:
            unknown = sorted(set(execution.depends_on) - known_ids)
            if unknown:
                raise ValueError(
                    f'execution {execution.execution_id} has unknown dependencies: '
                    + ', '.join(unknown)
                )

        remaining = {
            execution.execution_id: set(execution.depends_on)
            for execution in self.executions
        }
        resolved: set[str] = set()
        while remaining:
            ready = {
                execution_id
                for execution_id, dependencies in remaining.items()
                if dependencies <= resolved
            }
            if not ready:
                raise ValueError('plan execution dependencies must form an acyclic graph')
            resolved.update(ready)
            for execution_id in ready:
                del remaining[execution_id]
        return self


class InvestigationPlanSnapshot(BaseModel):
    model_config = ConfigDict(extra='forbid')

    investigation_id: str
    research_mode: Literal['exploratory', 'confirmatory']
    research_question: str
    hypotheses: list[InvestigationHypothesisRecord] = Field(min_length=1)
    plan: InvestigationPlanRecord


class InvestigationPlanApprovalRecord(BaseModel):
    model_config = ConfigDict(extra='forbid')

    approval_id: str
    plan_id: str
    plan_sha256: str = Field(min_length=64, max_length=64)
    approved_at: datetime
    approved_by: str
    hypothesis_ids: list[str] = Field(min_length=1)
    research_mode: Literal['exploratory', 'confirmatory']
    plan_snapshot: InvestigationPlanSnapshot
    note: str | None = None


class InvestigationEvidenceReference(BaseModel):
    model_config = ConfigDict(extra='forbid')

    run_id: str
    artifact_name: str = Field(min_length=1)
    artifact_ref: str = Field(min_length=1)
    artifact_sha256: str = Field(
        min_length=64,
        max_length=64,
        pattern=r'^[0-9a-f]{64}$',
    )


class InvestigationClaimRecord(BaseModel):
    model_config = ConfigDict(extra='forbid')

    claim_id: str
    statement: str = Field(min_length=8)
    assessment: Literal['supported', 'refuted', 'inconclusive']
    hypothesis_ids: list[str] = Field(min_length=1)
    evidence: list[InvestigationEvidenceReference] = Field(min_length=1)
    created_at: datetime
    submitted_by: str
    note: str | None = None


class InvestigationCreateRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')

    title: str | None = None
    research_question: str = Field(min_length=12)
    research_mode: Literal['exploratory', 'confirmatory'] = 'exploratory'
    hypotheses: list[str] = Field(default_factory=list)
    priorities: list[str] = Field(default_factory=list)
    submitted_by: str | None = None

    @field_validator('research_question')
    @classmethod
    def normalize_research_question(cls, value: str) -> str:
        cleaned = ' '.join(value.split()).strip()
        if len(cleaned) < 12:
            raise ValueError('research_question must be at least 12 characters')
        return cleaned

    @field_validator('hypotheses')
    @classmethod
    def validate_unique_hypotheses(cls, value: list[str]) -> list[str]:
        if not value:
            return []
        cleaned = [' '.join(item.split()).strip() for item in value if item.strip()]
        if not cleaned:
            raise ValueError('hypotheses must not contain only blank entries')
        if any(len(item) < 8 for item in cleaned):
            raise ValueError('hypotheses must be at least 8 characters')
        deduped = list(dict.fromkeys(cleaned))
        if len(deduped) != len(cleaned):
            raise ValueError('hypotheses entries must be unique')
        return deduped

    @field_validator('priorities')
    @classmethod
    def validate_unique_priorities(cls, value: list[str]) -> list[str]:
        cleaned = [' '.join(item.split()).strip() for item in value if item.strip()]
        deduped = list(dict.fromkeys(cleaned))
        if len(deduped) != len(cleaned):
            raise ValueError('priorities entries must be unique')
        return deduped


class InvestigationHypothesisCreateRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')

    statement: str = Field(min_length=8)
    submitted_by: str | None = None

    @field_validator('statement')
    @classmethod
    def normalize_hypothesis_statement(cls, value: str) -> str:
        cleaned = ' '.join(value.split()).strip()
        if len(cleaned) < 8:
            raise ValueError('statement must be at least 8 characters')
        return cleaned


class InvestigationPlanApproveRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')

    plan_id: str = Field(min_length=1)
    approved_by: str | None = None
    note: str | None = None

    @field_validator('note')
    @classmethod
    def normalize_approval_note(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = ' '.join(value.split()).strip()
        return cleaned or None


class InvestigationPlanCreateRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')

    title: str = Field(min_length=3)
    rationale: str = Field(min_length=8)
    hypothesis_ids: list[str] = Field(min_length=1)
    executions: list[InvestigationExecutionSpec] = Field(min_length=1)
    submitted_by: str | None = None

    @model_validator(mode='after')
    def validate_execution_graph(self) -> 'InvestigationPlanCreateRequest':
        InvestigationPlanRecord(
            plan_id='validation',
            revision=1,
            created_at=datetime.min,
            submitted_by=self.submitted_by or 'validation',
            title=self.title,
            rationale=self.rationale,
            hypothesis_ids=self.hypothesis_ids,
            executions=self.executions,
        )
        return self

    @field_validator('title', 'rationale')
    @classmethod
    def normalize_plan_text(cls, value: str) -> str:
        cleaned = ' '.join(value.split()).strip()
        if not cleaned:
            raise ValueError('plan text must not be blank')
        return cleaned

    @field_validator('hypothesis_ids')
    @classmethod
    def validate_plan_hypothesis_ids(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value if item.strip()]
        if not cleaned:
            raise ValueError('at least one hypothesis_id is required')
        if len(set(cleaned)) != len(cleaned):
            raise ValueError('hypothesis_ids entries must be unique')
        return cleaned


class InvestigationRunCreateRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')

    approval_id: str = Field(min_length=1)
    execution_id: str = Field(min_length=1)

    @field_validator('approval_id', 'execution_id')
    @classmethod
    def normalize_run_identifier(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError('approval_id must not be blank')
        return cleaned


class InvestigationEvidenceRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')

    run_id: str = Field(min_length=1)
    artifact_name: str = Field(min_length=1)

    @field_validator('run_id', 'artifact_name')
    @classmethod
    def normalize_evidence_identifier(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError('evidence identifiers must not be empty')
        return cleaned


class InvestigationClaimCreateRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')

    statement: str = Field(min_length=8)
    assessment: Literal['supported', 'refuted', 'inconclusive']
    hypothesis_ids: list[str] = Field(min_length=1)
    evidence: list[InvestigationEvidenceRequest] = Field(min_length=1)
    submitted_by: str | None = None
    note: str | None = None

    @field_validator('statement')
    @classmethod
    def normalize_claim_statement(cls, value: str) -> str:
        cleaned = ' '.join(value.split()).strip()
        if len(cleaned) < 8:
            raise ValueError('statement must be at least 8 characters')
        return cleaned

    @field_validator('hypothesis_ids')
    @classmethod
    def validate_unique_claim_hypotheses(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value if item.strip()]
        if not cleaned:
            raise ValueError('at least one hypothesis_id is required')
        deduped = list(dict.fromkeys(cleaned))
        if len(deduped) != len(cleaned):
            raise ValueError('hypothesis_ids entries must be unique')
        return deduped

    @field_validator('note')
    @classmethod
    def normalize_claim_note(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = ' '.join(value.split()).strip()
        return cleaned or None


class InvestigationRecord(BaseModel):
    model_config = ConfigDict(extra='forbid')

    investigation_id: str
    created_at: datetime
    updated_at: datetime
    status: Literal['planning', 'approved', 'running', 'evaluating', 'completed', 'paused']
    title: str
    research_question: str
    research_mode: Literal['exploratory', 'confirmatory']
    priorities: list[str] = Field(default_factory=list)
    hypotheses: list[InvestigationHypothesisRecord] = Field(default_factory=list)
    plans: list[InvestigationPlanRecord] = Field(default_factory=list)
    plan_approvals: list[InvestigationPlanApprovalRecord] = Field(default_factory=list)
    active_plan_approval_id: str | None = None
    run_ids: list[str] = Field(default_factory=list)
    claims: list[InvestigationClaimRecord] = Field(default_factory=list)
    submitted_by: str


class InvestigationRunResponse(BaseModel):
    model_config = ConfigDict(extra='forbid')

    investigation: InvestigationRecord
    approval: InvestigationPlanApprovalRecord
    plan: InvestigationPlanRecord
    execution: InvestigationExecutionSpec
    run: RunRecord


class InvestigationContextResponse(BaseModel):
    model_config = ConfigDict(extra='forbid')

    investigation: InvestigationRecord
    current_plan: InvestigationPlanRecord | None = None
    approved_plan: InvestigationPlanRecord | None = None
    runs: list[RunRecord] = Field(default_factory=list)


class PaperPipelineReportState(BaseModel):
    model_config = ConfigDict(extra='forbid')

    run_id: str | None = None
    run_status: str
    terminal: bool
    report_available: bool
    report_path: str | None = None
    artifact_count: int = 0
    artifact_names: list[str] = Field(default_factory=list)


class ResearchProblemPaperCandidate(BaseModel):
    model_config = ConfigDict(extra='forbid')

    paper_id: str
    title: str
    year: int
    venue: str
    venue_id: str | None = None
    priority: str
    tracks: list[str] = Field(default_factory=list)
    bounded_job_fit: int
    replication_complexity: int
    official_page: str | None = None
    pdf_url: str | None = None
    abstract_excerpt: str | None = None
    why_seed: str
    first_jobs: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    match_score: int = 0
    match_reasons: list[str] = Field(default_factory=list)


class PaperIntakeCandidateRecord(ResearchProblemPaperCandidate):
    model_config = ConfigDict(extra='forbid')

    intake_status: Literal['pending', 'staged'] = 'pending'
    staged_intake_id: str | None = None


class PaperIntakeQueueRecord(BaseModel):
    model_config = ConfigDict(extra='forbid')

    queue_id: str
    created_at: datetime
    updated_at: datetime
    status: Literal['ready', 'exhausted'] = 'ready'
    problem_statement: str
    selected_tracks: list[str] = Field(default_factory=list)
    selected_queries: list[str] = Field(default_factory=list)
    coverage_summary: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    candidates: list[PaperIntakeCandidateRecord] = Field(default_factory=list)
    submitted_by: str
    session_id: str | None = None


class SourceDocumentRecord(BaseModel):
    model_config = ConfigDict(extra='forbid')

    document_id: str
    created_at: datetime
    updated_at: datetime
    status: Literal['fetched', 'fetch-failed']
    source_url: str
    submitted_by: str
    storage_uri: str | None = None
    content_type: str | None = None
    size_bytes: int | None = None
    sha256: str | None = None
    title: str | None = None
    text_excerpt: str | None = None
    authors: list[str] = Field(default_factory=list)
    abstract_excerpt: str | None = None
    method_hints: list[str] = Field(default_factory=list)
    dataset_hints: list[str] = Field(default_factory=list)
    loss_hints: list[str] = Field(default_factory=list)
    architecture_hints: list[str] = Field(default_factory=list)
    baseline_hints: list[str] = Field(default_factory=list)
    metric_hints: list[str] = Field(default_factory=list)
    domain_task_hints: list[str] = Field(default_factory=list)
    python_library_hints: list[str] = Field(default_factory=list)
    expected_title: str | None = None
    validation_status: Literal['unknown', 'matched', 'mismatch'] = 'unknown'
    validation_notes: list[str] = Field(default_factory=list)
    fetch_error: str | None = None
    session_id: str | None = None


class ResearchSessionContextResponse(BaseModel):
    model_config = ConfigDict(extra='forbid')

    session: ResearchSessionRecord
    research_problem: ResearchProblemRecord | None = None
    paper_intake_queue: PaperIntakeQueueRecord | None = None
    active_dataset: DatasetRecord | None = None
    datasets: list[DatasetRecord] = Field(default_factory=list)
    source_document: SourceDocumentRecord | None = None
    intake: IntakeRecord | None = None
    interpretation: InterpretationRecord | None = None
    assessment: ReplicabilityAssessmentRecord | None = None
    design: DesignDraftRecord | None = None
    run: RunRecord | None = None


class LiteratureDigestResponse(BaseModel):
    model_config = ConfigDict(extra='forbid')

    session_id: str
    source_documents: list[SourceDocumentRecord] = Field(default_factory=list)
    matched_document_count: int = 0
    mismatched_document_count: int = 0
    fetch_failed_document_count: int = 0
    top_methods: list[str] = Field(default_factory=list)
    top_datasets: list[str] = Field(default_factory=list)
    top_losses: list[str] = Field(default_factory=list)
    top_architectures: list[str] = Field(default_factory=list)
    top_baselines: list[str] = Field(default_factory=list)
    top_metrics: list[str] = Field(default_factory=list)
    top_domain_tasks: list[str] = Field(default_factory=list)
    notable_titles: list[str] = Field(default_factory=list)
    summary_notes: list[str] = Field(default_factory=list)


class ResearchSessionBootstrapStatusResponse(BaseModel):
    model_config = ConfigDict(extra='forbid')

    active_session: ResearchSessionRecord | None = None
    staged_research_problem: ResearchProblemRecord | None = None
    recommended_next_action: Literal[
        'create-session-manually',
        'create-session-from-latest-problem',
        'apply-session-skills',
    ]
    can_create_session_from_latest_problem: bool = False
    can_apply_session_skills: bool = False
    detail: str


class ResearchSessionBootstrapResponse(BaseModel):
    model_config = ConfigDict(extra='forbid')

    bootstrap_action: Literal[
        'reuse-active-session',
        'created-session-from-latest-problem',
        'create-session-manually',
    ]
    session: ResearchSessionRecord | None = None
    staged_research_problem: ResearchProblemRecord | None = None
    detail: str


class StartLiteratureSearchRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')

    goal_statement: str | None = None
    priorities: list[str] = Field(default_factory=list)
    submitted_by: str | None = None

    @field_validator('goal_statement')
    @classmethod
    def validate_optional_goal_statement(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = ' '.join(value.split()).strip()
        if not cleaned:
            raise ValueError('goal_statement must not be empty')
        if len(cleaned) < 12:
            raise ValueError('goal_statement must be at least 12 characters')
        return cleaned

    @field_validator('priorities')
    @classmethod
    def validate_unique_start_priorities(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value if item.strip()]
        deduped = list(dict.fromkeys(cleaned))
        if len(deduped) != len(cleaned):
            raise ValueError('priorities entries must be unique')
        return deduped


class StartLiteratureSearchResponse(BaseModel):
    model_config = ConfigDict(extra='forbid')

    action: str
    session: ResearchSessionRecord
    research_problem: ResearchProblemRecord
    paper_intake_queue: PaperIntakeQueueRecord
    operation: OperationRecord


class AutoresearchCampaignCreateRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')

    session_id: str | None = None
    source_design_id: str | None = None
    objective: str | None = None
    max_iterations: int = Field(default=3, ge=1, le=25)
    evaluation_policy: str = 'metrics-first-v1'
    mutation_policy: str = 'methodology-variants-v1'
    evaluator_contract: EvaluatorContract | None = None
    budget_contract: BudgetContract | None = None
    notes: list[str] = Field(default_factory=list)

    @field_validator('notes')
    @classmethod
    def validate_unique_campaign_notes(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value if item.strip()]
        return list(dict.fromkeys(cleaned))


class AutoresearchDraftMethodologiesResponse(BaseModel):
    model_config = ConfigDict(extra='forbid')

    campaign: AutoresearchCampaignRecord
    methodology_drafts: list[MethodologyDraftRecord] = Field(default_factory=list)
    operation: OperationRecord


class AutoresearchLaunchIterationResponse(BaseModel):
    model_config = ConfigDict(extra='forbid')

    campaign: AutoresearchCampaignRecord
    methodology_draft: MethodologyDraftRecord
    iteration: AutoresearchIterationRecord
    run: RunRecord
    operation: OperationRecord


class AutoresearchLaunchBatchItem(BaseModel):
    model_config = ConfigDict(extra='forbid')

    methodology_draft: MethodologyDraftRecord
    iteration: AutoresearchIterationRecord
    run: RunRecord


class AutoresearchLaunchBatchResponse(BaseModel):
    model_config = ConfigDict(extra='forbid')

    campaign: AutoresearchCampaignRecord
    launches: list[AutoresearchLaunchBatchItem] = Field(default_factory=list)
    operation: OperationRecord


class AutoresearchDecisionResponse(BaseModel):
    model_config = ConfigDict(extra='forbid')

    campaign: AutoresearchCampaignRecord
    iteration: AutoresearchIterationRecord
    decision: AutoresearchDecisionRecord
    operation: OperationRecord


class AutoresearchDecisionBatchResponse(BaseModel):
    model_config = ConfigDict(extra='forbid')

    campaign: AutoresearchCampaignRecord
    decisions: list[AutoresearchDecisionRecord] = Field(default_factory=list)
    iterations: list[AutoresearchIterationRecord] = Field(default_factory=list)
    operation: OperationRecord


class AutoresearchSuggestedMutation(BaseModel):
    model_config = ConfigDict(extra='forbid')

    source_component: str
    mutation_axis: str
    summary: str
    suggested_updates: dict[str, Any] = Field(default_factory=dict)


class AutoresearchCampaignSummaryResponse(BaseModel):
    model_config = ConfigDict(extra='forbid')

    campaign: AutoresearchCampaignRecord
    methodology_drafts: list[MethodologyDraftRecord] = Field(default_factory=list)
    iterations: list[AutoresearchIterationRecord] = Field(default_factory=list)
    decisions: list[AutoresearchDecisionRecord] = Field(default_factory=list)
    best_methodology_draft: MethodologyDraftRecord | None = None
    latest_run: RunRecord | None = None
    recommended_model: str | None = None
    model_comparison: list[dict[str, Any]] = Field(default_factory=list)
    proposed_next_variants: list[str] = Field(default_factory=list)
    proposed_next_mutations: list[AutoresearchSuggestedMutation] = Field(default_factory=list)


class AutoresearchNotebookDraftResponse(BaseModel):
    model_config = ConfigDict(extra='forbid')

    campaign: AutoresearchCampaignRecord
    methodology_draft: MethodologyDraftRecord
    created_at: datetime
    storage_uri: str
    notebook: dict[str, Any]
    refinement_source: Literal['deterministic', 'coding-model'] = 'deterministic'
    warnings: list[str] = Field(default_factory=list)


class FreshPaperPipelineResponse(BaseModel):
    model_config = ConfigDict(extra='forbid')

    intake: IntakeRecord
    interpretation: InterpretationRecord
    assessment: ReplicabilityAssessmentRecord
    design: DesignDraftRecord
    run: RunRecord | None = None
    report_state: PaperPipelineReportState
    warnings: list[str] = Field(default_factory=list)
    next_action: str


class ResearchProblemPipelineResponse(BaseModel):
    model_config = ConfigDict(extra='forbid')

    problem_statement: str
    selected_tracks: list[str] = Field(default_factory=list)
    selected_queries: list[str] = Field(default_factory=list)
    selected_papers: list[ResearchProblemPaperCandidate] = Field(default_factory=list)
    chosen_paper_id: str | None = None
    pipeline: FreshPaperPipelineResponse | None = None
    warnings: list[str] = Field(default_factory=list)
    next_action: str


class LogEntry(BaseModel):
    model_config = ConfigDict(extra='forbid')

    timestamp: datetime
    level: str
    message: str
    payload: dict[str, Any] | None = None


class RunArtifactsResponse(BaseModel):
    model_config = ConfigDict(extra='forbid')

    run_id: str
    artifacts: ArtifactsIndex


class RunLogsResponse(BaseModel):
    model_config = ConfigDict(extra='forbid')

    run_id: str
    logs: list[LogEntry]


class ResearchSessionRunCommandResponse(BaseModel):
    model_config = ConfigDict(extra='forbid')

    session: ResearchSessionRecord
    design: DesignDraftRecord
    run: RunRecord


class ResearchSessionNextCommandResponse(BaseModel):
    model_config = ConfigDict(extra='forbid')

    session: ResearchSessionRecord
    campaign: AutoresearchCampaignRecord
    draft: AutoresearchDraftMethodologiesResponse | None = None
    decide: AutoresearchDecisionBatchResponse | None = None
    launch: AutoresearchLaunchBatchResponse
    drafted_methodology_count: int = 0
    decisions_recorded: int = 0
    launches_started: int = 0


class PromotePaperToIntakeRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')

    queue_id: str
    paper_id: str
    submitted_by: str | None = None


class PromotePaperToIntakeResponse(BaseModel):
    model_config = ConfigDict(extra='forbid')

    intake_id: str
    intake_status: str
    summary: str


class CreateInterpretationRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')

    intake_id: str
    submitted_by: str | None = None


class CreateInterpretationResponse(BaseModel):
    model_config = ConfigDict(extra='forbid')

    interpretation_id: str
    status: str
    recommended_workflow_id: str | None = None


class CreateMethodologyDraftRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')

    interpretation_id: str
    submitted_by: str | None = None


class CreateMethodologyDraftResponse(BaseModel):
    model_config = ConfigDict(extra='forbid')

    design_id: str
    status: str
    workflow_id: str


class CreateValidationRunRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')

    design_id: str
    submitted_by: str | None = None


class CreateValidationRunResponse(BaseModel):
    model_config = ConfigDict(extra='forbid')

    run_id: str
    run_status: str
    workflow_id: str
