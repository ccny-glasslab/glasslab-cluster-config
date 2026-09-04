"""Verification preflight: deterministic claim-to-evidence URI resolution.

Hyperplan T14 encodes the policy that independent verification is satisfied by
the deterministic claim-to-evidence layer (T3), not by prompt-only same-model
verification. The verification preflight resolves every evidence URI cited by
a candidate's claims against the authoritative store tables before the
verification evidence is accepted; fabricated or non-existent URIs fail the
preflight with a specific error listing the offending URIs.
"""

from __future__ import annotations

from hashlib import sha256

from app.preflight import preflight_verification_evidence
from app.schemas import ArtifactRecord, Claim, RunCreateRequest


def _save_artifact(store, run_id: str, *, uri: str) -> str:
    store.save_artifact(
        ArtifactRecord(
            run_id=run_id,
            type='runner_log',
            uri=uri,
            sha256=sha256(b'authoritative content').hexdigest(),
        )
    )
    return f'artifact://{run_id}/artifacts/{uri}'


def test_verification_preflight_rejects_unresolved_evidence_uris(
    orchestrator_bundle,
) -> None:
    """Given claims citing URIs that do not resolve, the verification preflight
    fails with a specific 'evidence URI unresolved' error listing the offending
    URIs."""
    _, store, _, _, engine = orchestrator_bundle
    run = engine.create_run(
        RunCreateRequest(objective='Reject fabricated verification evidence.')
    )
    missing_artifact = f'artifact://{run.run_id}/artifacts/missing.json'
    claims = [
        Claim(
            text='The workload produced the expected metrics.',
            evidence=[missing_artifact, 'job://missing-job'],
        )
    ]

    report = preflight_verification_evidence(claims=claims, store=store)

    assert not report.passed
    assert any('evidence URI unresolved' in error for error in report.errors)
    assert missing_artifact in report.unresolved_uris
    assert 'job://missing-job' in report.unresolved_uris


def test_verification_preflight_accepts_resolving_evidence_uris(
    orchestrator_bundle,
) -> None:
    """Given claims citing URIs that resolve to authoritative records, the
    verification preflight passes."""
    _, store, _, _, engine = orchestrator_bundle
    run = engine.create_run(
        RunCreateRequest(objective='Accept authoritative verification evidence.')
    )
    resolving_uri = _save_artifact(store, run.run_id, uri='artifacts/job-1/runner.log')
    claims = [
        Claim(
            text='Verified against the authoritative record.',
            evidence=[resolving_uri],
        )
    ]

    report = preflight_verification_evidence(claims=claims, store=store)

    assert report.passed
    assert report.errors == []
    assert report.unresolved_uris == []