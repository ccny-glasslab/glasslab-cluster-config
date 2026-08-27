"""Strict, identifier-only broker envelopes."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Mapping


class EnvelopeValidationError(ValueError):
    """An envelope is malformed or uses an unsupported schema."""


_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,254}$")
_TASK_TYPE = re.compile(r"^[a-z][a-z0-9-]*(?:\.[a-z][a-z0-9-]*)+$")
_FIELDS = frozenset({"schema_version", "task_id", "task_type"})


@dataclass(frozen=True, slots=True)
class TaskEnvelope:
    task_id: str
    task_type: str
    schema_version: int = 1

    def __post_init__(self) -> None:
        if (
            isinstance(self.schema_version, bool)
            or not isinstance(self.schema_version, int)
            or self.schema_version != 1
        ):
            raise EnvelopeValidationError("unsupported envelope schema version")
        if not isinstance(self.task_id, str) or not _IDENTIFIER.fullmatch(self.task_id):
            raise EnvelopeValidationError("invalid task identifier")
        if not isinstance(self.task_type, str) or not _TASK_TYPE.fullmatch(self.task_type):
            raise EnvelopeValidationError("invalid task type")

    def to_mapping(self) -> dict[str, str | int]:
        return {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "task_type": self.task_type,
        }

    @classmethod
    def from_mapping(cls, value: object) -> "TaskEnvelope":
        if not isinstance(value, Mapping) or set(value) != _FIELDS:
            raise EnvelopeValidationError("envelope must contain exactly the v1 fields")
        try:
            return cls(
                schema_version=value["schema_version"],
                task_id=value["task_id"],
                task_type=value["task_type"],
            )
        except (TypeError, EnvelopeValidationError) as exc:
            if isinstance(exc, EnvelopeValidationError):
                raise
            raise EnvelopeValidationError("invalid envelope field type") from exc
