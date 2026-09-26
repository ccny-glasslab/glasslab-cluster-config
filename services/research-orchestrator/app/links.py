"""Signed, self-contained links to run reports, artifacts, and context packets.

A link token embeds its entire signed payload, so nothing about the target is
reconstructed from separate query parameters. The signature covers exactly
``(kind, run_id, ref, exp, kid)`` where ``ref`` is a run-relative path or a
packet id -- never an absolute filesystem path. Redemption always re-resolves
the ref through the durable ``ArtifactRecord`` and ``VerifiedArtifactReader``,
so a valid signature authorizes only the named logical target.

The payload is domain-separated from the operator token (the literal ``glink``
prefix) and the signing secret is a dedicated setting, never
``operator_api_token``. All primitives come from the standard library.
"""

from __future__ import annotations

from base64 import b64decode, urlsafe_b64encode
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from hashlib import sha256
import hmac
from pathlib import PurePosixPath
import re

# Domain separator and payload version. They are part of the signed bytes, so
# a token can never be confused with another signed surface, and a future
# format change invalidates old tokens instead of being silently reinterpreted.
LINK_SIGNING_DOMAIN = 'glink'
LINK_PAYLOAD_VERSION = 'v1'
DEFAULT_LINK_KID = 'v1'
# Key ids this verifier accepts. Rotation adds the new kid here together with
# its secret; an unknown kid fails closed.
ACCEPTED_LINK_KIDS = frozenset({DEFAULT_LINK_KID})

_FIELD_SEPARATOR = '\n'
_RUN_ID_PATTERN = re.compile(r'^[A-Za-z0-9._-]{8,64}$')
_KID_PATTERN = re.compile(r'^[A-Za-z0-9._-]{1,32}$')
_KNOWLEDGE_PACKET_PREFIX = 'knowledge://context:'
_PACKET_ID_PATTERN = re.compile(r'^[A-Za-z0-9._-]{1,64}$')
# Run-relative directories whose contents may be served over a signed link.
# Everything else (source.zip, task.zip, metrics.json, evaluation.json,
# runtime/**, worktree paths) is denied by default.
LINKABLE_ARTIFACT_PREFIXES = (
    'reports/',
    'plots/',
    'tables/',
    'shared-artifacts/',
)
MAXIMUM_TOKEN_CHARS = 4096


class LinkError(Exception):
    """A link token is malformed, tampered with, expired, or out of policy."""


class LinkKind(StrEnum):
    REPORT = 'report'
    ARTIFACT = 'artifact'
    PACKET = 'packet'


@dataclass(frozen=True)
class LinkPayload:
    kind: LinkKind
    run_id: str
    ref: str
    exp: int
    kid: str


# A callable that mints an absolute link URL, or returns None when the link
# surface is not configured. Injected into the Discord projection so it stays
# free of settings and signing knowledge.
LinkBuilder = Callable[[LinkKind, str, str], str | None]


def _epoch_seconds(now: datetime) -> int:
    # A naive datetime is treated as UTC rather than local time so an
    # expiry check can never drift with the host timezone.
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return int(now.timestamp())


def _b64url_encode(data: bytes) -> str:
    return urlsafe_b64encode(data).rstrip(b'=').decode('ascii')


def _b64url_decode(value: str) -> bytes:
    # The encoder emits only the URL-safe alphabet with no padding, so the
    # standard base64 aliases ('+', '/') and padding ('=') must be rejected:
    # otherwise one signature would have several token spellings, defeating
    # canonicalisation and letting a token carry a path separator.
    if any(char in value for char in '+/='):
        raise ValueError('link token is not canonical base64url')
    padding = '=' * (-len(value) % 4)
    # validate=True rejects any remaining character outside the base64url
    # alphabet; binascii.Error is a ValueError subclass.
    return b64decode(value + padding, altchars=b'-_', validate=True)


def validate_run_id(run_id: str) -> str:
    if not _RUN_ID_PATTERN.fullmatch(run_id):
        raise LinkError('link run id is not a valid run identifier')
    return run_id


def validate_kid(kid: str) -> str:
    if not _KID_PATTERN.fullmatch(kid):
        raise LinkError('link key id is not valid')
    return kid


def validate_ref(ref: str) -> str:
    """Validate and normalize a run-relative ref, rejecting every escape.

    Percent-encoding is never part of a canonical ref, so a URL-encoded
    traversal (``%2e%2e``) fails closed here instead of depending on a later
    decode. Backslashes, NUL/control characters, absolute paths, ``..``
    components, and non-canonical forms are all rejected. A packet citation
    (``knowledge://context:<packet_id>``) is allowed only with a safe id
    suffix; it is a lookup key, never a filesystem path.
    """
    if not ref or any(ord(char) < 0x20 for char in ref):
        raise LinkError('link ref is empty or contains control characters')
    if '\\' in ref or '%' in ref:
        raise LinkError('link ref contains forbidden characters')
    if ref.startswith(_KNOWLEDGE_PACKET_PREFIX):
        packet_id = ref.removeprefix(_KNOWLEDGE_PACKET_PREFIX)
        if not _PACKET_ID_PATTERN.fullmatch(packet_id):
            raise LinkError('link packet ref is not a valid packet id')
        return ref
    if ref.startswith('/'):
        raise LinkError('link ref must be run-relative')
    path = PurePosixPath(ref)
    if path.is_absolute() or '..' in path.parts:
        raise LinkError('link ref must not escape the run root')
    if any(part in {'', '.'} for part in path.parts):
        raise LinkError('link ref must be a canonical relative path')
    normalized = path.as_posix()
    if normalized != ref:
        raise LinkError('link ref must be a canonical relative path')
    return normalized


