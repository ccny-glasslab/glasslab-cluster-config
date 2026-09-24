"""Evidence URI resolution tolerates the job-artifact URI shapes.

The cluster adapter records job artifacts under two shapes: an absolute path
(``/mnt/artifacts/<job>/metrics.json``) and a relative one
(``artifacts/<job>/metrics.json``). The evidence snapshot shows the agent
``artifact://{uri}``, so the resolver must accept both the exact shown form and
a leading-slash difference.
"""

from __future__ import annotations

from app.evidence_resolver import EvidenceURIResolver
from app.schemas import ArtifactRecord, RunCreateRequest


def _artifact(run_id: str, uri: str) -> ArtifactRecord:
    return ArtifactRecord(
        run_id=run_id,
        type='metrics',
        uri=uri,
        sha256='a' * 64,
    )


def test_resolver_matches_absolute_path_job_artifact(orchestrator_bundle) -> None:
    _, store, _, _, engine = orchestrator_bundle
    run = engine.create_run(
        RunCreateRequest(objective='Resolve an absolute job artifact URI.')
    )
    store.save_artifact(_artifact(run.run_id, '/mnt/artifacts/job-1/metrics.json'))

    result = EvidenceURIResolver(store).resolve(
        'artifact:///mnt/artifacts/job-1/metrics.json'
    )

    assert result.resolved
    assert result.resolved_to == 'artifact'


def test_resolver_matches_relative_job_artifact_exact_form(
    orchestrator_bundle,
) -> None:
    _, store, _, _, engine = orchestrator_bundle
    run = engine.create_run(
        RunCreateRequest(objective='Resolve a relative job artifact URI.')
    )
    store.save_artifact(_artifact(run.run_id, 'artifacts/job-2/metrics.json'))

    result = EvidenceURIResolver(store).resolve(
        'artifact://artifacts/job-2/metrics.json'
    )

    assert result.resolved
