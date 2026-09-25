"""Task-bundle import, dataset binding, and deployment preflight.

Covers immutable/idempotent archive compilation, path-traversal rejection,
rebinding persisted tasks to the fixed workload runner image, uploaded
dataset resolution (immutable, tamper-rejected) into a task, and preflight
gating on missing inputs and public asset URLs.
"""

from __future__ import annotations

from hashlib import sha256
import io
from pathlib import Path
import socket
import zipfile

import httpx
import pytest

from app.schemas import TaskAssetProposal, TaskSpecProposal
from app.config import Settings
from app.datasets import DatasetIngestionManager
from app.storage import SqliteStore
from app.task_bundles import (
    FIXED_WORKLOAD_RUNNER_IMAGES,
    RUNTIME_PROFILES,
    TaskAssetFetcher,
    TaskBundleError,
    TaskBundleManager,
)
from app.workspaces import WorkspaceManager


def _problem_markdown() -> str:
    return (
        '# Adult Income Classification\n\n'
        '## Objective\n'
        'Predict whether an adult earns more than 50K a year.\n\n'
        '## Inputs\n'
        '- Dataset: glasslab-dataset://' + 'a' * 64 + '\n\n'
        '## Method and architecture\n'
        '- Logistic regression over the tabular features.\n\n'
        '## Hyperparameter search space (exact)\n'
        '- C: {0.1, 1.0}; solver: lbfgs.\n\n'
        '## Evaluation rubric (exact)\n'
        '- accuracy >= 0.78; stop after the approved matrix completes.\n\n'
        '## Evidence artifacts (required)\n'
        '- metrics.json, report.md, tables/\n'
    )


def _archive(
    *,
    unsafe_name: str | None = None,
    problem: str | None = None,
) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as handle:
        handle.writestr(
            unsafe_name or 'ML_Benchmark_Adult_Income/problem.md',
            problem if problem is not None else _problem_markdown(),
        )
        handle.writestr(
            'ML_Benchmark_Adult_Income/eval_agent_prompt.md',
            '# Rubric\n',
        )
    return output.getvalue()


def _manager(tmp_path: Path) -> TaskBundleManager:
    catalog_path = tmp_path / 'catalog.json'
    catalog_path.write_text('{}')
    return TaskBundleManager(
        root=str(tmp_path / 'task-bundles'),
        shared_mount_root=str(tmp_path),
        dataset_catalog_path=str(catalog_path),
        task_asset_root=str(tmp_path / 'task-assets'),
    )


def test_import_task_bundle_is_immutable_and_idempotent(tmp_path: Path) -> None:
    # Recompiling identical bytes must return the exact same record, and the
    # on-disk bundle is read-only (mode without write bits), so the compiled
    # task can never drift after the digest is pinned.
    manager = _manager(tmp_path)
    content = _archive()
    proposal = TaskSpecProposal(
        schema_version='glasslab-task-spec-v1',
        display_name='Adult Income Classification',
        runtime_profile='cpu-ml-standard-v1',
        required_artifacts=['tables/metrics.csv'],
        required_metric_keys=['accuracy'],
        rationale='Small tabular classification task.',
    )
    first = manager.compile(manager.stage_archive(
        filename='ML_Benchmark_Adult_Income.zip',
        content=content,
    ), proposal)
    second = manager.compile(manager.stage_archive(
        filename='ML_Benchmark_Adult_Income.zip',
        content=content,
    ), proposal)
    assert first == second
    assert first.digest == sha256(content).hexdigest()
    assert Path(first.problem_path).read_text() == _problem_markdown()
    assert Path(first.problem_path).stat().st_mode & 0o222 == 0
    assert first.compilation_source == 'honeydew-task-spec'
    assert first.workload_id == 'workspace-cpu-ml-v1'
    assert first.datasets == []


