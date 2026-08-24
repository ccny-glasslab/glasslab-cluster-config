"""LLM provider abstraction for the corpus-RAG prototype.

Networkless by default: ``get_llm()`` returns the deterministic offline
provider, and the remote OpenAI-compatible provider performs no network
I/O at construction — the HTTP client is created lazily inside
``complete_json``. The API key is only ever placed in request headers and
is never logged.
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING, Any, Literal, Protocol

from app.corpus_rag.contracts import MAX_SUBQUERIES

if TYPE_CHECKING:
    import httpx

BASE_URL_ENV = 'GLASSLAB_RAG_LLM_BASE_URL'
API_KEY_ENV = 'GLASSLAB_RAG_LLM_API_KEY'
MODEL_ENV = 'GLASSLAB_RAG_LLM_MODEL'

REQUEST_TIMEOUT_SECONDS = 30
SUBQUERY_LINE_PREFIX = 'SUBQUERY:'


class ProviderNotConfiguredError(RuntimeError):
    """A required provider environment variable is missing."""


class LlmResponseError(RuntimeError):
    """A provider response could not be parsed as a JSON object."""


class LlmProvider(Protocol):
    """One prompt turn in, one parsed JSON object out."""

    def complete_json(self, system: str, user: str) -> dict[str, Any]:
        ...


class OfflineDeterministicLlm:
    """Scripted provider: echoes ``SUBQUERY: <text>`` lines from the user message.

    Order-preserving and capped at ``MAX_SUBQUERIES``; no markers yields an
    empty ``subqueries`` list.
    """

    def complete_json(self, system: str, user: str) -> dict[str, Any]:
        subqueries: list[str] = []
        for line in user.splitlines():
            stripped = line.strip()
            if not stripped.startswith(SUBQUERY_LINE_PREFIX):
                continue
            text = stripped[len(SUBQUERY_LINE_PREFIX):].strip()
            if text:
                subqueries.append(text)
            if len(subqueries) >= MAX_SUBQUERIES:
                break
        return {'subqueries': subqueries}


class OpenAiCompatibleProvider:
    """Client for an OpenAI-compatible ``/chat/completions`` endpoint.

    Configuration is read from the environment at construction time; a
    missing ``GLASSLAB_RAG_LLM_BASE_URL`` raises
    :class:`ProviderNotConfiguredError` naming the variable. Construction
    never touches the network.
    """

    def __init__(self) -> None:
        base_url = os.environ.get(BASE_URL_ENV)
        if not base_url:
            msg = (
                f'{type(self).__name__} requires {BASE_URL_ENV} to be set '
                f'(also optional: {API_KEY_ENV}, {MODEL_ENV})'
            )
            raise ProviderNotConfiguredError(msg)
        self.base_url = base_url.rstrip('/')
        self.api_key = os.environ.get(API_KEY_ENV)
        self.model = os.environ.get(MODEL_ENV)

    def _headers(self) -> dict[str, str]:
        headers = {'Content-Type': 'application/json'}
        if self.api_key:
            # Header material only; never logged.
            headers['Authorization'] = f'Bearer {self.api_key}'
        return headers

    @staticmethod
    def _extract_content_object(response_json: Any) -> dict[str, Any]:
        try:
            content = response_json['choices'][0]['message']['content']
        except (KeyError, IndexError, TypeError) as exc:
            raise LlmResponseError(
                'provider response envelope missing choices/message content'
            ) from exc
        try:
            parsed = json.loads(content)
        except (TypeError, ValueError) as exc:
            raise LlmResponseError(
                'provider message content is not valid JSON'
            ) from exc
        if not isinstance(parsed, dict):
            raise LlmResponseError(
                'provider JSON content is not a JSON object'
            )
        return parsed

    def complete_json(self, system: str, user: str) -> dict[str, Any]:
        import httpx

        payload = {
            'model': self.model,
            'messages': [
                {'role': 'system', 'content': system},
                {'role': 'user', 'content': user},
            ],
            # Best-effort: servers that ignore response_format still work;
            # we validate the returned content ourselves either way.
            'response_format': {'type': 'json_object'},
        }
        client: httpx.Client = httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS)
        try:
            response = client.post(
                f'{self.base_url}/chat/completions',
                headers=self._headers(),
                json=payload,
            )
        finally:
            client.close()
        response.raise_for_status()
        try:
            envelope = response.json()
        except ValueError as exc:
            raise LlmResponseError(
                'provider returned a non-JSON HTTP body'
            ) from exc
        return self._extract_content_object(envelope)


def get_llm(mode: Literal['offline', 'remote'] = 'offline') -> LlmProvider:
    """Factory: offline scripted provider by default, remote when asked."""
    match mode:
        case 'offline':
            return OfflineDeterministicLlm()
        case 'remote':
            return OpenAiCompatibleProvider()
        case other:
            raise ValueError(f'unknown LLM mode: {other!r}')
