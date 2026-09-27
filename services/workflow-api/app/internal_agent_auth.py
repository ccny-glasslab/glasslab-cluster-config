"""Outbound shared-token headers for workflow-api stage-agent calls.

Issue #602: the four stage agents (intake, interpretation, assessment, design)
reject any request that does not carry the shared internal token once
enforcement is deployed. workflow-api is their only caller, so every call site
routes its headers through ``internal_agent_headers`` instead of hard-coding
``Content-Type`` alone.

The token is attached only when ``GLASSLAB_WORKFLOW_API_AGENT_INTERNAL_TOKEN``
is configured. Rollout order is callers first (this change) and server
enforcement second, so a caller deployed before the shared Secret exists must
keep working against agents that do not yet validate the header. Once the
server side enforces, an unconfigured caller token degrades every agent call
to the deterministic fallback path; the server side is the fail-closed edge.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Settings


INTERNAL_TOKEN_HEADER = 'X-Glasslab-Internal-Token'


def internal_agent_headers(settings: Settings) -> dict[str, str]:
    """Return JSON content-type plus the shared internal token when configured."""
    headers = {'Content-Type': 'application/json'}
    token = settings.agent_internal_token
    if token is not None and token.get_secret_value().strip():
        headers[INTERNAL_TOKEN_HEADER] = token.get_secret_value()
    return headers
