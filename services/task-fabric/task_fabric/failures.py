"""Sanitized failure values safe for durable records and broker metadata."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re


class FailureClass(str, Enum):
    INVALID_ENVELOPE = "invalid_envelope"
    UNSUPPORTED_VERSION = "unsupported_version"
    POLICY_REJECTED = "policy_rejected"
    LEASE_EXPIRED = "lease_expired"
    ATTEMPT_EXHAUSTED = "attempt_exhausted"
    TRANSIENT_DEPENDENCY = "transient_dependency"
    INTERNAL = "internal"


_DETAIL_CODE = re.compile(r"^[a-z][a-z0-9_]{0,255}$")


@dataclass(frozen=True, slots=True)
class SanitizedFailure:
    failure_class: FailureClass
    detail_code: str

    def __post_init__(self) -> None:
        if not isinstance(self.failure_class, FailureClass):
            raise ValueError("failure class must be a FailureClass value")
        if not isinstance(self.detail_code, str) or not _DETAIL_CODE.fullmatch(
            self.detail_code
        ):
            raise ValueError("failure detail must be a bounded public code")

    def to_mapping(self) -> dict[str, str]:
        return {
            "failure_class": self.failure_class.value,
            "detail_code": self.detail_code,
        }
