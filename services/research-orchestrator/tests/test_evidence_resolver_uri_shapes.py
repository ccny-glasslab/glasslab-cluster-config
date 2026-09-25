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


def test_resolver_matches_absolute_citation_for_relative_stored_artifact(
    orchestrator_bundle,
) -> None:
    # The workflow-api directory scan records evaluation.json as a
    # mount-relative path while the agent cites the absolute shared-volume path
    # it read; both spellings must resolve to the same artifact.
    _, store, _, _, engine = orchestrator_bundle
    run = engine.create_run(
        RunCreateRequest(objective='Resolve a cross-form evaluation URI.')
    )
    store.save_artifact(
        _artifact(run.run_id, 'artifacts/eval-run-1/evaluation.json')
    )

    result = EvidenceURIResolver(store).resolve(
        'artifact:///mnt/artifacts/eval-run-1/evaluation.json'
    )

    assert result.resolved
    assert result.resolved_to == 'artifact'


class _FakeContract:
    def __init__(self, digest: str) -> None:
        self.digest = digest


class _FakeContracts:
    def __init__(self, digest: str) -> None:
        self._digest = digest

    def resolve(self, contract_id: str, version: str) -> _FakeContract:
        if (contract_id, version) != (
            'titanic-survival-methodology-v1',
            '1.0.11',
        ):
            raise KeyError(f'{contract_id}/{version}')
        return _FakeContract(self._digest)


def test_resolver_resolves_contract_uri_with_matching_digest(
    orchestrator_bundle,
) -> None:
    _, store, _, _, _ = orchestrator_bundle
    digest = 'b' * 64
    resolver = EvidenceURIResolver(store, contracts=_FakeContracts(digest))

    result = resolver.resolve(
        f'contract://titanic-survival-methodology-v1/1.0.11@{digest}'
    )

    assert result.resolved
    assert result.resolved_to == 'contract'


def test_resolver_rejects_contract_uri_with_wrong_digest(
    orchestrator_bundle,
) -> None:
    _, store, _, _, _ = orchestrator_bundle
    resolver = EvidenceURIResolver(store, contracts=_FakeContracts('b' * 64))

    result = resolver.resolve(
        f'contract://titanic-survival-methodology-v1/1.0.11@{"c" * 64}'
    )

    assert not result.resolved
    assert 'digest mismatch' in (result.error or '')


def test_resolver_contract_uri_without_resolver_is_unresolved(
    orchestrator_bundle,
) -> None:
    _, store, _, _, _ = orchestrator_bundle

    result = EvidenceURIResolver(store).resolve(
        'contract://titanic-survival-methodology-v1/1.0.11'
    )

    assert not result.resolved
    assert 'unavailable' in (result.error or '')


class _JobStoreStub:
    def __init__(self, jobs) -> None:
        self._jobs = {job.job_id: job for job in jobs}
        self._by_run: dict[str, list] = {}
        for job in jobs:
            self._by_run.setdefault(job.run_id, []).append(job)

    def get_job(self, job_id: str):
        if job_id not in self._jobs:
            raise ValueError(job_id)
        return self._jobs[job_id]

    def list_jobs(self, run_id: str):
        return self._by_run.get(run_id, [])


def _job_stub(**kwargs) -> object:
    from types import SimpleNamespace

    return SimpleNamespace(**kwargs)


def test_resolver_resolves_compound_job_run_action_uri() -> None:
    # An agent may cite job://<run_id>/<action_id> to reference the approval
    # action whose jobs carried the comparison; it must resolve to a job of
    # that run under that action.
    store = _JobStoreStub(
        [_job_stub(job_id='job-1', run_id='run-1', action_id='action-1')]
    )

    result = EvidenceURIResolver(store).resolve('job://run-1/action-1')

    assert result.resolved
    assert result.resolved_to == 'job'
    assert result.record_id == 'job-1'


def test_resolver_resolves_compound_job_run_job_uri() -> None:
    store = _JobStoreStub(
        [_job_stub(job_id='job-1', run_id='run-1', action_id='action-1')]
    )

    result = EvidenceURIResolver(store).resolve('job://run-1/job-1')

    assert result.resolved
    assert result.record_id == 'job-1'


def test_resolver_rejects_unknown_compound_job_uri() -> None:
    store = _JobStoreStub(
        [_job_stub(job_id='job-1', run_id='run-1', action_id='action-1')]
    )

    result = EvidenceURIResolver(store).resolve('job://run-1/action-missing')

    assert not result.resolved
    assert 'job not found' in (result.error or '')
