"""Manifest parity guard: deployment env must never silently narrow a default.

A Kubernetes ConfigMap env entry overrides the pydantic-settings default, so a
stale tracked manifest silently defeats a deliberate widening in
``app/config.py`` even when the code, its comment, and its unit test all agree
on the wider value. That happened to ``opencode_turn_timeout_seconds``: the
code default widened 1800 -> 2400 -> 3600 while
``kubeadm/glasslab-v2/research-orchestrator/10-configmap.yaml`` stayed at 1800,
and the first deployment of the widened code kept enforcing the stale 30-minute
wall clock.

These tests pin the invariant, not a literal: the deployment may widen the
budget above the code default, but it must never narrow it. The literal-pin
variant (``test_opencode_turn_timeout_stays_1800``) lived in
``tests/security/test_orchestrator_configmap.py`` and is what hid the drift.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from app.config import Settings


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
CONFIGMAP_PATH = (
    REPOSITORY_ROOT
    / "kubeadm"
    / "glasslab-v2"
    / "research-orchestrator"
    / "10-configmap.yaml"
)
ENV_EXAMPLE_PATH = (
    REPOSITORY_ROOT / "services" / "research-orchestrator" / ".env.example"
)

TURN_TIMEOUT_KEY = "GLASSLAB_ORCHESTRATOR_OPENCODE_TURN_TIMEOUT_SECONDS"


def configmap_data() -> dict[str, str]:
    document = next(
        item
        for item in yaml.safe_load_all(CONFIGMAP_PATH.read_text(encoding="utf-8"))
        if item
    )
    return document["data"]


def env_example_value(name: str) -> str:
    for raw_line in ENV_EXAMPLE_PATH.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() == name:
            return value.strip()
    raise AssertionError(f"{name} is missing from {ENV_EXAMPLE_PATH}")


def test_tracked_configmap_never_narrows_turn_timeout() -> None:
    code_default = Settings().opencode_turn_timeout_seconds
    deployed = float(configmap_data()[TURN_TIMEOUT_KEY])
    assert deployed >= code_default, (
        f"{CONFIGMAP_PATH} sets {TURN_TIMEOUT_KEY}={deployed:g} below the code "
        f"default {code_default:g} in app/config.py; the env override silently "
        "defeats the documented widening. Raise the configmap to at least "
        f"{code_default:g}."
    )


def test_env_example_mirrors_turn_timeout_default() -> None:
    code_default = Settings().opencode_turn_timeout_seconds
    example = float(env_example_value(TURN_TIMEOUT_KEY))
    assert example == code_default, (
        f"{ENV_EXAMPLE_PATH} sets {TURN_TIMEOUT_KEY}={example:g} but the code "
        f"default is {code_default:g}; keep the example in lockstep with "
        "app/config.py."
    )
