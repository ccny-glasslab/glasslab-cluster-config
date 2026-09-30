"""LLM provider abstraction for the corpus-RAG prototype.

Networkless by default: ``get_llm()`` returns the deterministic offline
provider, and the remote OpenAI-compatible provider performs no network
I/O at construction — the HTTP client is created lazily inside the
completion methods. The API key is only ever placed in request headers and
is never logged.

``build_rag_llm_provider`` is the ``/ui`` chat factory: it is opt-in
(``rag_llm_enabled``), reads the ``opencode-go`` key from the mounted OpenCode
auth file at construction time, and returns ``None`` rather than raising when
the lane is disabled or the credential is missing, so app startup never
depends on the remote provider.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol

from app.corpus_rag.contracts import MAX_SUBQUERIES

if TYPE_CHECKING:
    import httpx

BASE_URL_ENV = 'GLASSLAB_RAG_LLM_BASE_URL'
API_KEY_ENV = 'GLASSLAB_RAG_LLM_API_KEY'
MODEL_ENV = 'GLASSLAB_RAG_LLM_MODEL'

REQUEST_TIMEOUT_SECONDS = 30
SUBQUERY_LINE_PREFIX = 'SUBQUERY:'
OPENCODE_GO_PROVIDER_ID = 'opencode-go'

# The OpenCode Go gateway rejects a request without a routing session header
# (400 MissingSessionID), so every call identifies the caller and its session.
_USER_AGENT = 'glasslab-corpus-rag/1.0'
SESSION_ID_HEADER = 'x-opencode-session'
DEFAULT_SESSION_ID = 'glasslab-corpus-rag'


class ProviderNotConfiguredError(RuntimeError):
    """A required provider environment variable is missing."""


class LlmRequestError(RuntimeError):
    """A provider request failed with a non-2xx HTTP status."""


class LlmResponseError(RuntimeError):
    """A provider response could not be parsed as a JSON object."""


def _extract_message_content(response_json: Any) -> str:
    """Return ``choices[0].message.content`` as a plain string.

    Raises :class:`LlmResponseError` when the envelope is malformed or the
    content is not a string (for example a tool-call-only message).
    """
    try:
        content = response_json['choices'][0]['message']['content']
    except (KeyError, IndexError, TypeError) as exc:
        raise LlmResponseError(
            'provider response envelope missing choices/message content'
        ) from exc
    if not isinstance(content, str):
        raise LlmResponseError('provider message content is not a string')
    return content


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

    Endpoint, model, key, and timeout may be passed to the constructor; any
    omitted value falls back to the environment, and a missing
    ``GLASSLAB_RAG_LLM_BASE_URL`` raises :class:`ProviderNotConfiguredError`
    naming the variable. Construction never touches the network.
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        model: str | None = None,
        api_key: str | None = None,
        timeout: float = REQUEST_TIMEOUT_SECONDS,
        session_id: str = DEFAULT_SESSION_ID,
    ) -> None:
        resolved_base_url = base_url or os.environ.get(BASE_URL_ENV)
        if not resolved_base_url:
            msg = (
                f'{type(self).__name__} requires {BASE_URL_ENV} to be set '
                f'(also optional: {API_KEY_ENV}, {MODEL_ENV})'
            )
            raise ProviderNotConfiguredError(msg)
        self.base_url = resolved_base_url.rstrip('/')
        self.api_key = (
            api_key if api_key is not None else os.environ.get(API_KEY_ENV)
        )
        self.model = model if model is not None else os.environ.get(MODEL_ENV)
        self.timeout = timeout
        self.session_id = session_id

    def _headers(self) -> dict[str, str]:
        headers = {
            'Content-Type': 'application/json',
            'User-Agent': _USER_AGENT,
            SESSION_ID_HEADER: self.session_id,
        }
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

    def complete(self, *, system: str, user: str) -> str:
        """Return the assistant message content for one prompt turn.

        The raw-text counterpart to :meth:`complete_json` (the ``/ui`` chat
        parses the JSON answer itself). Raises :class:`LlmRequestError` on a
        non-2xx status and :class:`LlmResponseError` on a malformed body; the
        caller falls back to extractive synthesis on any of these.
        """
        import httpx

        payload = {
            'model': self.model,
            'messages': [
                {'role': 'system', 'content': system},
                {'role': 'user', 'content': user},
            ],
        }
        client: httpx.Client = httpx.Client(timeout=self.timeout)
        try:
            response = client.post(
                f'{self.base_url}/chat/completions',
                headers=self._headers(),
                json=payload,
            )
        finally:
            client.close()
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise LlmRequestError(
                f'provider returned HTTP {exc.response.status_code}'
            ) from exc
        try:
            envelope = response.json()
        except ValueError as exc:
            raise LlmResponseError(
                'provider returned a non-JSON HTTP body'
            ) from exc
        return _extract_message_content(envelope)


def _read_opencode_go_key(auth_json_path: str | None) -> str | None:
    """Return the ``opencode-go`` key from an OpenCode auth file, or None.

    Best-effort and call-time: a missing/unreadable/unparseable file, or an
    entry without a string ``key``, yields ``None`` so a caller can disable the
    lane instead of failing. The key is returned for request-header use only
    and is never logged.
    """
    if not auth_json_path:
        return None
    try:
        raw = Path(auth_json_path).read_text(encoding='utf-8')
    except OSError:
        return None
    try:
        document = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(document, dict):
        return None
    entry = document.get(OPENCODE_GO_PROVIDER_ID)
    if not isinstance(entry, dict):
        return None
    key = entry.get('key')
    if isinstance(key, str) and key:
        return key
    return None


def build_rag_llm_provider(settings: Any) -> LlmProvider | None:
    """Build the ``/ui`` chat synthesis provider, or None when unavailable.

    Returns ``None`` unless the lane is enabled and the mounted OpenCode auth
    file yields an ``opencode-go`` key, so app startup never depends on the
    remote provider (the chat then answers extractively). The endpoint and
    model come from the ``rag_llm_*`` settings.
    """
    if not getattr(settings, 'rag_llm_enabled', False):
        return None
    api_key = _read_opencode_go_key(
        getattr(settings, 'opencode_auth_json_path', None)
    )
    if not api_key:
        return None
    try:
        return OpenAiCompatibleProvider(
            base_url=settings.rag_llm_base_url,
            model=settings.rag_llm_model,
            api_key=api_key,
            timeout=settings.rag_llm_timeout_seconds,
        )
    except ProviderNotConfiguredError:
        return None


def get_llm(mode: Literal['offline', 'remote'] = 'offline') -> LlmProvider:
    """Factory: offline scripted provider by default, remote when asked."""
    match mode:
        case 'offline':
            return OfflineDeterministicLlm()
        case 'remote':
            return OpenAiCompatibleProvider()
        case other:
            raise ValueError(f'unknown LLM mode: {other!r}')
