"""Caller authentication and operation authorization for workflow-api."""

from __future__ import annotations

import secrets
from typing import TYPE_CHECKING

from fastapi import HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, SecretStr, field_validator
from starlette.routing import Match


if TYPE_CHECKING:
    from .config import Settings


CALLER_HEADER = 'X-Glasslab-Caller'
TOKEN_HEADER = 'X-Glasslab-Workflow-Token'


class CallerPolicy(BaseModel):
    """One authenticated caller's allowed normalized workflow operations."""

    model_config = ConfigDict(extra='forbid', frozen=True)

    name: str
    token: SecretStr
    allowed_operations: frozenset[str]

    @field_validator('name')
    @classmethod
    def require_caller_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError('caller policy name must not be empty')
        return value

    @field_validator('token')
    @classmethod
    def require_token(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value():
            raise ValueError('caller policy token must not be empty')
        return value

    @field_validator('allowed_operations')
    @classmethod
    def require_normalized_operations(cls, value: frozenset[str]) -> frozenset[str]:
        for operation in value:
            method, separator, path = operation.partition(' ')
            if not separator or method not in {'POST', 'PUT', 'PATCH', 'DELETE'} or not path.startswith('/'):
                raise ValueError('caller policy operations must use the form "METHOD /path-template"')
        return value


def route_template(request: Request) -> str | None:
    """Return the matched Starlette route template for this request, if any."""
    route = request.scope.get('route')
    if route is not None:
        return getattr(route, 'path_format', route.path)

    # Application middleware runs before Starlette assigns ``scope['route']``.
    # Match through the router here so authorization uses the same template the
    # endpoint will receive, rather than attacker-controlled raw path text.
    for candidate in request.app.router.routes:
        matched, _ = candidate.matches(request.scope)
        if matched is Match.FULL:
            request.scope['route'] = candidate
            return getattr(candidate, 'path_format', candidate.path)
    return None


def authenticate_request(request: Request, settings: Settings) -> CallerPolicy:
    """Authenticate a named caller and authorize its normalized operation."""
    caller_name = request.headers.get(CALLER_HEADER)
    supplied_token = request.headers.get(TOKEN_HEADER)
    if not caller_name or not supplied_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail='valid workflow caller credentials required',
        )

    policy = next((candidate for candidate in settings.caller_policies if candidate.name == caller_name), None)
    if policy is None or not secrets.compare_digest(supplied_token, policy.token.get_secret_value()):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail='valid workflow caller credentials required',
        )

    path_template = route_template(request)
    operation = f'{request.method.upper()} {path_template}' if path_template else None
    if operation not in policy.allowed_operations:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail='workflow caller is not authorized for this operation',
        )
    return policy
