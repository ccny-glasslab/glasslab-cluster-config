"""Settings layer for the workflow-api service.

All runtime configuration is read from environment variables prefixed
GLASSLAB_WORKFLOW_API_ or a .env file. The single Settings instance is cached
via lru_cache so repeated calls to get_settings() return the same object.
Settings owns the contract for store backends, agent endpoints, Kubernetes
job templates, and evaluation-contract resolution.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .auth import CallerPolicy
from .paths import discover_repo_root

DEFAULT_REGISTRY_DIR = discover_repo_root() / 'services' / 'workflow-registry' / 'definitions'


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file='.env',
        env_prefix='GLASSLAB_WORKFLOW_API_',
        case_sensitive=False,
        extra='ignore',
    )

    app_name: str = 'glasslab-workflow-api'
    app_version: str = '0.1.0'
    build_source_revision: str = 'unknown'
    build_source_label: str = 'unspecified'
    log_level: str = 'INFO'
    registry_dir: str = str(DEFAULT_REGISTRY_DIR)
    store_backend: Literal['memory', 'json', 'postgres'] = 'memory'
    allow_inmemory_store: bool = True
    store_json_path: str = '/mnt/artifacts/workflow-api/state/run-store.json'
    store_postgres_dsn: str | None = None
    runner_namespace: str = 'glasslab-v2'
    default_submitted_by: str = 'glasslab-operator'
    job_submission_mode: Literal['null', 'kubernetes'] = 'null'
    runner_service_account_name: str = 'default'
    runner_image_pull_policy: str = 'IfNotPresent'
    runner_backoff_limit: int = 0
    runner_job_ttl_seconds: int = 86400
    dataset_pvc_name: str = 'glasslab-shared-datasets'
    dataset_mount_path: str = '/mnt/datasets'
    dataset_bucket: str = 'glasslab-datasets'
    artifacts_pvc_name: str = 'glasslab-shared-artifacts'
    artifacts_mount_path: str = '/mnt/artifacts'
    image_pull_secret_name: str = 'glasslab-ghcr-pull'
    gpu_runtime_class_name: str = 'nvidia'
    evaluation_contracts: dict[str, dict[str, str]] = Field(default_factory=dict)
    evaluation_contract_catalog_path: str | None = None
    evaluation_contract_bundle_root: str = '/mnt/artifacts'
    user_priority_class_name: str = 'glasslab-user-high'
    autonomous_priority_class_name: str = 'glasslab-autonomous-low'
    intake_agent_enabled: bool = False
    intake_agent_url: str = 'http://glasslab-intake-agent.glasslab-v2.svc.cluster.local:8090'
    intake_agent_timeout_seconds: float = 30.0
    interpretation_agent_enabled: bool = False
    interpretation_agent_url: str = 'http://glasslab-interpretation-agent.glasslab-v2.svc.cluster.local:8091'
    interpretation_agent_timeout_seconds: float = 90.0
    assessment_agent_enabled: bool = False
    assessment_agent_url: str = 'http://glasslab-assessment-agent.glasslab-v2.svc.cluster.local:8092'
    assessment_agent_timeout_seconds: float = 45.0
    design_agent_enabled: bool = False
    design_agent_url: str = 'http://glasslab-design-agent.glasslab-v2.svc.cluster.local:8093'
    design_agent_timeout_seconds: float = 45.0
    coding_notebook_agent_enabled: bool = False
    coding_notebook_agent_url: str = 'http://192.168.1.12:11434/api/chat'
    coding_notebook_agent_timeout_seconds: float = 90.0
    coding_notebook_model: str = 'qwen2.5-coder:14b'
    ranker_enabled: bool = False
    ranker_url: str = 'http://192.168.1.12:8181/rank/workflow-family'
    ranker_timeout_seconds: float = 15.0
    ranker_min_top_score: float = 0.75
    ranker_min_score_gap: float = 0.10
    source_document_storage_mode: Literal['filesystem', 'minio'] = 'filesystem'
    source_document_storage_dir: str = '/mnt/artifacts/source-documents'
    source_document_bucket: str = 'research-sources'
    minio_endpoint: str = 'glasslab-minio.glasslab-v2.svc.cluster.local:9000'
    minio_access_key: str | None = None
    minio_secret_key: str | None = None
    minio_secure: bool = False
    external_literature_enabled: bool = True
    external_literature_openalex_url: str = 'https://api.openalex.org/works'
    external_literature_crossref_url: str = 'https://api.crossref.org/works'
    external_literature_arxiv_url: str = 'https://export.arxiv.org/api/query'
    external_literature_dblp_url: str = 'https://dblp.org/search/publ/api'
    external_literature_timeout_seconds: float = 20.0
    external_literature_mailto: str | None = None
    caller_policies: tuple[CallerPolicy, ...] = Field(default_factory=tuple)

    @model_validator(mode='after')
    def validate_store_backend(self) -> 'Settings':
        if self.store_backend == 'memory' and not self.allow_inmemory_store:
            raise ValueError(
                'workflow-api store backend is set to memory but allow_inmemory_store=false; '
                'choose a durable backend or explicitly allow in-memory mode'
            )
        if self.store_backend == 'json' and not self.store_json_path.strip():
            raise ValueError('json store backend requires a non-empty store_json_path')
        if self.store_backend == 'postgres' and not (self.store_postgres_dsn or '').strip():
            raise ValueError('postgres store backend requires a non-empty store_postgres_dsn')
        caller_names = [policy.name for policy in self.caller_policies]
        if len(caller_names) != len(set(caller_names)):
            raise ValueError('caller policy names must be unique')
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
