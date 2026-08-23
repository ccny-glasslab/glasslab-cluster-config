from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from task_fabric.claims import ClaimLease, ClaimValidationError
from task_fabric.envelope import EnvelopeValidationError, TaskEnvelope
from task_fabric.failures import FailureClass, SanitizedFailure


NOW = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)


def test_envelope_round_trip_contains_identifiers_and_version_only() -> None:
    envelope = TaskEnvelope(
        task_id="task-01234567",
        task_type="orchestrator.reconcile-run",
    )
    assert envelope.to_mapping() == {
        "schema_version": 1,
        "task_id": "task-01234567",
        "task_type": "orchestrator.reconcile-run",
    }
    assert TaskEnvelope.from_mapping(envelope.to_mapping()) == envelope


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"schema_version": 2, "task_id": "task-1", "task_type": "x.y"},
        {"schema_version": 1, "task_id": "", "task_type": "x.y"},
        {"schema_version": 1, "task_id": "task-1", "task_type": "bad type"},
        {
            "schema_version": 1,
            "task_id": "task-1",
            "task_type": "x.y",
            "secret": "must-not-be-accepted",
        },
    ],
)
def test_envelope_rejects_missing_unsupported_or_extra_payload(payload: object) -> None:
    with pytest.raises(EnvelopeValidationError):
        TaskEnvelope.from_mapping(payload)


def test_claim_lease_expires_at_boundary_and_renews_without_changing_fence() -> None:
    lease = ClaimLease.issue(
        owner="worker-1",
        attempt_number=2,
        fencing_token=7,
        now=NOW,
        ttl=timedelta(seconds=30),
    )
    assert lease.is_active(NOW + timedelta(seconds=29))
    assert not lease.is_active(NOW + timedelta(seconds=30))

    renewed = lease.renew(
        owner="worker-1",
        fencing_token=7,
        now=NOW + timedelta(seconds=20),
        ttl=timedelta(seconds=30),
    )
    assert renewed.fencing_token == 7
    assert renewed.heartbeat_at == NOW + timedelta(seconds=20)
    assert renewed.expires_at == NOW + timedelta(seconds=50)


def test_claim_rejects_stale_owner_token_and_non_increasing_fences() -> None:
    lease = ClaimLease.issue(
        owner="worker-1",
        attempt_number=1,
        fencing_token=4,
        now=NOW,
        ttl=timedelta(seconds=30),
    )
    with pytest.raises(ClaimValidationError):
        lease.renew(
            owner="worker-2",
            fencing_token=4,
            now=NOW,
            ttl=timedelta(seconds=30),
        )
    with pytest.raises(ClaimValidationError):
        lease.can_complete(
            owner="worker-1",
            fencing_token=3,
            active_fencing_token=4,
            now=NOW,
        )
    assert ClaimLease.next_fencing_token(4) == 5
    with pytest.raises(ClaimValidationError):
        ClaimLease.issue(
            owner="worker-1",
            attempt_number=1,
            fencing_token=0,
            now=NOW,
            ttl=timedelta(seconds=30),
        )


def test_completion_requires_current_fence_owner_and_unexpired_lease() -> None:
    lease = ClaimLease.issue(
        owner="worker-1",
        attempt_number=1,
        fencing_token=9,
        now=NOW,
        ttl=timedelta(seconds=30),
    )
    assert lease.can_complete(
        owner="worker-1",
        fencing_token=9,
        active_fencing_token=9,
        now=NOW + timedelta(seconds=29),
    )
    with pytest.raises(ClaimValidationError):
        lease.can_complete(
            owner="worker-1",
            fencing_token=9,
            active_fencing_token=10,
            now=NOW + timedelta(seconds=29),
        )
    with pytest.raises(ClaimValidationError):
        lease.can_complete(
            owner="worker-1",
            fencing_token=9,
            active_fencing_token=9,
            now=NOW + timedelta(seconds=30),
        )


def test_envelope_rejects_malformed_field_types() -> None:
    with pytest.raises(EnvelopeValidationError):
        TaskEnvelope(task_id="task-1", task_type="x.y", schema_version=True)
    with pytest.raises(EnvelopeValidationError):
        TaskEnvelope(task_id=7, task_type="x.y")
    with pytest.raises(EnvelopeValidationError):
        TaskEnvelope(task_id="task-1", task_type=["x.y"])
    with pytest.raises(EnvelopeValidationError):
        TaskEnvelope.from_mapping(
            {"schema_version": True, "task_id": "task-1", "task_type": "x.y"}
        )


