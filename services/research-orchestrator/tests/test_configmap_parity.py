"""Manifest parity guard: every tracked ConfigMap key must respect its default.

A Kubernetes ConfigMap env entry overrides the pydantic-settings default, so a
stale tracked manifest silently defeats a deliberate change in
``app/config.py`` even when the code, its comment, and its unit test all agree.
The single-key guard that caught this for
``GLASSLAB_ORCHESTRATOR_OPENCODE_TURN_TIMEOUT_SECONDS`` (code default widened
1800 -> 2400 -> 3600 while the manifest stayed at 1800) is generalized here to
every key the manifest sets: each key is audited against the invariant for its
field type, and any intentional deviation must carry a reviewed reason.

The audit lives in ``services/common/configmap_parity.py`` and is shared with
the workflow-api guard. It never injects the ConfigMap into ``os.environ`` and
never constructs ``Settings(**configmap)``: both would trip unrelated model
validators (e.g. ``store_backend=postgres`` requiring a DSN).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from app.config import Settings

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
if str(REPOSITORY_ROOT) not in sys.path:
    # services/common is importable from the repository root (same bootstrap as
    # tests/test_workflow_api_contract.py).
    sys.path.insert(0, str(REPOSITORY_ROOT))

from services.common.configmap_parity import (  # noqa: E402
    Override,
    audit,
    env_name_index,
    load_configmap_data,
    validate_overrides,
)

# allow: SIZE_OK -- the bulk of this file is the reviewed override DATA table
# (one entry per intentional deployment deviation with its reason); the audit
# LOGIC lives in services/common/configmap_parity.py.
CONFIGMAP_PATH = (
    REPOSITORY_ROOT
    / 'kubeadm'
    / 'glasslab-v2'
    / 'research-orchestrator'
    / '10-configmap.yaml'
)
ENV_EXAMPLE_PATH = (
    REPOSITORY_ROOT / 'services' / 'research-orchestrator' / '.env.example'
)

CONFIGMAP_DATA = load_configmap_data(CONFIGMAP_PATH)
CONFIGMAP_KEYS = tuple(sorted(CONFIGMAP_DATA))

TURN_TIMEOUT_KEY = 'GLASSLAB_ORCHESTRATOR_OPENCODE_TURN_TIMEOUT_SECONDS'


_P = 'GLASSLAB_ORCHESTRATOR_'

_CONTAINER_PATH_REASON = (
    'Container layout: the code default targets a host/source-checkout path; '
    'the deployed pod mounts persistent storage at /mnt/artifacts (contracts '
    'ship under /app). Pinned so an unintended relocation fails review.'
)
_LIVE_ROUTING_REASON = (
    'Live per-agent routing activation: the code default is None (single-'
    'endpoint fallback); the deployment activates the .17/.18 split serving on '
    'port 52417. Pinned so a routing regression fails review.'
)
_LIVE_DISCORD_REASON = (
    'Live Discord identity: the code default is None so local/test never posts; '
    'the deployment points at the production guild/channel. Pinned so a '
    'misdirected deployment fails review.'
)


def _container_path(key: str, value: str) -> tuple[str, Override]:
    return key, Override('eq', _CONTAINER_PATH_REASON, expected=value)


def _live_route(key: str, value: str) -> tuple[str, Override]:
    return key, Override('eq', _LIVE_ROUTING_REASON, expected=value)


def _live_discord(key: str, value: str) -> tuple[str, Override]:
    return key, Override('eq', _LIVE_DISCORD_REASON, expected=value)


# Reviewed deviations from the code default. Every entry states WHY the
# deployment intentionally differs; a new key or a changed value fails the
# parametrized audit until a human adds (or edits) an entry here.
OVERRIDES: dict[str, Override] = dict(
    [
        # --- container path relocations ---
        _container_path(
            _P + 'DATABASE_PATH',
            '/mnt/artifacts/research-orchestrator/state/orchestrator.db',
        ),
        _container_path(
            _P + 'WORKSPACE_ROOT', '/mnt/artifacts/research-orchestrator/runs'
        ),
        _container_path(
            _P + 'ARTIFACT_ROOT',
            '/mnt/artifacts/research-orchestrator/artifacts',
        ),
        _container_path(
            _P + 'APPROVED_REPO_PATH',
            '/mnt/artifacts/research-orchestrator/approved-repo',
        ),
        _container_path(
            _P + 'EVALUATION_CONTRACT_ROOT', '/app/evaluation-contracts'
        ),
        _container_path(
            _P + 'PROMOTED_CONTRACT_ROOT',
            '/mnt/artifacts/research-orchestrator/trusted-contracts/bundles',
        ),
        _container_path(
            _P + 'SEALED_CONTRACT_CANDIDATE_ROOT',
            '/mnt/artifacts/research-orchestrator/contract-candidates',
        ),
        _container_path(
            _P + 'TRUSTED_CONTRACT_CATALOG_PATH',
            '/mnt/artifacts/research-orchestrator/trusted-contracts/catalog.json',
        ),
        _container_path(_P + 'SHARED_MOUNT_ROOT', '/mnt/artifacts'),
        _container_path(
            _P + 'TASK_BUNDLE_ROOT',
            '/mnt/artifacts/research-orchestrator/task-bundles',
        ),
        _container_path(
            _P + 'TASK_ASSET_ROOT',
            '/mnt/artifacts/research-orchestrator/task-assets',
        ),
        _container_path(
            _P + 'DATASET_UPLOAD_ROOT',
            '/mnt/artifacts/research-orchestrator/dataset-uploads',
        ),
        _container_path(
            _P + 'BENCHMARK_DATASET_CATALOG_PATH',
            '/mnt/artifacts/research-orchestrator/datasets/catalog.json',
        ),
        _container_path(
            _P + 'OPENCODE_SHARED_CACHE_ROOT',
            '/mnt/artifacts/research-orchestrator/opencode-cache',
        ),
        # --- live split-model routing (code default None) ---
        _live_route(_P + 'AGENT_MODEL_NAME', 'mlx-community/Qwen3-Coder-Next-4bit'),
        _live_route(
            _P + 'AGENT_BASE_URL_HONEYDEW', 'http://192.168.1.18:52417/v1'
        ),
        _live_route(
            _P + 'AGENT_BASE_URL_BEAKER', 'http://192.168.1.14:52417/v1'
        ),
        _live_route(
            _P + 'AGENT_MODEL_HONEYDEW',
            'mlx-community/Qwen3-Next-80B-A3B-Thinking-4bit',
        ),
        _live_route(
            _P + 'AGENT_MODEL_BEAKER', 'mlx-community/Qwen3-14B-8bit'
        ),
        _live_route(
            _P + 'HONEYDEW_REASONING_AGENT_BASE_URL',
            'http://192.168.1.18:52417/v1',
        ),
        _live_route(
            _P + 'HONEYDEW_REASONING_AGENT_MODEL',
            'mlx-community/Qwen3-Next-80B-A3B-Thinking-4bit',
        ),
        _live_route(
            _P + 'HONEYDEW_STRUCTURED_AGENT_BASE_URL',
            'http://192.168.1.17:52417/v1',
        ),
        _live_route(
            _P + 'HONEYDEW_STRUCTURED_AGENT_MODEL',
            'mlx-community/Qwen3-Coder-Next-4bit',
        ),
        _live_route(
            _P + 'TASK_COMPILER_AGENT_BASE_URL',
            'http://192.168.1.17:52417/v1',
        ),
        _live_route(
            _P + 'TASK_COMPILER_AGENT_MODEL',
            'mlx-community/Qwen3-Coder-Next-4bit',
        ),
        # --- live backend / feature selection ---
        (
            _P + 'STORE_BACKEND',
            Override(
                'eq',
                'Production record/event store is Postgres; sqlite is only the '
                'local-development and import-migration default.',
                expected='postgres',
            ),
        ),
        (
            _P + 'DISCORD_ENABLED',
            Override(
                'eq',
                'Discord projection is enabled in the live deployment; the code '
                'default stays False so local/test never contact Discord.',
                expected='true',
            ),
        ),
        (
            _P + 'DISCORD_CONTROLS_ENABLED',
            Override(
                'eq',
                'Operator slash-command controls are enabled in the live '
                'deployment; the code default stays False.',
                expected='true',
            ),
        ),
        _live_discord(_P + 'DISCORD_APPLICATION_ID', '1531982907772502027'),
        _live_discord(_P + 'DISCORD_GUILD_ID', '1529693375027089458'),
        _live_discord(_P + 'DISCORD_CHANNEL_ID', '1529939608895488160'),
        _live_discord(_P + 'DISCORD_ADMIN_ROLE_ID', '1529938777714200767'),
        # --- pinned artifact revision ---
        (
            _P + 'KNOWLEDGE_EMBEDDING_REVISION',
            Override(
                'eq',
                'Dense-retrieval lineage pins the resolved HuggingFace revision '
                '(configmap comment: cached refs/main 2026-09-01) so the '
                'provider resolves without a lazy model load.',
                expected='e58a8f756156a1293d763f17e3aae643474e9b8a',
            ),
        ),
        # --- numeric widening (never narrowing) ---
        (
            _P + 'KNOWLEDGE_MAX_SOURCE_BYTES',
            Override(
                'gte',
                'Whole books upload as single sources; the 2 MiB code default '
                'rejects textbook-size extracts, so the deployment widens to '
                '16 MiB and must never narrow below the default.',
            ),
        ),
        # --- list override (deployed set differs on purpose) ---
        (
            _P + 'KNOWLEDGE_ALLOWLIST_ROOTS',
            Override(
                'set',
                'Ingestion may only read the mounted approved repository; the '
                'two container roots replace the host-oriented code defaults.',
                expected_set=(
                    '/mnt/artifacts/research-orchestrator/approved-repo/docs',
                    '/mnt/artifacts/research-orchestrator/approved-repo/services/'
                    'research-orchestrator/evaluation-contracts',
                ),
            ),
        ),
        # --- explicit enum allowlist (real semantic drift, surfaced) ---
        (
            _P + 'OPENCODE_STRUCTURED_OUTPUT_MODE',
            Override(
                'allowlist',
                'The deployment deliberately runs OpenCode in prompt-mode '
                'structured output, overriding the json_schema code default. '
                'Only prompt is accepted; any other mode must be reviewed.',
                allowed=frozenset({'prompt'}),
            ),
        ),
    ]
)

VIOLATIONS = audit(Settings, CONFIGMAP_DATA, OVERRIDES)


def env_example_value(name: str) -> str:
    for raw_line in ENV_EXAMPLE_PATH.read_text(encoding='utf-8').splitlines():
        line = raw_line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, _, value = line.partition('=')
        if key.strip() == name:
            return value.strip()
    raise AssertionError(f'{name} is missing from {ENV_EXAMPLE_PATH}')


def test_tracked_configmap_never_narrows_turn_timeout() -> None:
    code_default = Settings().opencode_turn_timeout_seconds
    deployed = float(CONFIGMAP_DATA[TURN_TIMEOUT_KEY])
    assert deployed >= code_default, (
        f'{CONFIGMAP_PATH} sets {TURN_TIMEOUT_KEY}={deployed:g} below the code '
        f'default {code_default:g} in app/config.py; the env override silently '
        'defeats the documented widening. Raise the configmap to at least '
        f'{code_default:g}.'
    )


def test_env_example_mirrors_turn_timeout_default() -> None:
    code_default = Settings().opencode_turn_timeout_seconds
    example = float(env_example_value(TURN_TIMEOUT_KEY))
    assert example == code_default, (
        f'{ENV_EXAMPLE_PATH} sets {TURN_TIMEOUT_KEY}={example:g} but the code '
        f'default is {code_default:g}; keep the example in lockstep with '
        'app/config.py.'
    )


@pytest.mark.parametrize(
    'key', [pytest.param(key, id=key) for key in CONFIGMAP_KEYS]
)
def test_configmap_key_respects_default_invariant(key: str) -> None:
    assert key not in VIOLATIONS, VIOLATIONS[key]


def test_unknown_configmap_key_is_flagged() -> None:
    typo = dict(
        CONFIGMAP_DATA,
        GLASSLAB_ORCHESTRATOR_OPENCODE_TURN_TIMOUT_SECONDS='3600',
    )
    violations = audit(Settings, typo, OVERRIDES)
    typo_key = 'GLASSLAB_ORCHESTRATOR_OPENCODE_TURN_TIMOUT_SECONDS'
    assert typo_key in violations, (
        'a ConfigMap key with no matching Settings field must be flagged; '
        "pydantic-settings extra='ignore' does not detect it"
    )
    assert 'no Settings field' in violations[typo_key]


def test_override_table_is_well_formed() -> None:
    problems = validate_overrides(Settings, CONFIGMAP_DATA, OVERRIDES)
    assert not problems, '\n'.join(problems)


def test_env_name_resolution_contract() -> None:
    """Tripwire: the audit's upper-casing assumption depends on private API.

    ``EnvSettingsSource._extract_field_info`` is private. If a future
    pydantic-settings stops lower-casing names under ``case_sensitive=False``,
    the audit's ``.upper()`` normalization silently breaks; this test fails
    first so the assumption is re-derived instead of trusted.
    """
    index = env_name_index(Settings)
    assert TURN_TIMEOUT_KEY in index
    field_name, _field = index[TURN_TIMEOUT_KEY]
    assert field_name == 'opencode_turn_timeout_seconds'
