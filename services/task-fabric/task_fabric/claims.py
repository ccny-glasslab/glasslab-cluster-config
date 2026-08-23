"""Authority-neutral lease and fencing value objects."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone


class ClaimValidationError(ValueError):
    """A lease operation is invalid or stale."""


def _require_utc(value: datetime) -> None:
    if not isinstance(value, datetime):
        raise ClaimValidationError("claim timestamps must be timezone-aware UTC")
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ClaimValidationError("claim timestamps must be timezone-aware UTC")


def _require_integer(value: int, description: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ClaimValidationError(f"{description} must be an integer")


def _require_ttl(ttl: timedelta) -> None:
    if not isinstance(ttl, timedelta) or ttl <= timedelta(0):
        raise ClaimValidationError("claim TTL must be positive")


@dataclass(frozen=True, slots=True)
class ClaimLease:
    owner: str
    attempt_number: int
    fencing_token: int
    heartbeat_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.owner, str) or not self.owner or len(self.owner) > 255:
            raise ClaimValidationError("invalid claim owner")
        _require_integer(self.attempt_number, "attempt number")
        if self.attempt_number < 1:
            raise ClaimValidationError("attempt number must be positive")
        _require_integer(self.fencing_token, "fencing token")
        if self.fencing_token < 1:
            raise ClaimValidationError("fencing token must be positive")
        _require_utc(self.heartbeat_at)
        _require_utc(self.expires_at)
        if self.expires_at <= self.heartbeat_at:
            raise ClaimValidationError("claim expiry must follow heartbeat")

    @classmethod
    def issue(
        cls,
        *,
        owner: str,
        attempt_number: int,
        fencing_token: int,
        now: datetime,
        ttl: timedelta,
    ) -> "ClaimLease":
        _require_utc(now)
        _require_ttl(ttl)
        return cls(owner, attempt_number, fencing_token, now, now + ttl)

    @staticmethod
    def next_fencing_token(active_fencing_token: int) -> int:
        _require_integer(active_fencing_token, "active fencing token")
        if active_fencing_token < 0:
            raise ClaimValidationError("active fencing token cannot be negative")
        return active_fencing_token + 1

    def is_active(self, now: datetime) -> bool:
        _require_utc(now)
        return now < self.expires_at

    def renew(
        self,
        *,
        owner: str,
        fencing_token: int,
        now: datetime,
        ttl: timedelta,
    ) -> "ClaimLease":
        if owner != self.owner or fencing_token != self.fencing_token:
            raise ClaimValidationError("claim owner or fencing token is stale")
        _require_integer(fencing_token, "fencing token")
        if not self.is_active(now):
            raise ClaimValidationError("expired claim cannot be renewed")
        _require_ttl(ttl)
        return replace(self, heartbeat_at=now, expires_at=now + ttl)

    def can_complete(
        self,
        *,
        owner: str,
        fencing_token: int,
        active_fencing_token: int,
        now: datetime,
    ) -> bool:
        if owner != self.owner:
            raise ClaimValidationError("claim owner is stale")
        _require_integer(fencing_token, "fencing token")
        _require_integer(active_fencing_token, "active fencing token")
        if fencing_token != self.fencing_token or fencing_token != active_fencing_token:
            raise ClaimValidationError("fencing token is stale")
        if not self.is_active(now):
            raise ClaimValidationError("claim lease expired")
        return True