def run_relative_ref(uri: str, run_id: str) -> str | None:
    """Return the run-relative ref for an ``artifact://<run_id>/...`` URI."""
    prefix = f'artifact://{run_id}/'
    if not uri.startswith(prefix):
        return None
    ref = uri[len(prefix):]
    return ref or None


def link_token_fingerprint(token: str) -> str:
    """A short, non-reversible token fingerprint for audit records."""
    return sha256(token.encode('utf-8', errors='replace')).hexdigest()[:12]


def _payload_bytes(payload: LinkPayload) -> bytes:
    # Every string field is validated to exclude the delimiter before this
    # point, so the newline-joined encoding is unambiguous.
    return _FIELD_SEPARATOR.join(
        (
            LINK_SIGNING_DOMAIN,
            LINK_PAYLOAD_VERSION,
            payload.kid,
            payload.kind.value,
            payload.run_id,
            payload.ref,
            str(payload.exp),
        )
    ).encode('utf-8')


def _signature(secret: str, payload_bytes: bytes) -> bytes:
    return hmac.new(
        secret.encode('utf-8'),
        payload_bytes,
        sha256,
    ).digest()


def sign_link(
    secret: str,
    *,
    kind: LinkKind,
    run_id: str,
    ref: str,
    kid: str = DEFAULT_LINK_KID,
    ttl_seconds: int,
    now: datetime,
) -> str:
    """Return ``base64url(payload) + '.' + base64url(hmac_sha256(secret, payload))``.

    Both segments are unpadded base64url, so the token is a single opaque path
    segment with no query parameters.
    """
    if not secret:
        raise LinkError('link signing secret is not configured')
    try:
        resolved_kind = LinkKind(kind)
    except ValueError as exc:
        raise LinkError(f'unknown link kind: {kind!r}') from exc
    payload = LinkPayload(
        kind=resolved_kind,
        run_id=validate_run_id(run_id),
        ref=validate_ref(ref),
        exp=_epoch_seconds(now) + int(ttl_seconds),
        kid=validate_kid(kid),
    )
    payload_bytes = _payload_bytes(payload)
    return (
        f'{_b64url_encode(payload_bytes)}.'
        f'{_b64url_encode(_signature(secret, payload_bytes))}'
    )


def verify_link(secret: str, token: str, *, now: datetime) -> LinkPayload:
    """Verify a token and return its payload, or raise :class:`LinkError`.

    The HMAC is checked with :func:`hmac.compare_digest` before any field is
    parsed or trusted, and every field is re-validated afterwards so a
    correctly signed but out-of-policy payload still fails closed.
    """
    if not secret:
        raise LinkError('link signing secret is not configured')
    if not token or len(token) > MAXIMUM_TOKEN_CHARS:
        raise LinkError('link token is malformed')
    encoded_payload, separator, encoded_signature = token.partition('.')
    if not separator or not encoded_payload or not encoded_signature:
        raise LinkError('link token is malformed')
    if '.' in encoded_signature:
        raise LinkError('link token is malformed')
    try:
        payload_bytes = _b64url_decode(encoded_payload)
        supplied_signature = _b64url_decode(encoded_signature)
    except ValueError as exc:
        raise LinkError('link token is malformed') from exc
    expected_signature = _signature(secret, payload_bytes)
    if not hmac.compare_digest(supplied_signature, expected_signature):
        raise LinkError('link token signature is invalid')
    fields = payload_bytes.decode('utf-8', errors='replace').split(
        _FIELD_SEPARATOR
    )
    if (
        len(fields) != 7
        or fields[0] != LINK_SIGNING_DOMAIN
        or fields[1] != LINK_PAYLOAD_VERSION
    ):
        raise LinkError('link token payload is malformed')
    kid, kind_value, run_id, ref, exp_text = fields[2:]
    try:
        exp = int(exp_text)
    except ValueError as exc:
        raise LinkError('link token expiry is malformed') from exc
    try:
        kind = LinkKind(kind_value)
    except ValueError as exc:
        raise LinkError('link token kind is unknown') from exc
    payload = LinkPayload(
        kind=kind,
        run_id=validate_run_id(run_id),
        ref=validate_ref(ref),
        exp=exp,
        kid=validate_kid(kid),
    )
    if payload.kid not in ACCEPTED_LINK_KIDS:
        raise LinkError('link token key id is not accepted')
    if payload.exp <= _epoch_seconds(now):
        raise LinkError('link token has expired')
    return payload


def build_link_url(
    *,
    public_base_url: str | None,
    secret: str | None,
    kind: LinkKind,
    run_id: str,
    ref: str,
    ttl_seconds: int,
    kid: str = DEFAULT_LINK_KID,
    now: datetime | None = None,
) -> str | None:
    """Return ``{public_base_url}/links/{token}``, or None when not configured.

    A missing base URL or signing secret returns None instead of raising so
    callers fall back to their non-link rendering; a deployment that sets a
    base URL without a secret therefore emits no links (fail closed).
    """
    base = (public_base_url or '').strip().rstrip('/')
    if not base or not secret:
        return None
    token = sign_link(
        secret,
        kind=kind,
        run_id=run_id,
        ref=ref,
        kid=kid,
        ttl_seconds=ttl_seconds,
        now=now or datetime.now(timezone.utc),
    )
    return f'{base}/links/{token}'
