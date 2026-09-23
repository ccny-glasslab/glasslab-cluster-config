"""Manifest parity guard: every tracked workflow-api ConfigMap key is audited.

Mirrors ``services/research-orchestrator/tests/test_configmap_parity.py`` using
the shared engine in ``services/common/configmap_parity.py``. The
workflow-api deployment overrides the local/test defaults (``memory`` store,
``null`` submission mode, disabled agents) with its production wiring, so each
of those deviations must carry a reviewed reason. There is no ``.env.example``
mirror for this service.

``job_submission_mode`` is deliberately allowlisted to ``kubernetes``: the code
default ``null`` is a safe no-op for local/test, while the deployed service IS
the bounded cluster-execution control plane and must submit real Kubernetes
Jobs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
CONFIGMAP_PATH = (
    REPOSITORY_ROOT
    / 'kubeadm'
    / 'glasslab-v2'
    / 'config'
    / '10-workflow-api-configmap.yaml'
)

from services.common.configmap_parity import (  # noqa: E402
    Override,
    audit,
    env_name_index,
    load_configmap_data,
    validate_overrides,
)

CONFIGMAP_DATA = load_configmap_data(CONFIGMAP_PATH)
CONFIGMAP_KEYS = tuple(sorted(CONFIGMAP_DATA))

_P = 'GLASSLAB_WORKFLOW_API_'

_CONTAINER_PATH_REASON = (
    'Container layout: the code default targets the source checkout or a local '
    'path; the deployed image ships the registry at /app and mounts artifacts '
    'under /mnt/artifacts. Pinned so an unintended relocation fails review.'
)
_AGENT_URL_REASON = (
    'Live agent endpoint: the deployment appends the service route to the '
    'in-cluster base URL so the disabled-by-default agent is reachable when '
    'enabled. Pinned so a wrong endpoint fails review.'
)

OVERRIDES: dict[str, Override] = {
    _P + 'REGISTRY_DIR': Override(
        'eq',
        _CONTAINER_PATH_REASON,
        expected='/app/services/workflow-registry/definitions',
    ),
    _P + 'STORE_BACKEND': Override(
        'eq',
        'Production run store is Postgres; memory/json are local-test defaults.',
        expected='postgres',
    ),
    _P + 'ALLOW_INMEMORY_STORE': Override(
        'eq',
        'The deployed service forbids the in-memory store so a restart cannot '
        'silently drop run state; the code default allows it for local tests.',
        expected='false',
    ),
    _P + 'JOB_SUBMISSION_MODE': Override(
        'allowlist',
        'The deployed service IS the cluster-execution control plane and must '
        "submit real Kubernetes Jobs; the code default 'null' is a safe no-op "
        'for local/test. Only kubernetes is accepted.',
        allowed=frozenset({'kubernetes'}),
    ),
    _P + 'EVALUATION_CONTRACT_CATALOG_PATH': Override(
        'eq',
        _CONTAINER_PATH_REASON,
        expected='/mnt/artifacts/research-orchestrator/trusted-contracts/catalog.json',
    ),
    _P + 'INTAKE_AGENT_URL': Override(
        'eq',
        _AGENT_URL_REASON,
        expected=(
            'http://glasslab-intake-agent.glasslab-v2.svc.cluster.local:8090/'
            'normalize-intake'
        ),
    ),
    _P + 'INTERPRETATION_AGENT_ENABLED': Override(
        'eq',
        'The interpretation agent is enabled in the live workflow; the code '
        'default keeps it off for local/test.',
        expected='true',
    ),
    _P + 'INTERPRETATION_AGENT_URL': Override(
        'eq',
        _AGENT_URL_REASON,
        expected=(
            'http://glasslab-interpretation-agent.glasslab-v2.svc.cluster.local'
            ':8091/interpret-intake'
        ),
    ),
    _P + 'ASSESSMENT_AGENT_URL': Override(
        'eq',
        _AGENT_URL_REASON,
        expected=(
            'http://glasslab-assessment-agent.glasslab-v2.svc.cluster.local:'
            '8092/assess-interpretation'
        ),
    ),
    _P + 'DESIGN_AGENT_URL': Override(
        'eq',
        _AGENT_URL_REASON,
        expected=(
            'http://glasslab-design-agent.glasslab-v2.svc.cluster.local:8093/'
            'draft-design'
        ),
    ),
    _P + 'CODING_NOTEBOOK_AGENT_ENABLED': Override(
        'eq',
        'The coding-notebook agent is enabled in the live workflow; the code '
        'default keeps it off for local/test.',
        expected='true',
    ),
    _P + 'SOURCE_DOCUMENT_BUCKET': Override(
        'eq',
        'The live object-store bucket is glasslab-source-documents; the code '
        'default research-sources is the legacy local name.',
        expected='glasslab-source-documents',
    ),
}

VIOLATIONS = audit(Settings, CONFIGMAP_DATA, OVERRIDES)


@pytest.mark.parametrize(
    'key', [pytest.param(key, id=key) for key in CONFIGMAP_KEYS]
)
def test_configmap_key_respects_default_invariant(key: str) -> None:
    assert key not in VIOLATIONS, VIOLATIONS[key]


def test_unknown_configmap_key_is_flagged() -> None:
    typo_key = 'GLASSLAB_WORKFLOW_API_JOB_SUBMISSION_MOD'
    typo = dict(CONFIGMAP_DATA, **{typo_key: 'kubernetes'})
    violations = audit(Settings, typo, OVERRIDES)
    assert typo_key in violations, (
        'a ConfigMap key with no matching Settings field must be flagged; '
        "pydantic-settings extra='ignore' does not detect it"
    )
    assert 'no Settings field' in violations[typo_key]


def test_override_table_is_well_formed() -> None:
    problems = validate_overrides(Settings, CONFIGMAP_DATA, OVERRIDES)
    assert not problems, '\n'.join(problems)


def test_env_name_resolution_contract() -> None:
    """Tripwire: the audit's upper-casing depends on private pydantic-settings.

    ``EnvSettingsSource._extract_field_info`` is private; if a future version
    stops lower-casing names under ``case_sensitive=False`` the audit's
    ``.upper()`` normalization breaks silently. This fails first.
    """
    index = env_name_index(Settings)
    key = 'GLASSLAB_WORKFLOW_API_JOB_SUBMISSION_MODE'
    assert key in index
    assert index[key][0] == 'job_submission_mode'
