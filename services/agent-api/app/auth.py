"""Shared API-token authentication for the legacy agent API."""

from __future__ import annotations

import secrets

from fastapi import HTTPException, Request, status


TOKEN_HEADER = 'X-Glasslab-Agent-Token'


def authenticate_request(request: Request, expected_token: str) -> None:
    """Reject requests that do not present the configured API token.

    Comparison is constant-time so response timing does not leak the expected
    token. Raises HTTPException(401) when the header is absent or mismatched.
    """
    supplied_token = request.headers.get(TOKEN_HEADER)
    if supplied_token is None or not secrets.compare_digest(
        supplied_token.encode('utf-8'),
        expected_token.encode('utf-8'),
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail='valid agent API token required',
        )
