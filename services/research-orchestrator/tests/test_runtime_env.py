"""Agent subprocess environment allowlist contract.

The security invariant is: agent-controlled subprocesses (OpenCode CLI,
Hermes gateway) must never inherit orchestrator control-plane secrets from
the parent environment. This module pins the allowlist builder shared by
both runtimes: benign runtime variables and explicitly named model-auth
variables may pass through; anything secret-bearing, GLASSLAB_*-prefixed,
or not on the allowlist must never reach a child.
"""

from __future__ import annotations

import pytest

from app.runtime_env import (
    BENIGN_RUNTIME_VARS,
    MODEL_AUTH_ENV_VARS,
    build_agent_environment,
)


def test_model_and_benign_forwarded_secrets_absent(monkeypatch) -> None:
    monkeypatch.setenv('PATH', '/usr/local/bin:/usr/bin')
    monkeypatch.setenv('TMPDIR', '/tmp')
    monkeypatch.setenv('LANG', 'C.UTF-8')
    monkeypatch.setenv('OPENCODE_API_KEY', 'model-key-1')
    monkeypatch.setenv('CUSTOM_API_KEY', 'model-key-2')
    monkeypatch.setenv('GLASSLAB_ORCHESTRATOR_DISCORD_BOT_TOKEN', 'discord-token')
    monkeypatch.setenv('GLASSLAB_ORCHESTRATOR_OPERATOR_API_TOKEN', 'operator-token')
    monkeypatch.setenv('GLASSLAB_WORKFLOW_API_TOKEN', 'workflow-token')
    monkeypatch.setenv('GLASSLAB_ORCHESTRATOR_DISCORD_WEBHOOK_URL', 'https://hook')

    environment = build_agent_environment(
        runtime_vars={'HOME': '/run/home', 'XDG_CACHE_HOME': '/run/cache'},
        model_auth_vars={'OPENCODE_API_KEY', 'CUSTOM_API_KEY'},
    )

    assert environment['PATH'] == '/usr/local/bin:/usr/bin'
    assert environment['TMPDIR'] == '/tmp'
    assert environment['LANG'] == 'C.UTF-8'
    assert environment['HOME'] == '/run/home'
    assert environment['XDG_CACHE_HOME'] == '/run/cache'
    assert environment['OPENCODE_API_KEY'] == 'model-key-1'
    assert environment['CUSTOM_API_KEY'] == 'model-key-2'
    for leaked in (
        'GLASSLAB_ORCHESTRATOR_DISCORD_BOT_TOKEN',
        'GLASSLAB_ORCHESTRATOR_OPERATOR_API_TOKEN',
        'GLASSLAB_WORKFLOW_API_TOKEN',
        'GLASSLAB_ORCHESTRATOR_DISCORD_WEBHOOK_URL',
    ):
        assert leaked not in environment, f'secret {leaked} leaked into child env'


def test_control_plane_secrets_never_forwarded(monkeypatch) -> None:
    monkeypatch.setenv('GLASSLAB_ORCHESTRATOR_DISCORD_BOT_TOKEN', 'd-token')
    monkeypatch.setenv('GLASSLAB_ORCHESTRATOR_OPERATOR_API_TOKEN', 'o-token')
    monkeypatch.setenv('GLASSLAB_WORKFLOW_API_TOKEN', 'w-token')
    monkeypatch.setenv('GLASSLAB_ORCHESTRATOR_DISCORD_WEBHOOK_URL', 'https://hook')
    monkeypatch.setenv('GLASSLAB_KUBECONFIG', '/root/.kube/config')
    monkeypatch.setenv('SOME_RANDOM_TOKEN', 'ambient-token')

    environment = build_agent_environment(
        runtime_vars={'HOME': '/run/home'},
    )

    for leaked in (
        'GLASSLAB_ORCHESTRATOR_DISCORD_BOT_TOKEN',
        'GLASSLAB_ORCHESTRATOR_OPERATOR_API_TOKEN',
        'GLASSLAB_WORKFLOW_API_TOKEN',
        'GLASSLAB_ORCHESTRATOR_DISCORD_WEBHOOK_URL',
        'GLASSLAB_KUBECONFIG',
        'SOME_RANDOM_TOKEN',
    ):
        assert leaked not in environment, f'secret {leaked} leaked into child env'


def test_missing_benign_vars_are_not_invented(monkeypatch) -> None:
    monkeypatch.delenv('TMPDIR', raising=False)
    monkeypatch.delenv('LANG', raising=False)

    environment = build_agent_environment(runtime_vars={'HOME': '/run/home'})

    assert 'TMPDIR' not in environment
    assert 'LANG' not in environment
    assert environment['HOME'] == '/run/home'


def test_runtime_vars_override_parent_values(monkeypatch) -> None:
    monkeypatch.setenv('HOME', '/inherited/home')

    environment = build_agent_environment(
        runtime_vars={'HOME': '/per-run/home'},
    )

    assert environment['HOME'] == '/per-run/home'


def test_default_model_auth_vars_are_exactly_the_allowlist(monkeypatch) -> None:
    values = {
        'OPENCODE_API_KEY': 'k1',
        'CUSTOM_API_KEY': 'k2',
        'OPENAI_API_KEY': 'k3',
        'EXO_API_KEY': 'k4',
        'HERMES_API_KEY': 'k5',
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv('SOME_OTHER_KEY', 'k6')

    environment = build_agent_environment(runtime_vars={})

    assert set(MODEL_AUTH_ENV_VARS) == set(values)
    for name, value in values.items():
        assert environment.get(name) == value
    assert 'SOME_OTHER_KEY' not in environment


def test_defense_in_depth_rejects_secret_named_requests(monkeypatch) -> None:
    monkeypatch.setenv('GLASSLAB_ORCHESTRATOR_DISCORD_BOT_TOKEN', 'd-token')

    environment = build_agent_environment(
        runtime_vars={},
        # Even if a caller asked for it, defense-in-depth must refuse.
        model_auth_vars={'GLASSLAB_ORCHESTRATOR_DISCORD_BOT_TOKEN'},
    )

    assert 'GLASSLAB_ORCHESTRATOR_DISCORD_BOT_TOKEN' not in environment


def test_benign_runtime_var_allowlist_is_exact(monkeypatch) -> None:
    assert BENIGN_RUNTIME_VARS == {
        'PATH',
        'TMPDIR',
        'TMP',
        'TEMP',
        'LANG',
        'LC_ALL',
        'LC_CTYPE',
        'USER',
        'LOGNAME',
        'TZ',
    }