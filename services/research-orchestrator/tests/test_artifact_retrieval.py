"""Operator artifact retrieval outside Discord (issue #585).

Covers the derived manifest route, the single-artifact content download
(mount confinement, symlink and traversal rejection, digest mismatch, size
bound, cross-run isolation), and scripts/fetch-run-artifacts.py driven
through the same app so the fail-closed behavior is tested end to end.
"""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import importlib.util
import json
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from app.main import create_app
from app.schemas import ArtifactRecord, RunRecord, RunState

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'fetch-run-artifacts.py'
_spec = importlib.util.spec_from_file_location('fetch_run_artifacts', SCRIPT)
assert _spec is not None and _spec.loader is not None
fetch_script = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fetch_script)

RUN_ID = 'retrieval-run-1'


def _create_run(store, run_id: str = RUN_ID) -> None:
    now = datetime.now(timezone.utc)
    store.create_run(
        RunRecord(
            run_id=run_id,
            objective='Artifact retrieval fixture.',
            state=RunState.COMPLETE,
            evaluation_contract_id='example-research-v1',
            evaluation_contract_version='1.0.0',
            evaluation_contract_digest='a' * 64,
            beaker_workspace='/tmp/beaker',
            honeydew_workspace='/tmp/honeydew',
            shared_artifacts_path='/tmp/shared',
            reports_path='/tmp/reports',
            maximum_turns=20,
            maximum_runtime_seconds=3600,
            maximum_parallel_jobs=2,
            created_at=now,
            updated_at=now,
        ),
        one_active_run=False,
    )


def _record(
    store,
    root: Path,
    relative: str,
    artifact_type: str,
    content: bytes,
    *,
    run_id: str = RUN_ID,
    job_id: str | None = None,
) -> ArtifactRecord:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return store.save_artifact(
        ArtifactRecord(
            run_id=run_id,
            job_id=job_id,
            type=artifact_type,
            uri=f'artifact://{relative}',
            sha256=sha256(content).hexdigest(),
        )
    )


@pytest.fixture
def bundle(orchestrator_bundle):
    settings, store, _, _, engine = orchestrator_bundle
    root = Path(settings.shared_mount_root)
    _create_run(store)
    base = f'research-orchestrator/runs/{RUN_ID}'
    records = {
        'report': _record(store, root, f'{base}/reports/report.md', 'report', b'# Report\n'),
        'pdf': _record(store, root, f'{base}/reports/glasslab.pdf', 'report.pdf', b'%PDF-1.7'),
        'comparison': _record(
            store, root, f'{base}/reports/comparison-abc.json', 'comparison', b'{"winner": "a"}'
        ),
        'protocol': _record(store, root, f'{base}/protocol/program.md', 'protocol', b'# Program\n'),
        'metrics': _record(
            store, root, 'ext-1/metrics.json', 'metrics.json', b'{"f1": 0.9}', job_id='job-1'
        ),
        'model': _record(
            store, root, 'ext-1/model.bin', 'model.bin', b'\x00' * 32, job_id='job-1'
        ),
    }
    app = create_app(settings, engine=engine, start_watcher=False)
    with TestClient(app) as client:
        yield client, settings, store, root, records


def _content_path(artifact: ArtifactRecord, run_id: str = RUN_ID) -> str:
    return f'/runs/{run_id}/artifacts/{artifact.artifact_id}/content'


def test_manifest_lists_every_run_and_job_artifact(bundle) -> None:
    client, _, _, _, records = bundle
    response = client.get(f'/runs/{RUN_ID}/artifacts/manifest')

    assert response.status_code == 200
    manifest = response.json()
    assert manifest['schema_version'] == 'glasslab-run-artifact-manifest-v1'
    by_id = {entry['artifact_id']: entry for entry in manifest['artifacts']}
    assert set(by_id) == {record.artifact_id for record in records.values()}
    metrics = by_id[records['metrics'].artifact_id]
    assert metrics['job_id'] == 'job-1'
    assert metrics['relative_path'] == 'ext-1/metrics.json'
    assert metrics['size_bytes'] == len(b'{"f1": 0.9}')
    assert metrics['sha256'] == records['metrics'].sha256
    assert metrics['uri'] == 'artifact://ext-1/metrics.json'
    assert all(entry['available'] for entry in manifest['artifacts'])


def test_manifest_reports_unavailable_entries_with_reason(bundle) -> None:
    client, _, _, root, records = bundle
    (root / 'ext-1/metrics.json').write_bytes(b'tampered')
    (root / 'ext-1/model.bin').unlink()

    entries = {
        entry['artifact_id']: entry
        for entry in client.get(f'/runs/{RUN_ID}/artifacts/manifest').json()['artifacts']
    }

    tampered = entries[records['metrics'].artifact_id]
    missing = entries[records['model'].artifact_id]
    assert (tampered['available'], missing['available']) == (False, False)
    assert 'digest mismatch' in tampered['reason']
    assert 'unavailable' in missing['reason']


def test_manifest_unknown_run_is_404(bundle) -> None:
    client = bundle[0]
    assert client.get('/runs/missing-run/artifacts/manifest').status_code == 404


