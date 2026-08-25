"""Application settings loaded from the GLASSLAB_ORCHESTRATOR_ env prefix.

Kept as a single pydantic-settings model so every knob is documented in one
place. Secret-bearing fields (operator_api_token, discord_bot_token,
discord_webhook_url) must never be rendered into prompts, events, or logs;
nothing in the app logs Settings as a whole.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


SERVICE_ROOT = Path(__file__).resolve().parents[1]

# The minimal satisfiable evidence snapshot is the empty jobs/artifacts/
# artifact_contents skeleton plus a count-only truncation note, which
# serializes to ~251 bytes. A cap below this floor could never be honored, so
# Settings rejects it at construction. 1024 bytes keeps a documented 4x margin
# over the measured floor while still allowing tight test caps.
EVIDENCE_SNAPSHOT_MIN_BYTES = 1024


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file='.env',
        env_prefix='GLASSLAB_ORCHESTRATOR_',
        case_sensitive=False,
        extra='ignore',
    )

    app_name: str = 'glasslab-research-orchestrator'
    app_version: str = '0.1.0'
    store_backend: Literal['sqlite', 'postgres'] = 'sqlite'
    store_postgres_dsn: str | None = None
    database_path: str = '/tmp/glasslab-research-orchestrator/orchestrator.db'
    workspace_root: str = '/tmp/glasslab-research-orchestrator/runs'
    artifact_root: str = '/tmp/glasslab-research-orchestrator/artifacts'
    # How long a terminal (FAILED/CANCELLED/TIMED_OUT/COMPLETE) run's
    # per-run runtime/worktree storage is kept before scripts/
    # cleanup-run-storage.py is eligible to remove it. See
    # app/storage_retention.py; this never affects protocol/reports/
    # shared-artifacts, only agent process scratch space.
    terminal_run_retention_days: int = 14
    # Knowledge store: chunk sizing and the per-turn retrieval budget bound
    # context cost regardless of model or content; the allowlist roots are the
    # only places ingestion may read from (in production these are the mounted
    # cluster-config docs and evaluation-contract directories).
    knowledge_root: str = '/tmp/glasslab-research-orchestrator/knowledge'
    knowledge_chunk_size: int = 1500
    knowledge_chunk_overlap: int = 150
    knowledge_max_source_bytes: int = 2 * 1024 * 1024
    knowledge_max_results: int = 10
    knowledge_token_budget: int = 4000
    knowledge_allowlist_roots: Annotated[list[str], NoDecode] = [
        '/workspace/cluster-config/docs',
        '/workspace/cluster-config/services/research-orchestrator/evaluation-contracts',
    ]
    evidence_excerpt_max_bytes: int = 32 * 1024
    # Verbatim tier for evaluator failures/metrics (larger than the general
    # excerpt cap); the whole snapshot is bounded by the snapshot cap.
    evidence_verbatim_max_bytes: int = 64 * 1024
    evidence_snapshot_max_bytes: int = 512 * 1024
    approved_repo_path: str = '/workspace/cluster-config'
    approved_repo_ref: str = 'main'
    evaluation_contract_root: str = str(SERVICE_ROOT / 'evaluation-contracts')
    promoted_contract_root: str = (
        '/tmp/glasslab-research-orchestrator/trusted-contracts'
    )
    sealed_contract_candidate_root: str = (
        '/tmp/glasslab-research-orchestrator/contract-candidates'
    )
    trusted_contract_catalog_path: str = (
        '/tmp/glasslab-research-orchestrator/trusted-contracts/catalog.json'
    )
    shared_mount_root: str = '/tmp/glasslab-research-orchestrator'
    task_bundle_root: str = '/tmp/glasslab-research-orchestrator/task-bundles'
    task_asset_root: str = '/tmp/glasslab-research-orchestrator/task-assets'
    maximum_task_asset_bytes: int = 2 * 1024 * 1024 * 1024
    dataset_upload_root: str = (
        '/tmp/glasslab-research-orchestrator/dataset-uploads'
    )
    maximum_dataset_upload_bytes: int = 2 * 1024 * 1024 * 1024
    maximum_discord_dataset_upload_bytes: int = 100 * 1024 * 1024
    maximum_discord_artifact_bundle_bytes: int = 24 * 1024 * 1024
    benchmark_dataset_catalog_path: str = (
        '/tmp/glasslab-research-orchestrator/datasets/catalog.json'
    )
    default_evaluation_contract_id: str = 'example-research-v1'
    default_evaluation_contract_version: str = '1.0.0'

    agent_runtime_backend: Literal['opencode', 'hermes'] = 'opencode'
    opencode_executable: str = '/usr/local/bin/opencode'
    opencode_server_host: str = '127.0.0.1'
    opencode_start_port: int = 4210
    opencode_start_timeout_seconds: float = 15.0
    opencode_turn_timeout_seconds: float = 1800.0
    opencode_repeated_tool_limit: int = 6
    opencode_structured_repair_attempts: int = 1
    opencode_structured_output_mode: Literal['json_schema', 'prompt'] = (
        'json_schema'
    )
    # OpenCode's package/model download cache (XDG_CACHE_HOME) is shared
    # across every run and both agents instead of copied per run: it is
    # non-essential, regenerable data (the same OpenCode version and plugin
    # set for everyone), unlike XDG_DATA_HOME (session auth) or
    # XDG_STATE_HOME (logs/history), which stay isolated per run. See
    # opencode_runtime.py's _write_runtime_config.
    opencode_shared_cache_root: str = (
        '/tmp/glasslab-research-orchestrator/opencode-cache'
    )
    agent_model_provider_id: str = 'exo'
    agent_model_name: str | None = None
    qwen_base_url: str = 'http://192.168.1.17:52415/v1'
    qwen_model_name: str = 'mlx-community/Qwen3-Coder-Next-4bit'
    opencode_runtime_image: str = (
        'ghcr.io/ccny-glasslab/glasslab-research-orchestrator:0.1.0'
    )
    hermes_executable: str = '/usr/local/bin/hermes'
    hermes_server_host: str = '127.0.0.1'
    hermes_start_port: int = 4310
    hermes_start_timeout_seconds: float = 30.0
    hermes_http_timeout_seconds: float = 30.0
    hermes_turn_timeout_seconds: float = 1800.0
    hermes_poll_interval_seconds: float = 1.0
    hermes_command_timeout_seconds: int = 180
    hermes_max_iterations: int = 60
    hermes_structured_repair_attempts: int = 1

    cluster_execution_api_url: str = (
        'http://glasslab-workflow-api.glasslab-v2.svc.cluster.local:8080'
    )
    cluster_execution_mode: str = 'workflow-api'
    cluster_execution_workload_id: str = 'workspace-cpu-ml-v1'
    cluster_execution_experiment_type: str = 'research-workspace-job'
    workflow_api_caller_name: str = Field(
        default='',
        validation_alias=AliasChoices(
            'GLASSLAB_WORKFLOW_API_CALLER_NAME',
            'workflow_api_caller_name',
        ),
    )
    workflow_api_token: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices(
            'GLASSLAB_WORKFLOW_API_TOKEN',
            'workflow_api_token',
        ),
    )
    kubernetes_namespace: str = 'glasslab-v2'
    permitted_job_images: Annotated[list[str], NoDecode] = [
        'ghcr.io/ccny-glasslab/glasslab-research-workspace-runner@sha256:'
        'dae5bc4967f5ac54edb6c6d63d8d3db9e4652cc46e035118b0c456eb70121061',
    ]

    maximum_turns: int = 20
    maximum_methodology_revisions: int = 2
    maximum_runtime_seconds: int = 86400
    maximum_cpu: float = 8.0
    maximum_memory_gib: float = 32.0
    maximum_gpus: int = 1
    maximum_parallel_jobs: int = 4
    one_active_run: bool = True
    job_poll_interval_seconds: float = 10.0
    require_operator_auth: bool = False
    operator_api_token: str | None = None

    discord_enabled: bool = False
    discord_bot_token: str | None = None
    discord_channel_id: str | None = None
    discord_webhook_url: str | None = None
    discord_application_id: str | None = None
    discord_guild_id: str | None = None
    discord_controls_enabled: bool = False
    discord_admin_role_id: str | None = None
    discord_admin_user_ids: Annotated[list[str], NoDecode] = []
    discord_rest_circuit_max_failures: int = 3
    discord_rest_circuit_cooldown_seconds: float = 60.0
    discord_rest_probe_interval_seconds: float = 60.0

    @property
    def effective_agent_model_name(self) -> str:
        return self.agent_model_name or self.qwen_model_name

    @field_validator('evidence_snapshot_max_bytes')
    @classmethod
    def enforce_evidence_snapshot_minimum(cls, value: int) -> int:
        if value < EVIDENCE_SNAPSHOT_MIN_BYTES:
            raise ValueError(
                'evidence_snapshot_max_bytes must be at least '
                f'{EVIDENCE_SNAPSHOT_MIN_BYTES} bytes'
            )
        return value

    @field_validator('store_postgres_dsn')
    @classmethod
    def require_postgres_dsn(cls, value: str | None, info) -> str | None:
        if info.data.get('store_backend') == 'postgres' and not (value or '').strip():
            raise ValueError('postgres store backend requires a non-empty store_postgres_dsn')
        return value

    @field_validator('permitted_job_images', mode='before')
    @classmethod
    def parse_image_allowlist(cls, value: object) -> object:
        # pydantic-settings passes the raw env string for list fields; NoDecode
        # stops its JSON attempt, and this validator splits the conventional
        # comma-separated spelling instead.
        if isinstance(value, str):
            return [item.strip() for item in value.split(',') if item.strip()]
        return value

    @field_validator('discord_rest_circuit_max_failures')
    @classmethod
    def enforce_discord_rest_circuit_minimum(cls, value: int) -> int:
        if value < 1:
            raise ValueError('discord_rest_circuit_max_failures must be >= 1')
        return value

    @field_validator('discord_rest_circuit_cooldown_seconds')
    @classmethod
    def enforce_discord_rest_cooldown_positive(cls, value: float) -> float:
        if value <= 0:
            raise ValueError('discord_rest_circuit_cooldown_seconds must be > 0')
        return value

    @field_validator('discord_rest_probe_interval_seconds')
    @classmethod
    def enforce_discord_rest_probe_interval_nonnegative(cls, value: float) -> float:
        if value < 0:
            raise ValueError('discord_rest_probe_interval_seconds must be >= 0')
        return value

    @field_validator('knowledge_allowlist_roots', mode='before')
    @classmethod
    def parse_knowledge_allowlist(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(',') if item.strip()]
        return value

    @field_validator('discord_admin_user_ids', mode='before')
    @classmethod
    def parse_discord_user_allowlist(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(',') if item.strip()]
        return value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    # Single cached instance so every module that needs settings observes the
    # same env snapshot for the process lifetime.
    return Settings()
