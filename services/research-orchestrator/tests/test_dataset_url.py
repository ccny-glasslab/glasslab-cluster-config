"""Offline regression tests for first-class public-URL dataset ingestion.

The orchestrator must fetch public HTTPS only, revalidate every redirect hop
and the connected peer, stream under the byte ceiling, dedup by content across
upload and URL paths, and fail closed on a checksum mismatch. Every test mocks
the network via httpx transports and a patched resolver, so the suite needs no
outbound access.
"""

from __future__ import annotations

from hashlib import sha256
from io import BytesIO
from pathlib import Path
import socket

from fastapi.testclient import TestClient
import httpx
import pytest

from app.cluster import FakeClusterExecutor
from app.config import SERVICE_ROOT, Settings
from app.contract_candidates import ContractCandidateManager
from app.contracts import EvaluationContractResolver
from app.datasets import (
    DatasetIngestionError,
    DatasetIngestionManager,
    DatasetUrlError,
)
from app.discord_adapter import DisabledDiscordAdapter
from app.engine import ResearchOrchestrator
from app.main import create_app
from app.mock_runtime import ScriptedMockRuntime
from app.policy import ActionPolicy
from app.schemas import CatalogDatasetRecord
from app.storage import SqliteStore
from app.url_fetch import UrlFetchErrorKind
from app.workspaces import WorkspaceManager
from conftest import RUNNER_IMAGE, create_test_repo


_PUBLIC_PEER = ('93.184.216.34', 443)
_PRIVATE_PEER = ('169.254.169.254', 443)


class _FakeNetworkStream:
    def __init__(self, server_addr: tuple[str, int]) -> None:
        self._server_addr = server_addr

    def get_extra_info(self, info: str):
        if info == 'server_addr':
            return self._server_addr
        return None


def _public_getaddrinfo(*args, **kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', _PUBLIC_PEER)]


def _private_getaddrinfo(*args, **kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 443))]


def _host_getaddrinfo(*, private_hosts: set[str]):
    def resolve(host, *args, **kwargs):
        address = _PRIVATE_PEER if host in private_hosts else _PUBLIC_PEER
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', address)]

    return resolve


def _manager(
    tmp_path: Path,
    *,
    maximum_bytes: int = 1024,
    transport: httpx.BaseTransport | None = None,
) -> DatasetIngestionManager:
    return DatasetIngestionManager(
        store=SqliteStore(str(tmp_path / 'orchestrator.db')),
        root=str(tmp_path / 'dataset-uploads'),
        shared_mount_root=str(tmp_path),
        maximum_bytes=maximum_bytes,
        transport=transport,
    )


def _ok_transport(
    body: bytes,
    *,
    headers: dict[str, str] | None = None,
    peer: tuple[str, int] = _PUBLIC_PEER,
) -> httpx.BaseTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=body,
            headers=headers or {},
            extensions={'network_stream': _FakeNetworkStream(peer)},
        )

    return httpx.MockTransport(handler)


def _failing_transport(error: httpx.TransportError) -> httpx.BaseTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        raise error

    return httpx.MockTransport(handler)


def _error_kind(excinfo: pytest.ExceptionInfo[DatasetUrlError]):
    return excinfo.value.kind


def test_register_url_happy_path_captures_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr('app.url_fetch.socket.getaddrinfo', _public_getaddrinfo)
    body = b'feature,label\n1,0\n'
    digest = sha256(body).hexdigest()
    manager = _manager(
        tmp_path,
        transport=_ok_transport(
            body,
            headers={'content-type': 'text/csv; charset=utf-8'},
        ),
    )

    record = manager.register_url(
        url='https://example.com/data/train.csv',
        name='train_data',
        role='train',
        contains_labels=True,
        expected_sha256=digest,
        created_by='discord:tester',
    )

    assert record.provenance == 'url'
    assert record.reference_uri == f'glasslab-dataset://{digest}'
    assert record.sha256 == digest
    assert record.size_bytes == len(body)
    assert record.source_url == 'https://example.com/data/train.csv'
    assert record.final_url == 'https://example.com/data/train.csv'
    assert record.filename == 'train.csv'
    assert record.media_type == 'text/csv'
    assert record.upstream_sha256 == digest
    assert record.deduplicated is False
    assert record.retrieved_at is not None
    assert Path(manager.store.get_dataset(digest).path).stat().st_mode & 0o222 == 0