def test_content_download_is_digest_verified(bundle) -> None:
    client, _, _, _, records = bundle
    response = client.get(_content_path(records['report']))

    assert response.status_code == 200
    assert response.content == b'# Report\n'
    assert response.headers['x-glasslab-artifact-sha256'] == records['report'].sha256
    assert 'filename="report.md"' in response.headers['content-disposition']


def test_content_rejects_artifact_from_another_run(bundle) -> None:
    client, _, store, root, records = bundle
    _create_run(store, 'other-run')
    other = _record(store, root, 'other/report.md', 'report', b'other', run_id='other-run')

    assert client.get(_content_path(other)).status_code == 404
    assert client.get(_content_path(records['report'], 'other-run')).status_code == 404


def test_content_digest_mismatch_is_409(bundle) -> None:
    client, _, _, root, records = bundle
    (root / f'research-orchestrator/runs/{RUN_ID}/reports/report.md').write_bytes(b'x')

    assert client.get(_content_path(records['report'])).status_code == 409


def test_content_rejects_traversal_and_symlink(bundle, tmp_path_factory) -> None:
    client, _, store, root, _ = bundle
    outside = tmp_path_factory.mktemp('outside') / 'secret.txt'
    outside.write_bytes(b'secret')
    digest = sha256(b'secret').hexdigest()
    escaped = store.save_artifact(
        ArtifactRecord(
            run_id=RUN_ID,
            type='report',
            uri=f'artifact://../{outside.parent.name}/secret.txt',
            sha256=digest,
        )
    )
    link = root / 'ext-1/link.txt'
    link.symlink_to(outside)
    linked = store.save_artifact(
        ArtifactRecord(
            run_id=RUN_ID,
            job_id='job-1',
            type='link.txt',
            uri='artifact://ext-1/link.txt',
            sha256=digest,
        )
    )

    assert client.get(_content_path(escaped)).status_code == 404
    assert client.get(_content_path(linked)).status_code == 404


def test_content_size_bound_is_413(orchestrator_bundle) -> None:
    settings, store, _, _, engine = orchestrator_bundle
    _create_run(store)
    big = _record(store, Path(settings.shared_mount_root), 'big.bin', 'big.bin', b'0' * 64)
    limited = settings.model_copy(update={'maximum_artifact_download_bytes': 63})
    with TestClient(create_app(limited, engine=engine, start_watcher=False)) as client:
        assert client.get(_content_path(big)).status_code == 413


def _client_fetcher(client: TestClient):
    def fetch(path: str) -> bytes:
        response = client.get(path)
        if response.status_code != 200:
            raise fetch_script.FetchError(f'{path} -> {response.status_code}')
        return response.content

    return fetch


def test_fetch_script_writes_verified_results(bundle, tmp_path) -> None:
    client = bundle[0]
    output = tmp_path / 'out'

    written = fetch_script.fetch_run(
        RUN_ID, output=output, include_jobs=False, fetch=_client_fetcher(client)
    )

    names = sorted(path.name for path in written)
    assert names == ['comparison-abc.json', 'glasslab.pdf', 'program.md', 'report.md']
    assert (output / f'research-orchestrator/runs/{RUN_ID}/reports/report.md').read_bytes() == b'# Report\n'
    manifest = json.loads((output / 'artifact-manifest.json').read_text())
    assert manifest['run_id'] == RUN_ID


def test_fetch_script_include_jobs_adds_evidence_only(bundle, tmp_path) -> None:
    client = bundle[0]

    written = fetch_script.fetch_run(
        RUN_ID, output=tmp_path / 'out', include_jobs=True, fetch=_client_fetcher(client)
    )

    names = {path.name for path in written}
    assert 'metrics.json' in names
    assert 'model.bin' not in names


def test_fetch_script_fails_closed_on_unavailable(bundle, tmp_path) -> None:
    client, _, _, root, _ = bundle
    (root / f'research-orchestrator/runs/{RUN_ID}/protocol/program.md').write_bytes(b'x')
    output = tmp_path / 'out'

    with pytest.raises(fetch_script.FetchError, match='unavailable'):
        fetch_script.fetch_run(
            RUN_ID, output=output, include_jobs=False, fetch=_client_fetcher(client)
        )
    assert not output.exists()


def test_fetch_script_fails_closed_on_served_digest_mismatch(bundle, tmp_path) -> None:
    client = bundle[0]
    fetch = _client_fetcher(client)

    def corrupting(path: str) -> bytes:
        content = fetch(path)
        return content if path.endswith('/manifest') else content + b'!'

    output = tmp_path / 'out'
    with pytest.raises(fetch_script.FetchError, match='digest mismatch'):
        fetch_script.fetch_run(
            RUN_ID, output=output, include_jobs=False, fetch=corrupting
        )
    assert not output.exists()


def test_fetch_script_requires_report_and_protocol() -> None:
    manifest = {'artifacts': [{'type': 'comparison', 'job_id': None, 'uri': 'a'}]}

    with pytest.raises(fetch_script.FetchError, match='report, protocol'):
        fetch_script.select_artifacts(manifest, include_jobs=False)


def test_fetch_script_rejects_unsafe_server_path(tmp_path) -> None:
    entry = {'relative_path': '../escape.md', 'uri': 'u'}

    with pytest.raises(fetch_script.FetchError, match='unsafe'):
        fetch_script._safe_relative(entry)
