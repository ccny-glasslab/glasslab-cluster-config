"""Request and response contracts between the schedule worker and workflow API.

All models use extra='forbid' so an unknown field from the workflow API fails
validation instead of silently round-tripping. ScheduledExecutionPayload is
the shared execution record for both digest and approved-rerun cycles.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class WorkerConfigMetadata(BaseModel):
    model_config = ConfigDict(extra='forbid')

    workflow_api_url: str
    timeout_seconds: float


class ScheduledExecutionPayload(BaseModel):
    model_config = ConfigDict(extra='forbid')

    execution_id: str
    schedule_id: str
    operation_type: str
    result_status: str
    result_detail: str
    digest_payload: dict[str, Any] = Field(default_factory=dict)


class RunOnceResponse(BaseModel):
    model_config = ConfigDict(extra='forbid')

    worker_status: str
    executed_count: int
    executions: list[ScheduledExecutionPayload] = Field(default_factory=list)
    worker_config: WorkerConfigMetadata
    errors: list[str] = Field(default_factory=list)


class HealthResponse(BaseModel):
    model_config = ConfigDict(extra='forbid')

    status: str
    worker_config: WorkerConfigMetadata