def test_upload_and_url_converge_on_same_dataset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr('app.url_fetch.socket.getaddrinfo', _public_getaddrinfo)
    body = b'shared-bytes\n'
    digest = sha256(body).hexdigest()
    manager = _manager(tmp_path, transport=_ok_transport(body))

    uploaded = manager.register_upload(
        BytesIO(body),
        filename='local.txt',
        name='local_copy',
        role='input',
    )
    fetched = manager.register_url(
        url='https://example.com/remote.txt',
        name='remote_copy',
        role='input',
    )

    assert fetched.reference_uri == uploaded.reference_uri
    assert fetched.sha256 == digest
    assert fetched.deduplicated is True
    assert len(manager.store.list_datasets()) == 1


def test_reingesting_same_content_does_not_duplicate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr('app.url_fetch.socket.getaddrinfo', _public_getaddrinfo)
    manager = _manager(tmp_path, transport=_ok_transport(b'stable\n'))

    first = manager.register_url(
        url='https://example.com/a.bin',
        name='stable_data',
        role='input',
    )
    second = manager.register_url(
        url='https://example.com/a.bin',
        name='stable_data',
        role='input',
    )

    assert second.catalog_id == first.catalog_id
    assert second.deduplicated is True
    assert len(manager.store.list_catalog_datasets()) == 1
    assert len(manager.store.list_datasets()) == 1


def test_same_name_different_content_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr('app.url_fetch.socket.getaddrinfo', _public_getaddrinfo)

    def handler(request: httpx.Request) -> httpx.Response:
        body = b'one\n' if request.url.path == '/one' else b'two\n'
        return httpx.Response(
            200,
            content=body,
            extensions={'network_stream': _FakeNetworkStream(_PUBLIC_PEER)},
        )

    manager = _manager(tmp_path, transport=httpx.MockTransport(handler))
    manager.register_url(
        url='https://example.com/one',
        name='clashing',
        role='input',
    )
    with pytest.raises(DatasetIngestionError, match='already registered'):
        manager.register_url(
            url='https://example.com/two',
            name='clashing',
            role='input',
        )


def test_checksum_mismatch_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr('app.url_fetch.socket.getaddrinfo', _public_getaddrinfo)
    manager = _manager(tmp_path, transport=_ok_transport(b'actual\n'))

    with pytest.raises(DatasetUrlError) as excinfo:
        manager.register_url(
            url='https://example.com/data.bin',
            name='bad_checksum',
            expected_sha256='f' * 64,
        )

    assert _error_kind(excinfo) is UrlFetchErrorKind.CHECKSUM_MISMATCH
    assert 'does not match' in str(excinfo.value)
    assert manager.store.list_datasets() == []


def test_private_target_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr('app.url_fetch.socket.getaddrinfo', _private_getaddrinfo)
    manager = _manager(tmp_path, transport=_ok_transport(b'secret'))

    with pytest.raises(DatasetUrlError) as excinfo:
        manager.register_url(
            url='https://internal.example.com/data',
            name='private_data',
        )

    assert _error_kind(excinfo) is UrlFetchErrorKind.PRIVATE_TARGET
    assert '127.0.0.1' not in str(excinfo.value)


def test_non_public_connected_peer_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Resolution returns a public address, but the connection lands on a
    # link-local peer (DNS rebinding): the fetch aborts before reading bytes.
    monkeypatch.setattr('app.url_fetch.socket.getaddrinfo', _public_getaddrinfo)
    manager = _manager(
        tmp_path,
        transport=_ok_transport(b'secret', peer=_PRIVATE_PEER),
    )

    with pytest.raises(DatasetUrlError) as excinfo:
        manager.register_url(
            url='https://rebound.example.com/data',
            name='rebound_data',
        )

    assert _error_kind(excinfo) is UrlFetchErrorKind.PRIVATE_TARGET
    assert _PRIVATE_PEER[0] not in str(excinfo.value)
    assert manager.store.list_datasets() == []


def test_size_overflow_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr('app.url_fetch.socket.getaddrinfo', _public_getaddrinfo)
    manager = _manager(tmp_path, maximum_bytes=4, transport=_ok_transport(b'12345'))

    with pytest.raises(DatasetUrlError) as excinfo:
        manager.register_url(
            url='https://example.com/big.bin',
            name='too_big',
        )

    assert _error_kind(excinfo) is UrlFetchErrorKind.SIZE_EXCEEDED
    assert manager.store.list_datasets() == []


