"""Explicit allowlist environment for agent-controlled subprocesses.

Security boundary: the orchestrator pod environment contains control-plane
secrets (Discord bot token, operator API token, workflow-api token,
kubeconfig, and every ``GLASSLAB_*`` variable). Agent subprocesses
(OpenCode CLI, Hermes gateway) must never inherit them. Both runtimes build
their child environment through :func:`build_agent_environment`, which
starts from an empty dict instead of ``os.environ`` and forwards only:

- a fixed set of benign runtime variables (PATH, locale, temp dirs), when
  present in the parent;
- explicitly named model-provider authentication variables; and
- the runtime-specific variables each adapter sets itself
  (XDG/HOME/per-run roots, server credentials).
"""

from __future__ import annotations

import os
import re
from typing import Mapping

# Variables a spawned agent runtime legitimately needs to function. None of
# these carry control-plane credentials.
BENIGN_RUNTIME_VARS = frozenset(
    {
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
)

# Model-provider authentication variables the agent runtimes may need to
# reach the model endpoint. OpenCode reads OPENCODE_API_KEY (the deployment
# injects it via secretRef); Hermes' custom provider reads its key from an
# ambient variable whose exact name is not documented in this repository,
# so the bounded set below is forwarded. Every entry is a model-provider
# credential — none are orchestrator control-plane secrets.
MODEL_AUTH_ENV_VARS = frozenset(
    {
        'OPENCODE_API_KEY',
        'CUSTOM_API_KEY',
        'OPENAI_API_KEY',
        'EXO_API_KEY',
        'HERMES_API_KEY',
    }
)

# Defense-in-depth: even an explicit request for a variable matching these
# patterns is refused. The allowlist already excludes them, but this makes
# the boundary robust against future callers widening the allowlist.
_SECRET_NAME_PATTERNS = (
    re.compile(r'^GLASSLAB_'),
    re.compile(r'(TOKEN|KEY|SECRET|PASSWORD|CREDENTIAL)', re.IGNORECASE),
)


def _is_forbidden(name: str) -> bool:
    return any(pattern.search(name) for pattern in _SECRET_NAME_PATTERNS)


def build_agent_environment(
    *,
    runtime_vars: Mapping[str, str],
    model_auth_vars: set[str] | None = None,
) -> dict[str, str]:
    """Build a child environment without inheriting orchestrator secrets.

    ``runtime_vars`` are the per-runtime variables the adapter sets
    (XDG/HOME roots, server credentials) and always win over any forwarded
    value. ``model_auth_vars`` names the model-provider variables to copy
    from the parent environment; it defaults to the full bounded allowlist.
    """
    requested_auth = set(model_auth_vars or MODEL_AUTH_ENV_VARS)
    environment: dict[str, str] = {}

    for name in BENIGN_RUNTIME_VARS:
        value = os.environ.get(name)
        if value is not None:
            environment[name] = value

    for name in requested_auth:
        if name not in MODEL_AUTH_ENV_VARS and _is_forbidden(name):
            # Defense-in-depth: a requested ambient variable that is not on
            # the canonical model-auth allowlist AND matches a credential
            # pattern is refused, so a future caller cannot widen the
            # allowlist to smuggle a control-plane secret.
            continue
        value = os.environ.get(name)
        if value is not None:
            environment[name] = value

    # runtime_vars are adapter-owned by contract: the runtime author names
    # them explicitly (per-run XDG/HOME roots, ephemeral server credentials),
    # so they are trusted inputs rather than inherited secrets.
    environment.update(runtime_vars)

    return environment