"""Pydantic models shared across the research orchestrator.

All models forbid unknown fields so a misbehaving agent or API client cannot
smuggle unexpected keys into a record. Validators here normalize and reject
unsafe model input before any deterministic code executes it; they are the
first line of defense between agent prose and policy-owned settings.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Annotated, Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class RunState(StrEnum):
    CREATED = 'CREATED'
    PREPARING = 'PREPARING'
    HONEYDEW_DRAFTING_PROTOCOL = 'HONEYDEW_DRAFTING_PROTOCOL'
    AWAITING_PROTOCOL_APPROVAL = 'AWAITING_PROTOCOL_APPROVAL'
    BEAKER_DRAFTING_CONTRACT = 'BEAKER_DRAFTING_CONTRACT'
    HONEYDEW_REVIEWING_CONTRACT = 'HONEYDEW_REVIEWING_CONTRACT'
    AWAITING_CONTRACT_PROMOTION = 'AWAITING_CONTRACT_PROMOTION'
    BEAKER_PLANNING = 'BEAKER_PLANNING'
    BEAKER_IMPLEMENTING = 'BEAKER_IMPLEMENTING'
    BEAKER_FINALIZING = 'BEAKER_FINALIZING'
    HONEYDEW_REVIEWING = 'HONEYDEW_REVIEWING'
    BEAKER_REVISING = 'BEAKER_REVISING'
    AWAITING_EXECUTION_APPROVAL = 'AWAITING_EXECUTION_APPROVAL'
    JOB_QUEUED = 'JOB_QUEUED'
    JOB_RUNNING = 'JOB_RUNNING'
    BEAKER_ANALYZING = 'BEAKER_ANALYZING'
    HONEYDEW_VERIFYING = 'HONEYDEW_VERIFYING'
    HONEYDEW_WRITING_REPORT = 'HONEYDEW_WRITING_REPORT'
    AWAITING_FINAL_ACCEPTANCE = 'AWAITING_FINAL_ACCEPTANCE'
    COMPLETE = 'COMPLETE'
    PAUSED = 'PAUSED'
    FAILED = 'FAILED'
    CANCELLED = 'CANCELLED'
    TIMED_OUT = 'TIMED_OUT'


TERMINAL_STATES = {
    RunState.COMPLETE,
    RunState.FAILED,
    RunState.CANCELLED,
    RunState.TIMED_OUT,
}


class AgentName(StrEnum):
    HONEYDEW = 'honeydew'
    BEAKER = 'beaker'
    ORCHESTRATOR = 'orchestrator'


class TurnKind(StrEnum):
    TASK_SPEC = 'task_spec'
    PROTOCOL_DRAFT = 'protocol_draft'
    CONTRACT_CANDIDATE = 'contract_candidate'
    IMPLEMENTATION_PLAN = 'implementation_plan'
    IMPLEMENTATION_PROPOSAL = 'implementation_proposal'
    METHODOLOGY_REVIEW = 'methodology_review'
    REVISION = 'revision'
    EXPERIMENT_ANALYSIS = 'experiment_analysis'
    VERIFICATION = 'verification'
    FINAL_REPORT = 'final_report'


class Claim(BaseModel):
    model_config = ConfigDict(extra='forbid')

    text: str = Field(min_length=1)
    evidence: list[
        Annotated[
            str,
            Field(
                pattern=r'^(artifact|git|event|job|contract|knowledge)://.+$',
            ),
        ]
    ] = Field(default_factory=list)

    @field_validator('evidence')
    @classmethod
    def validate_evidence_uris(cls, value: list[str]) -> list[str]:
        # The scheme allowlist mirrors the Field pattern (defense in depth)
        # and dedups URIs so a claim never cites the same evidence twice.
        allowed = (
            'artifact://',
            'git://',
            'event://',
            'job://',
            'contract://',
            'knowledge://',
        )
        for uri in value:
            if not uri.startswith(allowed):
                raise ValueError(f'unsupported evidence URI: {uri}')
        return list(dict.fromkeys(value))


class RequestedAction(BaseModel):
    model_config = ConfigDict(extra='forbid')

    type: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)
    reason: str = Field(min_length=1)


class ProposedMetric(BaseModel):
    model_config = ConfigDict(extra='forbid')

    name: str = Field(min_length=1)
    direction: Literal['maximize', 'minimize']
    minimum_effect: float = Field(default=0.0, ge=0.0)


class ProposedGuardrail(BaseModel):
    model_config = ConfigDict(extra='forbid')

    name: str = Field(min_length=1)
    direction: Literal['maximize', 'minimize']
    minimum: float | None = None
    maximum: float | None = None
    required: bool = False


class ProposedResourceConstraints(BaseModel):
    model_config = ConfigDict(extra='forbid')

    cpu: float = Field(gt=0)
    memory_gib: float = Field(gt=0)
    gpus: int = Field(ge=0)
    wallclock_minutes: int = Field(ge=1)


class EvaluationContractProposal(BaseModel):
    """Scientific contract proposed by Honeydew, never executable model code."""

    model_config = ConfigDict(extra='forbid')

    evaluator_type: str = Field(
        min_length=1,
        pattern=r'^[a-z0-9][a-z0-9._-]{0,127}$',
    )
    primary_metric: ProposedMetric
    guardrails: list[ProposedGuardrail] = Field(default_factory=list)
    required_artifacts: list[str] = Field(min_length=1)
    budget_mode: Literal[
        'wallclock',
        'training_exposure',
        'fixed_steps',
    ]
    max_wallclock_minutes: int | None = Field(default=None, ge=1)
    max_samples_seen: int | None = Field(default=None, ge=1)
    max_optimizer_steps: int | None = Field(default=None, ge=1)
    resource_constraints: ProposedResourceConstraints
    rationale: str = Field(min_length=1)

    @model_validator(mode='after')
    def require_budget_limit(self) -> 'EvaluationContractProposal':
        # Every budget mode must carry its matching numeric limit so a contract
        # can never be approved as unbounded.
        limits = {
            'wallclock': self.max_wallclock_minutes,
            'training_exposure': self.max_samples_seen,
            'fixed_steps': self.max_optimizer_steps,
        }
        if limits[self.budget_mode] is None:
            raise ValueError(
                f'{self.budget_mode} proposal requires its matching limit'
            )
        return self


class ProducedFile(BaseModel):
    model_config = ConfigDict(extra='forbid')

    path: str = Field(
        min_length=1,
        pattern=(
            r'^[A-Za-z0-9_-][A-Za-z0-9._-]*'
            r'(?:/[A-Za-z0-9_-][A-Za-z0-9._-]*)*$'
        ),
    )
    purpose: Literal['protocol', 'implementation', 'analysis', 'report', 'other']

    @field_validator('path')
    @classmethod
    def validate_relative_path(cls, value: str) -> str:
        # Backslashes are normalized to '/' so a Windows-style '..\\..' cannot
        # sneak past the path-safety checks, and all stored paths stay POSIX.
        normalized = value.strip().replace('\\', '/')
        parts = normalized.split('/')
        if (
            not normalized
            or normalized.startswith('/')
            or '..' in parts
            or any(not part for part in parts)
        ):
            raise ValueError('produced file must be a safe relative path')
        return normalized


class TaskAssetProposal(BaseModel):
    model_config = ConfigDict(extra='forbid')

    name: str = Field(pattern=r'^[a-z0-9][a-z0-9_-]{0,62}$')
    role: str = Field(min_length=1, max_length=120)
    source_url: str | None = None
    approved_uri: str | None = Field(
        default=None,
        pattern=r'^glasslab-dataset://[a-f0-9]{64}$',
    )
    expected_sha256: str | None = Field(
        default=None,
        pattern=r'^[a-f0-9]{64}$',
        description=(
            'Only set if a verified SHA-256 checksum is already known for '
            'this asset. Omit (leave null) for source_url assets, '
            'multi-file or sharded datasets, or any case where no single '
            'verified checksum exists. Do not invent or compute a value.'
        ),
    )
    contains_labels: bool = False

    @model_validator(mode='after')
    def validate_asset_source(self) -> 'TaskAssetProposal':
        # An asset is either fetched from a network URL or referenced from an
        # already-ingested approved dataset; both sources would create two
        # authoritative copies of the same bytes, which is never allowed.
        if self.source_url and self.approved_uri:
            raise ValueError(
                'asset proposal cannot use both source_url and approved_uri'
            )
        return self


class TaskSpecProposal(BaseModel):
    """Model interpretation compiled into policy-owned execution settings."""

    model_config = ConfigDict(extra='forbid')

    schema_version: Literal['glasslab-task-spec-v1']
    display_name: str = Field(min_length=3, max_length=160)
    runtime_profile: Literal['cpu-ml-standard-v1', 'gpu-ml-standard-v1']
    assets: list[TaskAssetProposal] = Field(default_factory=list)
    required_artifacts: list[str] = Field(default_factory=list)
    required_metric_keys: list[
        Annotated[str, Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$')]
    ] = Field(default_factory=list)
    missing_inputs: list[str] = Field(default_factory=list)
    rationale: str = Field(min_length=1)

    @field_validator('required_artifacts')
    @classmethod
    def safe_artifact_paths(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        for item in value:
            path = item.strip().replace('\\', '/')
            parts = path.rstrip('/').split('/')
            if (
                not path
                or path.startswith('/')
                or '..' in parts
                or any(not part for part in parts)
            ):
                raise ValueError(f'unsafe required artifact path: {item}')
            normalized.append(path)
        return list(dict.fromkeys(normalized))


class AgentTurnResult(BaseModel):
    model_config = ConfigDict(extra='forbid')

    kind: TurnKind
    summary: str = Field(min_length=1)
    task_spec_proposal: TaskSpecProposal | None = None
    evaluation_contract_proposal: EvaluationContractProposal | None = None
    claims: list[Claim] = Field(default_factory=list)
    requested_actions: list[RequestedAction] = Field(default_factory=list)
    produced_files: list[ProducedFile] = Field(default_factory=list)
    message_to_other_agent: str = ''
    recommended_next_state: RunState | None = None
    done: bool = False


class ResourceRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')

    cpu: float = Field(default=1.0, gt=0)
    memory_gib: float = Field(default=2.0, gt=0)
    gpus: int = Field(default=0, ge=0)
    wallclock_minutes: int = Field(default=30, ge=1)


class ExperimentVariant(BaseModel):
    model_config = ConfigDict(extra='forbid')

    name: str = Field(pattern=r'^[a-z0-9][a-z0-9-]{0,62}$')
    overrides: dict[str, Any] = Field(default_factory=dict)


class ExperimentMatrix(BaseModel):
    model_config = ConfigDict(extra='forbid')

    base_config: str = Field(min_length=1)
    variants: list[ExperimentVariant] = Field(min_length=1)
    seeds: list[int] = Field(min_length=1)
    maximum_parallel_jobs: int = Field(default=1, ge=1)
    runner_image: str = Field(min_length=1)
    resources: ResourceRequest = Field(default_factory=ResourceRequest)
    required_artifacts: list[str] = Field(default_factory=list)

    @field_validator('base_config')
    @classmethod
    def safe_base_config(cls, value: str) -> str:
        # Same normalization as ProducedFile: reject absolute and '..' paths,
        # and canonicalize Windows separators before the path is stored.
        normalized = value.strip().replace('\\', '/')
        parts = normalized.split('/')
        if (
            not normalized
            or normalized.startswith('/')
            or '..' in parts
            or any(not part for part in parts)
        ):
            raise ValueError('base_config must be a safe relative path')
        return normalized

    @field_validator('seeds')
    @classmethod
    def unique_seeds(cls, value: list[int]) -> list[int]:
        if len(value) != len(set(value)):
            raise ValueError('seeds must be unique')
        return value

    @field_validator('variants')
    @classmethod
    def unique_variants(
        cls,
        value: list[ExperimentVariant],
    ) -> list[ExperimentVariant]:
        names = [item.name for item in value]
        if len(names) != len(set(names)):
            raise ValueError('variant names must be unique')
        return value


class EvaluationContractDescriptor(BaseModel):
    model_config = ConfigDict(extra='forbid')

    contract_id: str = Field(min_length=3)
    version: str = Field(min_length=1)
    manifest: dict[str, Any]
    execution_wrapper: str = Field(min_length=1)
    evaluation_entry_point: str = Field(min_length=1)
    expected_input_schema: str = Field(min_length=1)
    expected_output_schema: str = Field(min_length=1)
    required_artifacts: list[str] = Field(min_length=1)
    resource_constraints: ResourceRequest
    container_image_digest: str | None = None


class ContractCandidateRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')

    contract_id: str = Field(
        min_length=3,
        pattern=r'^[a-z0-9][a-z0-9._-]{1,126}[a-z0-9]$',
    )
    version: str = Field(
        min_length=1,
        pattern=r'^[0-9]+\.[0-9]+\.[0-9]+(?:-[a-z0-9.-]+)?$',
    )
    candidate_path: str = Field(
        min_length=1,
        pattern=(
            r'^[A-Za-z0-9_-][A-Za-z0-9._-]*'
            r'(?:/[A-Za-z0-9_-][A-Za-z0-9._-]*)*$'
        ),
    )
    rationale: str = Field(min_length=1)


class ResolvedEvaluationContract(BaseModel):
    model_config = ConfigDict(extra='forbid')

    descriptor: EvaluationContractDescriptor
    digest: str = Field(pattern=r'^[a-f0-9]{64}$')
    root_path: str


class ExpandedJobSpec(BaseModel):
    model_config = ConfigDict(extra='forbid')

    orchestrator_job_id: str
    run_id: str
    action_id: str
    variant_name: str
    seed: int
    idempotency_key: str
    base_config: str
    overrides: dict[str, Any]
    runner_image: str
    resources: ResourceRequest
    required_artifacts: list[str]
    evaluation_contract_id: str
    evaluation_contract_version: str
    evaluation_contract_digest: str
    workload_id: str | None = None
    experiment_type: str | None = None
    task_bundle: dict[str, str] | None = None
    source_bundle: dict[str, str] | None = None
    workspace_command: list[str] = Field(default_factory=list)
    dataset_contracts: list[dict[str, Any]] = Field(default_factory=list)
    dataset_bindings: dict[str, str] = Field(default_factory=dict)
    task_spec: dict[str, Any] | None = None


class RunRecord(BaseModel):
    model_config = ConfigDict(extra='forbid')

    run_id: str
    # A retry is a distinct run.  This immutable link is the API-visible
    # lineage edge; terminal parents are never reopened or rewritten.
    parent_run_id: str | None = None
    retry_checkpoint_digest: str | None = None
    # The exact Git commit used to create both isolated worktrees.  Retry
    # children are pinned here instead of resolving a moving branch name.
    workspace_base_commit: str | None = Field(
        default=None, pattern=r'^[a-f0-9]{40}$'
    )
    objective: str
    state: RunState
    protocol_path: str | None = None
    protocol_version: int = 0
    evaluation_contract_id: str
    evaluation_contract_version: str
    evaluation_contract_digest: str
    task_id: str | None = None
    task_bundle_digest: str | None = None
    task_bundle_path: str | None = None
    task_definition: dict[str, Any] | None = None
    beaker_runtime_id: str | None = None
    beaker_session_id: str | None = None
    honeydew_runtime_id: str | None = None
    honeydew_session_id: str | None = None
    beaker_workspace: str
    honeydew_workspace: str
    shared_artifacts_path: str
    reports_path: str
    current_agent: AgentName | None = None
    turn_number: int = 0
    methodology_revision_count: int = Field(default=0, ge=0)
    discord_thread_id: str | None = None
    discord_status_message_id: str | None = None
    maximum_turns: int
    maximum_runtime_seconds: int
    maximum_parallel_jobs: int
    # Active-time accounting: active_runtime_seconds is the accumulated base
    # and active_since marks an open interval (when the run is doing work, not
    # waiting on a human or paused). Consumers sum both on the fly, so the
    # stored value is only the last closed portion.
    active_runtime_seconds: float = Field(default=0.0, ge=0.0)
    active_since: datetime | None = None
    resume_state: RunState | None = None
    # Optimistic-concurrency counter incremented on every run write; writers
    # must match it (see SqliteStore.replace_run) to avoid lost updates.
    version: int = 1
    created_at: datetime
    updated_at: datetime


class TurnRecord(BaseModel):
    model_config = ConfigDict(extra='forbid')

    turn_id: str = Field(default_factory=lambda: uuid4().hex)
    run_id: str
    agent: AgentName
    opencode_session_id: str | None = None
    opencode_message_id: str | None = None
    input_event: dict[str, Any] = Field(default_factory=dict)
    structured_output: AgentTurnResult | None = None
    status: Literal['running', 'completed', 'failed', 'aborted']
    error: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class PolicyClassification(StrEnum):
    AUTOMATIC = 'automatic'
    HONEYDEW_APPROVAL = 'honeydew_approval'
    HUMAN_APPROVAL = 'human_approval'
    HONEYDEW_AND_HUMAN_APPROVAL = 'honeydew_and_human_approval'
    DENY = 'deny'


class ApprovalStatus(StrEnum):
    # Ordering is significant: AUTOMATICALLY_APPROVED and PENDING are the only
    # non-terminal statuses (approvable); APPROVED means the action may be
    # executed; REJECTED/DENIED/EXECUTION_FAILED are terminal. An action moves
    # PENDING -> APPROVED exactly once (see SqliteStore.update_action).
    AUTOMATICALLY_APPROVED = 'automatically_approved'
    PENDING = 'pending'
    APPROVED = 'approved'
    REJECTED = 'rejected'
    DENIED = 'denied'
    EXECUTION_FAILED = 'execution_failed'


class ActionRecord(BaseModel):
    model_config = ConfigDict(extra='forbid')

    action_id: str = Field(default_factory=lambda: uuid4().hex)
    run_id: str
    proposed_by: AgentName
    type: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    policy_classification: PolicyClassification
    approval_status: ApprovalStatus
    # Honeydew's sign-off for a honeydew_and_human_approval action: the flag
    # records the first gate while approval_status stays PENDING until the
    # human gate also passes.
    honeydew_approved: bool = False
    honeydew_review_turn_id: str | None = None
    reviewer: str | None = None
    reason: str
    idempotency_key: str
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class JobStatus(StrEnum):
    QUEUED = 'queued'
    SUBMITTING = 'submitting'
    RUNNING = 'running'
    SUCCEEDED = 'succeeded'
    FAILED = 'failed'
    CANCELLED = 'cancelled'
    UNKNOWN = 'unknown'


JOB_TERMINAL_STATUSES = {
    JobStatus.SUCCEEDED,
    JobStatus.FAILED,
    JobStatus.CANCELLED,
}


class JobRecord(BaseModel):
    model_config = ConfigDict(extra='forbid')

    job_id: str
    run_id: str
    action_id: str
    kubernetes_namespace: str
    job_name: str | None = None
    kubernetes_uid: str | None = None
    external_run_id: str | None = None
    status: JobStatus
    requested_resources: ResourceRequest
    evaluation_contract_id: str
    evaluation_contract_version: str
    evaluation_contract_digest: str
    idempotency_key: str
    variant_name: str
    seed: int
    spec: ExpandedJobSpec
    exit_information: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class ArtifactRecord(BaseModel):
    model_config = ConfigDict(extra='forbid')

    artifact_id: str = Field(default_factory=lambda: uuid4().hex)
    run_id: str
    job_id: str | None = None
    type: str
    uri: str
    sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


class IngestedDatasetRecord(BaseModel):
    model_config = ConfigDict(extra='forbid')

    schema_version: Literal['glasslab-ingested-dataset-v1'] = (
        'glasslab-ingested-dataset-v1'
    )
    dataset_id: str = Field(pattern=r'^[a-f0-9]{64}$')
    name: str = Field(pattern=r'^[a-z0-9][a-z0-9_-]{0,62}$')
    filename: str = Field(min_length=1, max_length=255)
    reference_uri: str = Field(
        pattern=r'^glasslab-dataset://[a-f0-9]{64}$'
    )
    artifact_uri: str = Field(pattern=r'^s3://artifacts/.+$')
    path: str
    sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    size_bytes: int = Field(gt=0)
    media_type: str | None = Field(default=None, max_length=255)
    role: str = Field(min_length=1, max_length=120)
    contains_labels: bool = False
    uploaded_by: str | None = Field(default=None, max_length=255)
    created_at: datetime = Field(default_factory=utc_now)


class EventRecord(BaseModel):
    model_config = ConfigDict(extra='forbid')

    event_id: str = Field(default_factory=lambda: uuid4().hex)
    sequence_number: int
    run_id: str
    # Free-form string, not an AgentName: events may come from the
    # orchestrator, an agent runtime, or external adapters, and the set is
    # not closed by an enum.
    source: str
    event_type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=utc_now)


class SourceType(StrEnum):
    # Knowledge source taxonomy. The split between Honeydew's and Beaker's
    # sets below is the retrieval access-control boundary. Honeydew always
    # receives the full set; Beaker is excluded from PAPER. Planned
    # methodology and verified-result types are not yet wired into the enum.
    DOCUMENTATION = 'documentation'
    HANDOFF = 'handoff'
    EVALUATION_CONTRACT = 'evaluation_contract'
    TASK_BUNDLE = 'task_bundle'
    DATASET_METADATA = 'dataset_metadata'
    PAPER = 'paper'
    TECHNIQUE_CARD = 'technique_card'
    RUN_PROTOCOL = 'run_protocol'
    RUN_REPORT = 'run_report'
    RUN_ARTIFACT = 'run_artifact'
    IMPLEMENTATION_FILE = 'implementation_file'


HONEYDEW_SOURCE_TYPES = {
    SourceType.DOCUMENTATION,
    SourceType.HANDOFF,
    SourceType.EVALUATION_CONTRACT,
    SourceType.TASK_BUNDLE,
    SourceType.DATASET_METADATA,
    SourceType.PAPER,
    SourceType.TECHNIQUE_CARD,
    SourceType.RUN_PROTOCOL,
    SourceType.RUN_REPORT,
    SourceType.RUN_ARTIFACT,
}

BEAKER_SOURCE_TYPES = {
    SourceType.DOCUMENTATION,
    SourceType.HANDOFF,
    SourceType.EVALUATION_CONTRACT,
    SourceType.TASK_BUNDLE,
    SourceType.DATASET_METADATA,
    SourceType.RUN_PROTOCOL,
    SourceType.RUN_REPORT,
    SourceType.RUN_ARTIFACT,
    SourceType.IMPLEMENTATION_FILE,
}


class KnowledgeSource(BaseModel):
    """A provenance-carrying source in the knowledge index.

    Sources are ingested only from approved, allowlisted roots. The record
    never stores the source text; it points at the canonical URI and digests
    the source bytes so derived chunks stay reproducible. ``source_id`` is the
    stable surrogate used in ``knowledge://<source_id>`` evidence URIs; the
    digest enables content invalidation and dedup.
    """

    model_config = ConfigDict(extra='forbid')

    source_id: str = Field(default_factory=lambda: uuid4().hex)
    source_type: SourceType
    canonical_uri: str = Field(min_length=1)
    run_scope: str | None = None
    # run-approved sources are shareable across approved runs; run-private
    # sources are visible only to the run they were ingested under.
    access_policy: Literal['run-private', 'run-approved'] = 'run-approved'
    source_version: str | None = None
    digest: str = Field(pattern=r'^[a-f0-9]{64}$')
    ingested_at: datetime = Field(default_factory=utc_now)
    index_version: str = 'v1'
    title: str | None = Field(default=None, max_length=512)
    metadata: dict[str, Any] = Field(default_factory=dict)
    parent_source_id: str | None = None

    def evidence_uri(self) -> str:
        return f'knowledge://{self.source_id}'


class KnowledgeChunk(BaseModel):
    """A fixed-size, overlapping fragment of a source ready for FTS indexing."""

    model_config = ConfigDict(extra='forbid')

    chunk_id: str = Field(default_factory=lambda: uuid4().hex)
    source_id: str = Field(min_length=1)
    chunk_index: int = Field(ge=0)
    text: str = Field(min_length=1)
    digest: str = Field(pattern=r'^[a-f0-9]{64}$')
    token_count: int = Field(ge=1)
    index_version: str = 'v1'


class ContextPacket(BaseModel):
    """The durable, versioned context supplied to one agent turn.

    Persisted per turn so a report can cite ``knowledge://context:<packet_id>``
    as the exact retrieval that grounded a claim. ``ranked_sources`` is the
    ordered, budget-truncated list; ``exact_text_supplied`` is the literal text
    prepended to the agent prompt.
    """

    model_config = ConfigDict(extra='forbid')

    packet_id: str = Field(default_factory=lambda: uuid4().hex)
    run_id: str
    agent: AgentName
    turn_number: int = Field(ge=1)
    turn_kind: TurnKind
    query: str
    index_version: str
    ranked_sources: list[dict[str, Any]] = Field(default_factory=list)
    exact_text_supplied: str | None = None
    token_budget: int = Field(gt=0)
    # Actual retrieval outcome for this packet (defaults keep packets written
    # before dense retrieval valid to load).
    retrieval_mode_requested: str | None = None
    retrieval_mode_actual: str | None = None
    retrieval_fallback_reason: str = ''
    created_at: datetime = Field(default_factory=utc_now)

    def evidence_uri(self) -> str:
        return f'knowledge://context:{self.packet_id}'


class KnowledgeSourceRequest(BaseModel):
    """Typed request to ingest an approved source from an allowlisted root."""

    model_config = ConfigDict(extra='forbid')

    source_type: SourceType
    path: str = Field(min_length=1)
    title: str | None = Field(default=None, max_length=512)
    source_version: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ContextPacketListResponse(BaseModel):
    packets: list[ContextPacket]


class KnowledgeSourceListResponse(BaseModel):
    sources: list[KnowledgeSource]


class RunCreateRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')

    objective: str = Field(min_length=10)
    evaluation_contract_id: str | None = None
    evaluation_contract_version: str | None = None
    maximum_turns: int | None = Field(default=None, ge=1)
    maximum_runtime_seconds: int | None = Field(default=None, ge=60)
    maximum_parallel_jobs: int | None = Field(default=None, ge=1)
    task_id: str | None = Field(
        default=None,
        pattern=r'^[a-z0-9][a-z0-9-]{1,62}$',
    )
    task_bundle_digest: str | None = Field(
        default=None,
        pattern=r'^[a-f0-9]{64}$',
    )

    @model_validator(mode='after')
    def require_contract_pair(self) -> 'RunCreateRequest':
        # Contract ID and version travel together: a version-less reference
        # would make the resolved contract ambiguous.
        if bool(self.evaluation_contract_id) != bool(self.evaluation_contract_version):
            raise ValueError('evaluation contract ID and version must be supplied together')
        return self


class TerminalRetryRequest(BaseModel):
    """Optional caller key for an auditable, idempotent terminal retry."""

    model_config = ConfigDict(extra='forbid')

    idempotency_key: str | None = Field(default=None, min_length=8, max_length=256)


class ApprovalRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')

    reviewer: str = Field(min_length=1)
    reason: str = Field(default='Approved by human reviewer.', min_length=1)


class RejectionRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')

    reviewer: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class RunListResponse(BaseModel):
    runs: list[RunRecord]


class EventListResponse(BaseModel):
    events: list[EventRecord]


class ArtifactListResponse(BaseModel):
    artifacts: list[ArtifactRecord]


class TurnSummary(BaseModel):
    """Redacted, read-only projection of a TurnRecord.

    Returned by GET /runs/{run_id}/turns and the /research-turns Discord
    command (see turn_inspection.py). ``input``/``output`` are the turn's
    input_event and structured_output after app.redaction.redact_payload has
    scrubbed credential-shaped content; this is a convenience view over the
    persisted TurnRecord, not a new source of truth — the normalized event
    log remains authoritative.
    """

    model_config = ConfigDict(extra='forbid')

    turn_id: str
    run_id: str
    agent: AgentName
    status: Literal['running', 'completed', 'failed', 'aborted']
    error: str | None = None
    input: dict[str, Any] = Field(default_factory=dict)
    output: dict[str, Any] | None = None
    started_at: datetime
    ended_at: datetime | None = None


class TurnListResponse(BaseModel):
    turns: list[TurnSummary]