def test_empty_body_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr('app.url_fetch.socket.getaddrinfo', _public_getaddrinfo)
    manager = _manager(tmp_path, transport=_ok_transport(b''))

    with pytest.raises(DatasetUrlError) as excinfo:
        manager.register_url(
            url='https://example.com/empty.bin',
            name='empty_data',
        )

    assert _error_kind(excinfo) is UrlFetchErrorKind.EMPTY_BODY
    assert manager.store.list_datasets() == []


def test_redirect_without_location_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr('app.url_fetch.socket.getaddrinfo', _public_getaddrinfo)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302,
            headers={},
            extensions={'network_stream': _FakeNetworkStream(_PUBLIC_PEER)},
        )

    manager = _manager(tmp_path, transport=httpx.MockTransport(handler))
    with pytest.raises(DatasetUrlError) as excinfo:
        manager.register_url(
            url='https://example.com/redirect',
            name='redirected',
        )

    assert _error_kind(excinfo) is UrlFetchErrorKind.REDIRECT_REJECTED


def test_redirect_to_private_target_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        'app.url_fetch.socket.getaddrinfo',
        _host_getaddrinfo(private_hosts={'169.254.169.254'}),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            301,
            headers={'location': 'https://169.254.169.254/secret'},
            extensions={'network_stream': _FakeNetworkStream(_PUBLIC_PEER)},
        )

    manager = _manager(tmp_path, transport=httpx.MockTransport(handler))
    with pytest.raises(DatasetUrlError) as excinfo:
        manager.register_url(
            url='https://example.com/redirect',
            name='rebound_redirect',
        )

    assert _error_kind(excinfo) is UrlFetchErrorKind.PRIVATE_TARGET
    assert manager.store.list_datasets() == []


def test_transport_failure_is_classified_without_detail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr('app.url_fetch.socket.getaddrinfo', _public_getaddrinfo)
    manager = _manager(
        tmp_path,
        transport=_failing_transport(
            httpx.ConnectError('connection refused to internal host')
        ),
    )

    with pytest.raises(DatasetUrlError) as excinfo:
        manager.register_url(
            url='https://example.com/unreachable',
            name='unreachable',
        )

    assert _error_kind(excinfo) is UrlFetchErrorKind.NETWORK_FAILURE
    assert str(excinfo.value) == 'dataset URL fetch failed'
    assert 'internal host' not in str(excinfo.value)


@pytest.mark.parametrize(
    'bad_url',
    [
        'http://example.com/data.csv',
        'file:///etc/passwd',
        'https://user:pass@example.com/data.csv',
        'https://example.com:8443/data.csv',
    ],
)
def test_malformed_urls_are_rejected(tmp_path: Path, bad_url: str) -> None:
    manager = _manager(tmp_path, transport=_ok_transport(b'x'))

    with pytest.raises(DatasetUrlError) as excinfo:
        manager.register_url(url=bad_url, name='bad_url')

    assert _error_kind(excinfo) is UrlFetchErrorKind.MALFORMED_URL


def test_catalog_record_round_trips_provenance_fields(tmp_path: Path) -> None:
    store = SqliteStore(str(tmp_path / 'orchestrator.db'))
    record = CatalogDatasetRecord(
        name='provenance_data',
        reference_uri=f'glasslab-dataset://{"a" * 64}',
        artifact_uri='s3://artifacts/dataset-uploads/abc/data.csv',
        sha256='a' * 64,
        size_bytes=42,
        provenance='url',
        source_url='https://example.com/old.csv',
        final_url='https://cdn.example.com/new.csv',
        media_type='text/csv',
        filename='data.csv',
        upstream_sha256='b' * 64,
        deduplicated=True,
        created_by='operator',
    )

    stored = store.save_catalog_dataset(record)
    loaded = store.get_catalog_dataset(stored.catalog_id)

    assert loaded.final_url == 'https://cdn.example.com/new.csv'
    assert loaded.media_type == 'text/csv'
    assert loaded.filename == 'data.csv'
    assert loaded.upstream_sha256 == 'b' * 64
    assert loaded.deduplicated is True
    assert loaded.retrieved_at is None


