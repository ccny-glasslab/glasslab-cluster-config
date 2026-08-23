"""Run-time record contracts: manifests, metrics, and artifact indexes.

Written by the workspace runner and read by the orchestrator and evaluator.
Every model uses extra='forbid' so an unknown field fails validation instead of
silently round-tripping between services.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

RunState = Literal['accepted', 'queued', 'running', 'succeeded', 'failed', 'rejected', 'cancelled']
MetricDirection = Literal['maximize', 'minimize']
RunPriority = Literal['user', 'autonomous']


class RunManifest(BaseModel):
    model_config = ConfigDict(extra='forbid')

    run_id: str
    workflow_id: str
    workflow_family: str
    display_name: str
    objective: str = Field(min_length=5)
    submitted_by: str
    submitted_at: datetime
    run_priority: RunPriority = 'user'
    inputs: dict[str, Any] = Field(default_factory=dict)
    requested_models: list[str] = Field(min_length=1)
    resource_profile: str
    resource_requests: dict[str, str] = Field(default_factory=dict)
    resource_limits: dict[str, str] = Field(default_factory=dict)
    node_selector: dict[str, str] = Field(default_factory=dict)
    runner_image: str
    evaluator_type: str
    approval_tier: str
    expected_artifacts: dict[str, list[str]]
    experiment_type: str | None = None
    workload_id: str | None = None
    schema_ref: str | None = None
    entrypoint: list[str] = Field(default_factory=list)
    config_payload: dict[str, Any] = Field(default_factory=dict)
    dataset_bindings: dict[str, str] = Field(default_factory=dict)
    budget: dict[str, Any] = Field(default_factory=dict)
    metric_contract: dict[str, Any] = Field(default_factory=dict)

    @field_validator('requested_models')
    @classmethod
    def validate_unique_models(cls, value: list[str]) -> list[str]:
        deduped = list(dict.fromkeys(value))
        if len(deduped) != len(value):
            raise ValueError('requested_models must be unique')
        return value

    @field_validator('entrypoint')
    @classmethod
    def validate_entrypoint(cls, value: list[str]) -> list[str]:
        cleaned = [' '.join(str(item).split()).strip() for item in value]
        return [item for item in cleaned if item]


class RunStatus(BaseModel):
    model_config = ConfigDict(extra='forbid')

    run_id: str
    status: RunState
    updated_at: datetime
    detail: str | None = None


class MetricRecord(BaseModel):
    model_config = ConfigDict(extra='forbid')

    name: str
    value: float
    direction: MetricDirection
    split: str | None = None


class Metrics(BaseModel):
    model_config = ConfigDict(extra='forbid')

    run_id: str
    primary_metric: str | None = None
    values: list[MetricRecord] = Field(default_factory=list)
    runtime_seconds: float | None = None
    notes: list[str] = Field(default_factory=list)


class ArtifactIndexEntry(BaseModel):
    model_config = ConfigDict(extra='forbid')

    name: str
    path: str
    media_type: str
    required: bool = True
    size_bytes: int | None = None
    sha256: str | None = Field(
        default=None,
        min_length=64,
        max_length=64,
        pattern=r'^[0-9a-f]{64}$',
    )
    description: str | None = None


class ArtifactsIndex(BaseModel):
    model_config = ConfigDict(extra='forbid')

    run_id: str
    artifacts: list[ArtifactIndexEntry] = Field(default_factory=list)
