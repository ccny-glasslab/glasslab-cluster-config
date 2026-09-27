"""Inbound shared-token authentication for the interpretation agent.

The only intended caller is workflow-api, which presents the shared internal
token as X-Glasslab-Internal-Token (issue #602). Fail closed: an unconfigured
token rejects every protected request with 503 instead of silently serving
unauthenticated callers, and a missing or wrong token is 401. /healthz stays
anonymous so the kubelet probes and the public smoke test keep working.
"""

from __future__ import annotations

import os
import secrets

from fastapi import Header, HTTPException, status

INTERNAL_TOKEN_HEADER = 'X-Glasslab-Internal-Token'
INTERNAL_TOKEN_ENV = 'GLASSLAB_AGENT_INTERNAL_TOKEN'


def configured_internal_token() -> str | None:
    token = os.environ.get(INTERNAL_TOKEN_ENV, '')
    return token if token.strip() else None


def require_internal_token(
    x_glasslab_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
) -> None:
    """Reject requests that do not carry the configured shared internal token."""
    expected = configured_internal_token()
    if expected is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='internal token is not configured',
        )
    if x_glasslab_internal_token is None or not secrets.compare_digest(
        x_glasslab_internal_token.encode('utf-8'),
        expected.encode('utf-8'),
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail='valid internal token required',
        )
