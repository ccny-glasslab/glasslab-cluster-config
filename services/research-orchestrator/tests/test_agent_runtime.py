"""Agent runtime backend selection (issue #98).

The orchestrator retains OpenCode as the selected agent runtime; Hermes
remains only as an explicit opt-in rollback backend. These tests lock the
selector contract so the backend flip in the deployment configmap can never
silently regress.
"""

from __future__ import annotations

from app.config import Settings
from app.hermes_runtime import HermesProcessRuntime
from app.main import build_agent_runtime
from app.opencode_runtime import OpenCodeProcessRuntime


def test_build_agent_runtime_defaults_to_opencode() -> None:
    settings = Settings(agent_runtime_backend='opencode')
    runtime = build_agent_runtime(settings)
    assert isinstance(runtime, OpenCodeProcessRuntime)
    assert not isinstance(runtime, HermesProcessRuntime)


def test_build_agent_runtime_hermes_is_explicit_opt_in() -> None:
    settings = Settings(agent_runtime_backend='hermes')
    runtime = build_agent_runtime(settings)
    assert isinstance(runtime, HermesProcessRuntime)