def _api_settings(tmp_path: Path, *, require_auth: bool = False) -> Settings:
    repo = create_test_repo(tmp_path)
    return Settings(
        database_path=str(tmp_path / 'api.db'),
        workspace_root=str(tmp_path / 'runs'),
        artifact_root=str(tmp_path / 'artifacts'),
        approved_repo_path=str(repo),
        approved_repo_ref='main',
        evaluation_contract_root=str(SERVICE_ROOT / 'evaluation-contracts'),
        permitted_job_images=[RUNNER_IMAGE],
        cluster_execution_mode='fake',
        promoted_contract_root=str(tmp_path / 'trusted-contracts'),
        sealed_contract_candidate_root=str(tmp_path / 'contract-candidates'),
        trusted_contract_catalog_path=str(
            tmp_path / 'trusted-contracts' / 'catalog.json'
        ),
        shared_mount_root=str(tmp_path),
        task_bundle_root=str(tmp_path / 'task-bundles'),
        task_asset_root=str(tmp_path / 'task-assets'),
        dataset_upload_root=str(tmp_path / 'dataset-uploads'),
        benchmark_dataset_catalog_path=str(tmp_path / 'datasets' / 'catalog.json'),
        one_active_run=False,
        maximum_parallel_jobs=2,
        require_operator_auth=require_auth,
        operator_api_token='test-token' if require_auth else None,
    )


def _api_engine(
    tmp_path: Path,
    settings: Settings,
    manager: DatasetIngestionManager,
) -> ResearchOrchestrator:
    return ResearchOrchestrator(
        settings=settings,
        store=manager.store,
        runtime=ScriptedMockRuntime(runner_image=RUNNER_IMAGE),
        workspaces=WorkspaceManager(
            workspace_root=settings.workspace_root,
            approved_repo_path=settings.approved_repo_path,
            approved_repo_ref=settings.approved_repo_ref,
        ),
        contracts=EvaluationContractResolver(
            settings.promoted_contract_root,
            fallback_roots=[settings.evaluation_contract_root],
        ),
        contract_candidates=ContractCandidateManager(
            sealed_root=settings.sealed_contract_candidate_root,
            promoted_root=settings.promoted_contract_root,
            catalog_path=settings.trusted_contract_catalog_path,
            shared_mount_root=settings.shared_mount_root,
        ),
        policy=ActionPolicy(
            permitted_images=settings.permitted_job_images,
            maximum_cpu=settings.maximum_cpu,
            maximum_memory_gib=settings.maximum_memory_gib,
            maximum_gpus=settings.maximum_gpus,
            maximum_parallel_jobs=settings.maximum_parallel_jobs,
        ),
        cluster=FakeClusterExecutor(),
        discord=DisabledDiscordAdapter(),
        datasets=manager,
    )


def test_register_url_endpoint_happy_path_and_error_kind(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr('app.url_fetch.socket.getaddrinfo', _public_getaddrinfo)
    settings = _api_settings(tmp_path)
    manager = _manager(
        tmp_path,
        transport=_ok_transport(
            b'api-body\n',
            headers={'content-type': 'application/octet-stream'},
        ),
    )
    engine = _api_engine(tmp_path, settings, manager)
    app = create_app(settings, engine=engine, start_watcher=False)

    with TestClient(app) as client:
        ok = client.post(
            '/datasets/register-url',
            json={'name': 'api_data', 'url': 'https://example.com/api.bin'},
        )
        assert ok.status_code == 200, ok.text
        body = ok.json()
        assert body['provenance'] == 'url'
        assert body['source_url'] == 'https://example.com/api.bin'
        assert body['final_url'] == 'https://example.com/api.bin'
        assert body['media_type'] == 'application/octet-stream'

        mismatch = client.post(
            '/datasets/register-url',
            json={
                'name': 'api_mismatch',
                'url': 'https://example.com/api.bin',
                'expected_sha256': 'f' * 64,
            },
        )
        assert mismatch.status_code == 409
        assert mismatch.json()['detail']['kind'] == 'checksum_mismatch'
        assert 'does not match' in mismatch.json()['detail']['message']


def test_register_url_endpoint_requires_operator_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr('app.url_fetch.socket.getaddrinfo', _public_getaddrinfo)
    settings = _api_settings(tmp_path, require_auth=True)
    manager = _manager(tmp_path, transport=_ok_transport(b'x'))
    engine = _api_engine(tmp_path, settings, manager)
    app = create_app(settings, engine=engine, start_watcher=False)

    with TestClient(app) as client:
        unauthenticated = client.post(
            '/datasets/register-url',
            json={'name': 'needs_auth', 'url': 'https://example.com/x.bin'},
        )
        assert unauthenticated.status_code == 401

        authenticated = client.post(
            '/datasets/register-url',
            headers={'X-Glasslab-Operator-Token': 'test-token'},
            json={'name': 'needs_auth', 'url': 'https://example.com/x.bin'},
        )
        assert authenticated.status_code == 200