def test_default_settings_accept_compiled_workspace_runtime(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    proposal = TaskSpecProposal(
        schema_version='glasslab-task-spec-v1',
        display_name='Default Runtime Task',
        runtime_profile='cpu-ml-standard-v1',
        rationale='Verify defaults and compiled policy cannot drift.',
    )
    record = manager.compile(
        manager.stage_archive(filename='task.zip', content=_archive()),
        proposal,
    )

    preflight = manager.preflight(
        record,
        permitted_images=set(Settings().permitted_job_images),
        evaluator_ready=True,
    )

    assert preflight.ready


def test_loading_persisted_task_rebinds_fixed_workload_runner(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    content = _archive()
    proposal = TaskSpecProposal(
        schema_version='glasslab-task-spec-v1',
        display_name='Persisted CPU Task',
        runtime_profile='cpu-ml-standard-v1',
        rationale='Exercise deployment-policy rebinding.',
    )
    record = manager.compile(
        manager.stage_archive(filename='task.zip', content=content),
        proposal,
    )
    metadata_path = Path(record.archive_path).with_name('task.json')
    metadata_path.chmod(0o644)
    metadata_path.write_text(
        record.model_copy(
            update={
                'workload_id': 'benchmark-workspace-cpu-v1',
                'runner_image': (
                    'ghcr.io/ccny-glasslab/'
                    'glasslab-research-workspace-runner:benchmark-cpu-v1'
                ),
            }
        ).model_dump_json(indent=2)
    )

    loaded = manager.get(record.task_id, record.digest)

    assert loaded.runner_image == FIXED_WORKLOAD_RUNNER_IMAGES[
        'benchmark-workspace-cpu-v1'
    ]


def test_loading_persisted_task_rebinds_fixed_gpu_workload_runner(
    tmp_path: Path,
) -> None:
    # A legacy GPU benchmark task must rebind to the current pinned GPU image,
    # exactly like the CPU benchmark does; otherwise it keeps a stale non-ccny
    # image and can never pass preflight.
    manager = _manager(tmp_path)
    content = _archive()
    proposal = TaskSpecProposal(
        schema_version='glasslab-task-spec-v1',
        display_name='Persisted GPU Task',
        runtime_profile='gpu-ml-standard-v1',
        rationale='Exercise GPU deployment-policy rebinding.',
    )
    record = manager.compile(
        manager.stage_archive(filename='task.zip', content=content),
        proposal,
    )
    metadata_path = Path(record.archive_path).with_name('task.json')
    metadata_path.chmod(0o644)
    metadata_path.write_text(
        record.model_copy(
            update={
                'workload_id': 'benchmark-workspace-gpu-v1',
                'runner_image': (
                    'ghcr.io/offensivegeneric/'
                    'glasslab-research-workspace-runner:benchmark-gpu-v1'
                ),
            }
        ).model_dump_json(indent=2)
    )

    loaded = manager.get(record.task_id, record.digest)

    assert loaded.runner_image == FIXED_WORKLOAD_RUNNER_IMAGES[
        'benchmark-workspace-gpu-v1'
    ]
    assert loaded.runner_image == FIXED_WORKLOAD_RUNNER_IMAGES[
        'workspace-gpu-ml-v1'
    ]


def test_import_task_bundle_rejects_path_traversal(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    with pytest.raises(TaskBundleError, match='unsafe'):
        manager.stage_archive(
            filename='ML_Benchmark_Adult_Income.zip',
            content=_archive(unsafe_name='../problem.md'),
        )


def test_import_rejects_problem_missing_required_section(tmp_path: Path) -> None:
    # Issue #496: a missing mandatory section is rejected at import with the
    # section named, instead of being silently reinterpreted by the compiler.
    manager = _manager(tmp_path)
    problem = _problem_markdown().replace(
        '## Evaluation rubric (exact)',
        '## Scoring notes',
    )
    with pytest.raises(TaskBundleError) as excinfo:
        manager.stage_archive(
            filename='ML_Benchmark_Adult_Income.zip',
            content=_archive(problem=problem),
        )
    message = str(excinfo.value)
    assert 'Evaluation rubric' in message
    assert 'missing' in message


def test_import_rejects_problem_with_empty_required_section(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    problem = _problem_markdown().replace(
        'Predict whether an adult earns more than 50K a year.',
        '',
    )
    with pytest.raises(TaskBundleError) as excinfo:
        manager.stage_archive(
            filename='ML_Benchmark_Adult_Income.zip',
            content=_archive(problem=problem),
        )
    message = str(excinfo.value)
    assert 'Objective' in message
    assert 'empty' in message


def test_import_accepts_reordered_reformatted_required_sections(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    problem = (
        '# Adult task\n\n'
        '### Evidence artifacts (REQUIRED):\n'
        '- metrics.json\n\n'
        '## method and architecture:\n'
        'Logistic regression.\n\n'
        '# Objective:\n'
        'Classify adult income.\n\n'
        '## INPUTS\n'
        '- glasslab-dataset://' + 'a' * 64 + '\n\n'
        '###### Hyperparameter search space\n'
        '- C: {0.1, 1.0}\n\n'
        '## Evaluation Rubric\n'
        '- accuracy >= 0.78\n'
    )
    staged = manager.stage_archive(
        filename='ML_Benchmark_Adult_Income.zip',
        content=_archive(problem=problem),
    )
    assert staged.problem_path.read_text() == problem


def test_task_preflight_reports_missing_inputs(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    proposal = TaskSpecProposal(
        schema_version='glasslab-task-spec-v1',
        display_name='Needs Private Data',
        runtime_profile='gpu-ml-standard-v1',
        missing_inputs=['private training split must be supplied'],
        rationale='The requested dataset has no approved public source.',
    )
    record = manager.compile(
        manager.stage_archive(filename='anything.zip', content=_archive()),
        proposal,
    )
    preflight = manager.preflight(
        record,
        permitted_images={
            RUNTIME_PROFILES['gpu-ml-standard-v1'].runner_image
        },
        evaluator_ready=True,
    )
    assert not preflight.ready
    assert not preflight.assets_ready
    assert 'private training split' in preflight.blocking_issues[0]


def test_task_asset_fetcher_rejects_non_public_url(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    with pytest.raises(TaskBundleError, match='public HTTPS'):
        manager.assets.fetch(
            task_digest='a' * 64,
            proposal=TaskAssetProposal(
                name='private_data',
                role='train',
                source_url='http://127.0.0.1/data.csv',
                expected_sha256='a' * 64,
            ),
        )


class _FakeNetworkStream:
    def __init__(self, server_addr: tuple[str, int]) -> None:
        self._server_addr = server_addr

    def get_extra_info(self, info: str):
        if info == 'server_addr':
            return self._server_addr
        return None


class _PrivatePeerTransport(httpx.BaseTransport):
    def __init__(self, server_addr: tuple[str, int]) -> None:
        self._server_addr = server_addr

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={'content-type': 'text/plain'},
            content=b'secret internal data',
            extensions={'network_stream': _FakeNetworkStream(self._server_addr)},
        )


def test_asset_fetch_revalidates_peer_address(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Validation resolves the host to a public address, but the connection
    # lands on a link-local peer (a DNS-rebinding attacker answered the
    # connect-time lookup differently): the fetch must abort before reading
    # a single byte, so the private content never becomes an asset.
    monkeypatch.setattr(
        'app.url_fetch.socket.getaddrinfo',
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443))
        ],
    )
    content = b'secret internal data'
    fetcher = TaskAssetFetcher(
        root=str(tmp_path / 'task-assets'),
        shared_mount_root=str(tmp_path),
        maximum_bytes=1024,
        transport=_PrivatePeerTransport(('169.254.169.254', 443)),
    )
    with pytest.raises(TaskBundleError, match='non-public'):
        fetcher.fetch(
            task_digest='a' * 64,
            proposal=TaskAssetProposal(
                name='flipped',
                role='train',
                source_url='https://example.com/data.csv',
                expected_sha256=sha256(content).hexdigest(),
            ),
        )


def test_ingested_dataset_is_immutable_and_resolves_into_task(
    tmp_path: Path,
) -> None:
    store = SqliteStore(str(tmp_path / 'orchestrator.db'))
    datasets = DatasetIngestionManager(
        store=store,
        root=str(tmp_path / 'dataset-uploads'),
        shared_mount_root=str(tmp_path),
        maximum_bytes=1024,
    )
    first = datasets.ingest_bytes(
        b'feature,label\n1,0\n',
        filename='train.csv',
        name='training_data',
        role='train',
        contains_labels=True,
        uploaded_by='test',
    )
    second = datasets.ingest_bytes(
        b'feature,label\n1,0\n',
        filename='renamed.csv',
        name='other_name',
        role='input',
        contains_labels=True,
    )
    assert second == first
    assert first.reference_uri == f'glasslab-dataset://{first.sha256}'
    assert Path(first.path).stat().st_mode & 0o222 == 0

    manager = TaskBundleManager(
        root=str(tmp_path / 'task-bundles'),
        shared_mount_root=str(tmp_path),
        dataset_catalog_path=str(tmp_path / 'catalog.json'),
        task_asset_root=str(tmp_path / 'task-assets'),
        ingested_datasets=datasets,
    )
    proposal = TaskSpecProposal(
        schema_version='glasslab-task-spec-v1',
        display_name='Uploaded Dataset Task',
        runtime_profile='cpu-ml-standard-v1',
        assets=[
            TaskAssetProposal(
                name='training_data',
                role='train',
                approved_uri=first.reference_uri,
                expected_sha256=first.sha256,
                contains_labels=True,
            )
        ],
        rationale='Use the operator-approved upload.',
    )
    task = manager.compile(
        manager.stage_archive(filename='task.zip', content=_archive()),
        proposal,
    )
    assert task.datasets[0].uri == first.artifact_uri
    assert manager.preflight(
        task,
        permitted_images={task.runner_image},
        evaluator_ready=True,
    ).ready


def test_ingested_dataset_rejects_tampering(tmp_path: Path) -> None:
    store = SqliteStore(str(tmp_path / 'orchestrator.db'))
    datasets = DatasetIngestionManager(
        store=store,
        root=str(tmp_path / 'dataset-uploads'),
        shared_mount_root=str(tmp_path),
        maximum_bytes=1024,
    )
    record = datasets.ingest_bytes(
        b'original',
        filename='data.csv',
        name='data',
    )
    Path(record.path).chmod(0o644)
    Path(record.path).write_bytes(b'tampered')
    with pytest.raises(TaskBundleError, match='checksum'):
        datasets.resolve(
            record.reference_uri,
            name='data',
            role='input',
            contains_labels=False,
        )


def test_engine_compiles_arbitrary_task_name(orchestrator_bundle) -> None:
    _, _, _, runtime, engine = orchestrator_bundle
    record = engine.import_task_bundle(
        filename='new-contributor-task.zip',
        content=_archive(),
    )
    assert record.task_id == f'task-{record.digest[:16]}'
    assert record.task_spec is not None
    assert record.task_spec['required_metric_keys'] == ['accuracy']
    assert runtime.sessions == {}


def test_source_bundle_packaging_is_deterministic(
    tmp_path: Path,
    orchestrator_bundle,
) -> None:
    _, _, _, _, engine = orchestrator_bundle
    run_id = 'deterministic-source'
    paths = engine.workspaces.prepare(run_id)
    source = paths.beaker / 'benchmark-workspace' / 'adult-income'
    source.mkdir(parents=True)
    (source / 'run.py').write_text('print("ok")\n')
    first_path, first_digest = engine.workspaces.package_source_bundle(
        run_id=run_id,
        source_subdirectory='benchmark-workspace/adult-income',
    )
    first_bytes = first_path.read_bytes()
    second_path, second_digest = engine.workspaces.package_source_bundle(
        run_id=run_id,
        source_subdirectory='benchmark-workspace/adult-income',
    )
    assert second_path.read_bytes() == first_bytes
    assert second_digest == first_digest


def test_preflight_includes_actionable_feedback_when_not_ready(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    proposal = TaskSpecProposal(
        schema_version='glasslab-task-spec-v1',
        display_name='Incomplete Task',
        runtime_profile='cpu-ml-standard-v1',
        assets=[],
        required_artifacts=['metrics.json'],
        required_metric_keys=['accuracy'],
        missing_inputs=[
            'exact evaluation rubric with metric thresholds and stopping conditions'
        ],
        rationale='Missing the rubric on purpose.',
    )
    record = manager.compile(
        manager.stage_archive(filename='anything.zip', content=_archive()),
        proposal,
    )
    preflight = manager.preflight(
        record,
        permitted_images={
            RUNTIME_PROFILES['cpu-ml-standard-v1'].runner_image
        },
        evaluator_ready=True,
    )
    assert not preflight.ready
    assert '## Evaluation rubric' in preflight.feedback
    assert 'No run was started' in preflight.feedback


def test_engine_compiles_task_with_task_compiler_model_override(
    tmp_path: Path,
    orchestrator_bundle,
) -> None:
    settings, _, _, runtime, engine = orchestrator_bundle
    settings = settings.model_copy(
        update={
            'agent_model_honeydew': 'mlx-community/Thinking-4bit',
            'task_compiler_agent_model': 'mlx-community/Coder-Next-4bit',
            'task_compiler_agent_base_url': 'http://192.168.1.17:52416/v1',
        }
    )
    engine.settings = settings
    record = engine.import_task_bundle(
        filename='titanic-task.zip',
        content=_archive(),
    )
    assert record.task_spec is not None
    assert ('honeydew', 'mlx-community/Coder-Next-4bit') in (
        runtime.model_overrides
    )
    assert ('honeydew', 'http://192.168.1.17:52416/v1') in (
        runtime.base_url_overrides
    )


def test_source_url_asset_requires_expected_sha256() -> None:
    with pytest.raises(ValueError, match='expected_sha256'):
        TaskAssetProposal(
            name='public_data',
            role='train',
            source_url='https://example.com/data.csv',
        )


def test_source_url_and_approved_uri_cannot_be_combined() -> None:
    with pytest.raises(ValueError, match='both source_url and approved_uri'):
        TaskAssetProposal(
            name='public_data',
            role='train',
            source_url='https://example.com/data.csv',
            approved_uri=f'glasslab-dataset://{"a" * 64}',
            expected_sha256='b' * 64,
        )


def _public_getaddrinfo(*args, **kwargs):
    return [
        (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443))
    ]


def _private_getaddrinfo(*args, **kwargs):
    return [
        (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 443))
    ]


def _serving_transport(body: bytes) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={'content-type': 'text/csv'},
            content=body,
            extensions={
                'network_stream': _FakeNetworkStream(
                    ('93.184.216.34', 443)
                )
            },
        )

    return httpx.MockTransport(handler)


def _source_url_structured(*, name: str, url: str) -> dict:
    return {
        'kind': 'protocol_draft',
        'summary': 'Drafted a protocol over URL-declared task assets.',
        'task_spec_proposal': {
            'schema_version': 'glasslab-task-spec-v1',
            'display_name': 'Titanic Survival Classification',
            'runtime_profile': 'cpu-ml-standard-v1',
            'assets': [
                {
                    'name': name,
                    'role': 'training data',
                    'source_url': url,
                    'contains_labels': True,
                }
            ],
            'rationale': 'The problem declares these bytes by public URL.',
        },
    }


def test_source_url_without_checksum_records_observed_digest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    orchestrator_bundle,
) -> None:
    # The digest of a remote asset is established by the orchestrator from the
    # bytes the public URL actually serves, never invented by the model. The
    # prepared proposal then satisfies C6 and validates.
    monkeypatch.setattr(
        'app.url_fetch.socket.getaddrinfo',
        _public_getaddrinfo,
    )
    served = b'PassengerId,Pclass,Survived\n1,3,0\n'
    _, _, _, _, engine = orchestrator_bundle
    engine.task_bundles.assets = TaskAssetFetcher(
        root=str(tmp_path / 'task-assets'),
        shared_mount_root=str(tmp_path),
        maximum_bytes=1024 * 1024,
        transport=_serving_transport(served),
    )
    structured = _source_url_structured(
        name='titanic_train',
        url='https://example.com/kaggle-titanic/train.csv',
    )

    prepared = engine._establish_source_url_asset_checksums(structured)

    asset = prepared['task_spec_proposal']['assets'][0]
    assert asset['expected_sha256'] == sha256(served).hexdigest()
    proposal = TaskSpecProposal.model_validate(prepared['task_spec_proposal'])
    assert proposal.assets[0].expected_sha256 == sha256(served).hexdigest()
    assert (
        TaskAssetProposal.model_validate(asset).expected_sha256
        == sha256(served).hexdigest()
    )


def test_source_url_asset_declared_checksum_still_verified_at_materialisation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Regression guard: a declared expected_sha256 that does not match the
    # served bytes is still rejected when the asset is materialised.
    monkeypatch.setattr(
        'app.url_fetch.socket.getaddrinfo',
        _public_getaddrinfo,
    )
    fetcher = TaskAssetFetcher(
        root=str(tmp_path / 'task-assets'),
        shared_mount_root=str(tmp_path),
        maximum_bytes=1024 * 1024,
        transport=_serving_transport(b'the bytes actually served'),
    )
    with pytest.raises(TaskBundleError, match='checksum mismatch for titanic_train'):
        fetcher.fetch(
            task_digest='a' * 64,
            proposal=TaskAssetProposal(
                name='titanic_train',
                role='train',
                source_url='https://example.com/kaggle-titanic/train.csv',
                expected_sha256=sha256(b'some other bytes').hexdigest(),
            ),
        )


def test_source_url_fetch_failure_names_asset_and_reason(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    orchestrator_bundle,
) -> None:
    # A source_url that cannot be fetched and verified is rejected with the
    # asset name and the reason, so the operator can fix the task instead of
    # chasing an opaque 500.
    monkeypatch.setattr(
        'app.url_fetch.socket.getaddrinfo',
        _private_getaddrinfo,
    )
    _, _, _, _, engine = orchestrator_bundle
    engine.task_bundles.assets = TaskAssetFetcher(
        root=str(tmp_path / 'task-assets'),
        shared_mount_root=str(tmp_path),
        maximum_bytes=1024 * 1024,
        transport=_serving_transport(b'whatever'),
    )
    structured = _source_url_structured(
        name='titanic_test',
        url='https://internal.example.com/kaggle-titanic/test.csv',
    )

    with pytest.raises(TaskBundleError) as excinfo:
        engine._establish_source_url_asset_checksums(structured)

    message = str(excinfo.value)
    assert 'titanic_test' in message
    assert 'non-public' in message


def _flaky_asset_transport(*, fail_times: int, body: bytes):
    """MockTransport that raises transient read timeouts then serves the body."""
    import httpx

    state = {'calls': 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state['calls'] += 1
        if state['calls'] <= fail_times:
            raise httpx.ReadTimeout('read timed out', request=request)
        return httpx.Response(
            200,
            content=body,
            request=request,
            extensions={
                'network_stream': _FakeNetworkStream(('93.184.216.34', 443))
            },
        )

    transport = httpx.MockTransport(handler)
    return transport, state


def test_task_asset_fetch_retries_transient_timeout_then_succeeds(
    tmp_path: Path,
) -> None:
    import httpx

    from app.task_bundles import TaskAssetFetcher

    transport, state = _flaky_asset_transport(
        fail_times=2, body=b'feature,label\n1,0\n'
    )
    fetcher = TaskAssetFetcher(
        root=str(tmp_path / 'assets'),
        shared_mount_root=str(tmp_path),
        maximum_bytes=1024 * 1024,
        transport=transport,
        max_retries=2,
    )
    asset = fetcher.fetch(
        task_digest='a' * 64,
        proposal=TaskAssetProposal(
            name='training_data',
            role='train',
            source_url='https://example.com/data.csv',
            expected_sha256=sha256(b'feature,label\n1,0\n').hexdigest(),
        ),
    )
    assert state['calls'] == 3
    assert asset.name == 'training_data'
    assert asset.uri.startswith('s3://artifacts/')
    assert len(asset.sha256) == 64


def test_task_asset_fetch_exhausts_retries_with_guidance(tmp_path: Path) -> None:
    import httpx

    from app.task_bundles import TaskAssetFetcher

    transport, state = _flaky_asset_transport(fail_times=10, body=b'x')
    fetcher = TaskAssetFetcher(
        root=str(tmp_path / 'assets'),
        shared_mount_root=str(tmp_path),
        maximum_bytes=1024 * 1024,
        transport=transport,
        max_retries=2,
    )
    with pytest.raises(TaskBundleError, match='dataset-upload'):
        fetcher.fetch(
            task_digest='a' * 64,
            proposal=TaskAssetProposal(
                name='training_data',
                role='train',
                source_url='https://example.com/data.csv',
                expected_sha256=sha256(b'x').hexdigest(),
            ),
        )
    assert state['calls'] == 3  # initial + 2 retries