def test_claim_rejects_malformed_types() -> None:
    with pytest.raises(ClaimValidationError):
        ClaimLease.issue(
            owner=9,
            attempt_number=1,
            fencing_token=1,
            now=NOW,
            ttl=timedelta(seconds=30),
        )
    with pytest.raises(ClaimValidationError):
        ClaimLease.issue(
            owner="worker-1",
            attempt_number=True,
            fencing_token=1,
            now=NOW,
            ttl=timedelta(seconds=30),
        )
    with pytest.raises(ClaimValidationError):
        ClaimLease.issue(
            owner="worker-1",
            attempt_number="2",
            fencing_token=1,
            now=NOW,
            ttl=timedelta(seconds=30),
        )
    with pytest.raises(ClaimValidationError):
        ClaimLease.issue(
            owner="worker-1",
            attempt_number=1,
            fencing_token=True,
            now=NOW,
            ttl=timedelta(seconds=30),
        )
    with pytest.raises(ClaimValidationError):
        ClaimLease.issue(
            owner="worker-1",
            attempt_number=1,
            fencing_token=1.5,
            now=NOW,
            ttl=timedelta(seconds=30),
        )
    with pytest.raises(ClaimValidationError):
        ClaimLease.issue(
            owner="worker-1",
            attempt_number=1,
            fencing_token=1,
            now="not-a-datetime",
            ttl=timedelta(seconds=30),
        )
    with pytest.raises(ClaimValidationError):
        ClaimLease.issue(
            owner="worker-1",
            attempt_number=1,
            fencing_token=1,
            now=NOW,
            ttl="30",
        )
    with pytest.raises(ClaimValidationError):
        ClaimLease(owner="worker-1", attempt_number=1, fencing_token=1, heartbeat_at=NOW, expires_at=None)


def test_next_fencing_token_is_numeric_and_monotonic() -> None:
    assert ClaimLease.next_fencing_token(0) == 1
    assert ClaimLease.next_fencing_token(4) == 5
    assert ClaimLease.next_fencing_token(2**63) == 2**63 + 1
    for bad in ("4", 4.0, 4.5, True, False, None):
        with pytest.raises(ClaimValidationError):
            ClaimLease.next_fencing_token(bad)


def test_completion_rejects_non_integer_fencing_tokens() -> None:
    lease = ClaimLease.issue(
        owner="worker-1",
        attempt_number=1,
        fencing_token=9,
        now=NOW,
        ttl=timedelta(seconds=30),
    )
    for bad in (9.0, "9", True):
        with pytest.raises(ClaimValidationError):
            lease.can_complete(
                owner="worker-1",
                fencing_token=bad,
                active_fencing_token=9,
                now=NOW,
            )
        with pytest.raises(ClaimValidationError):
            lease.can_complete(
                owner="worker-1",
                fencing_token=9,
                active_fencing_token=bad,
                now=NOW,
            )
        with pytest.raises(ClaimValidationError):
            lease.renew(
                owner="worker-1",
                fencing_token=bad,
                now=NOW,
                ttl=timedelta(seconds=30),
            )


def test_sanitized_failure_accepts_only_bounded_public_detail() -> None:
    failure = SanitizedFailure(
        failure_class=FailureClass.LEASE_EXPIRED,
        detail_code="claim_lease_expired",
    )
    assert failure.to_mapping() == {
        "failure_class": "lease_expired",
        "detail_code": "claim_lease_expired",
    }
    with pytest.raises(ValueError):
        SanitizedFailure(FailureClass.INTERNAL, "password=secret")
    with pytest.raises(ValueError):
        SanitizedFailure(FailureClass.INTERNAL, "x" * 257)


def test_sanitized_failure_rejects_malformed_types() -> None:
    with pytest.raises(ValueError):
        SanitizedFailure(FailureClass.INTERNAL, 5)
    with pytest.raises(ValueError):
        SanitizedFailure("lease_expired", "claim_lease_expired")
    failure = SanitizedFailure(FailureClass.ATTEMPT_EXHAUSTED, "attempt_ceiling")
    assert failure.to_mapping() == {
        "failure_class": "attempt_exhausted",
        "detail_code": "attempt_ceiling",
    }
