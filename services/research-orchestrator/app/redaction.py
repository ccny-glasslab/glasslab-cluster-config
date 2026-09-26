"""Redaction of credential-shaped content before it leaves the process.

Used by turn_inspection.py to sanitize TurnRecord.input_event and
structured_output before either is returned through GET
/runs/{run_id}/turns or the /research-turns Discord command. Turns are
internal agent-loop state (recovery checkpoints, retrieved context, raw
model output) and were never audited for credential-safety the way the
normalized event log and knowledge index are, so this module scans
defensively rather than trusting the caller.

The content-pattern philosophy mirrors KnowledgeManager's ingestion-time
secret exclusion (see SECRET_PATTERNS in knowledge_manager.py): matching
text is treated as sensitive and never returned, rather than trying to
prove it safe first. This module additionally redacts by field name and
recognizes a few concrete credential formats (Discord bot tokens, JWTs,
common vendor API key prefixes, kubeconfig/PEM material) so a payload can be
redacted field-by-field instead of excluded wholesale.

Deliberately NOT reused here: knowledge_manager's generic "long base64/hex
blob at end of line" heuristic. Turn payloads legitimately carry bare
64-character SHA-256 digests (artifact and contract provenance hashes, see
ResearchOrchestrator._evidence_snapshot) that are exactly the kind of
structured input this endpoint exists to expose; that heuristic would
silently redact them.
"""

from __future__ import annotations

import re
from typing import Any

REDACTED = '[REDACTED]'

# Field names always treated as credential-bearing, regardless of their
# value's shape. Covers the credential kinds named in issue #95 (Discord
# tokens, the X-Glasslab-Operator-Token, kubeconfig contents, model API
# keys) plus the secret-bearing settings called out in config.py
# (operator_api_token, discord_bot_token, discord_webhook_url).
_SENSITIVE_KEY_SUBSTRINGS = (
    'token',
    'secret',
    'password',
    'passwd',
    'apikey',
    'credential',
    'kubeconfig',
    'privatekey',
    'clientsecret',
    'accesskey',
    'webhook',
    'authorization',
)

# Key names that legitimately contain one of the substrings above but never
# hold credential material (a token *count*, not a token).
_SAFE_KEY_EXCEPTIONS = frozenset({'token_count', 'token_budget'})

# Keyword-context content patterns: kept conceptually in sync with
# knowledge_manager.SECRET_PATTERNS, minus the bare long-blob heuristic (see
# module docstring). token/bearer/secret/credential concepts alone are not
# rejected; explicit value-assignment or well-known instruction phrasing is.
_KEYWORD_VALUE_PATTERNS = (
    re.compile(r'password', re.IGNORECASE),
    re.compile(r'api[_-]?key', re.IGNORECASE),
    re.compile(r'token[\"\']?\s*[:=]\s*\S+', re.IGNORECASE),
    re.compile(r'(client|api|auth|private|access)\s+secret', re.IGNORECASE),
    re.compile(r'secret\s*(key|token|id)', re.IGNORECASE),
    re.compile(r'secret[\"\']?\s*[:=]\s*\S+', re.IGNORECASE),
    re.compile(r'bearer\s+[A-Za-z0-9._\-+/=]{10,}', re.IGNORECASE),
    re.compile(r'credential', re.IGNORECASE),
    re.compile(r'private[_-]?key', re.IGNORECASE),
    re.compile(r'auth[_-]?header', re.IGNORECASE),
    re.compile(r'access[_-]?key', re.IGNORECASE),
    re.compile(r'client[_-]?secret', re.IGNORECASE),
)

# Concrete credential formats that need no surrounding keyword to be
# recognizable on sight.
_KNOWN_SECRET_FORMATS = (
    # Discord bot token: three dot-separated base64url segments.
    re.compile(r'\b[A-Za-z0-9_-]{24,28}\.[A-Za-z0-9_-]{6,7}\.[A-Za-z0-9_-]{27,40}\b'),
    # JSON Web Token.
    re.compile(r'\bey[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b'),
    # Common vendor API key prefixes.
    re.compile(r'\bsk-ant-[A-Za-z0-9_-]{20,}\b'),
    re.compile(r'\b(?:sk|rk|pk)-[A-Za-z0-9]{16,}\b'),
    re.compile(r'\bghp_[A-Za-z0-9]{36}\b'),
    re.compile(r'\bAKIA[0-9A-Z]{16}\b'),
    re.compile(r'\bxox[baprs]-[A-Za-z0-9-]{10,}\b'),
    # kubeconfig / PEM key material.
    re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----'),
    re.compile(r'\bclient-(?:certificate|key)-data:'),
    re.compile(r'\bcertificate-authority-data:'),
)

# A whole PEM private-key block. The header-only pattern above is enough to
# condemn a payload string outright, but free-text redaction replaces just the
# matched span, so the base64 body must be captured too. A truncated key with
# no END marker is redacted to end-of-string rather than left readable.
_PEM_PRIVATE_KEY_BLOCK = re.compile(
    r'-----BEGIN [A-Z ]*PRIVATE KEY-----'
    r'.*?'
    r'(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)',
    re.DOTALL,
)

# Free-text scanning needs no keyword context: the concrete formats plus whole
# PEM blocks. Ordered so a complete PEM block is consumed before its header
# line is matched on its own.
_FREE_TEXT_SECRET_FORMATS = (
    _PEM_PRIVATE_KEY_BLOCK,
    *_KNOWN_SECRET_FORMATS,
)


def _is_sensitive_key(key: str) -> bool:
    normalized = key.strip().lower()
    if normalized in _SAFE_KEY_EXCEPTIONS:
        return False
    collapsed = normalized.replace('-', '').replace('_', '')
    return any(substring in collapsed for substring in _SENSITIVE_KEY_SUBSTRINGS)


def _looks_like_secret(text: str) -> bool:
    if any(pattern.search(text) for pattern in _KNOWN_SECRET_FORMATS):
        return True
    return any(pattern.search(text) for pattern in _KEYWORD_VALUE_PATTERNS)


def redact_free_text(text: str) -> str:
    """Redact concrete credential formats inside free-form text.

    Unlike :func:`redact_payload`, no field-name or keyword-context heuristic
    applies: prose that merely discusses credentials ("set a password", "rotate
    the API key") is returned unchanged, and only byte sequences matching a
    concrete credential format are replaced. Used for report/answer/question/
    citation text, where keyword redaction would corrupt legitimate content.
    """
    redacted = text
    for pattern in _FREE_TEXT_SECRET_FORMATS:
        redacted = pattern.sub(REDACTED, redacted)
    return redacted


def redact_payload(value: Any) -> Any:
    """Recursively redact credential-shaped content from a JSON-like value.

    Fails closed: a field name that merely resembles a credential name is
    redacted outright (regardless of its value's type or shape), and every
    string leaf is scanned for known secret shapes even under an innocuous
    key. Non-string, non-container leaves (numbers, booleans, None) are
    returned unchanged since they cannot carry a credential payload.
    """
    if isinstance(value, dict):
        redacted: dict[Any, Any] = {}
        for item_key, item_value in value.items():
            if isinstance(item_key, str) and _is_sensitive_key(item_key):
                redacted[item_key] = REDACTED
            else:
                redacted[item_key] = redact_payload(item_value)
        return redacted
    if isinstance(value, list):
        return [redact_payload(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_payload(item) for item in value)
    if isinstance(value, str):
        return REDACTED if _looks_like_secret(value) else value
    return value
