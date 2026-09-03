"""Knowledge and context-packet HTTP surface over a mocked engine.

Builds a full FastAPI app with a temp knowledge root and tests ingest, list,
rebuild, and digest-based invalidation through the API, allowlist
enforcement for outside paths, and the context-packet list/inspect routes.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.cluster import FakeClusterExecutor
from app.config import SERVICE_ROOT, Settings
from app.contract_candidates import ContractCandidateManager
from app.contracts import EvaluationContractResolver
from app.discord_adapter import DisabledDiscordAdapter
from app.engine import ResearchOrchestrator
from app.main import create_app
from app.mock_runtime import ScriptedMockRuntime
from app.policy import ActionPolicy
from app.schemas import RunCreateRequest
from app.storage import SqliteStore
from app.workspaces import WorkspaceManager
from conftest import RUNNER_IMAGE, create_test_repo


def _bundle(tmp_path: Path):
    # The engine here mirrors the conftest orchestrator_bundle fixture but
    # adds a knowledge root and an allowlisted directory the API may index.
    approved = tmp_path / 'approved'
    approved.mkdir(parents=True)
    (approved / 'technique-card.md').write_text(
        'Technique card: metric-search over GPU clusters. '
        'Prefer cosine similarity for embedding retrieval.'
    )
    repo = create_test_repo(tmp_path)
    settings = Settings(
        database_path=str(tmp_path / 'orchestrator.db'),
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
        knowledge_root=str(tmp_path / 'knowledge'),
        knowledge_allowlist_roots=[str(approved)],
        one_active_run=False,
        maximum_parallel_jobs=2,
    )
    store = SqliteStore(settings.database_path)
    engine = ResearchOrchestrator(
        settings=settings,
        store=store,
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
    )
    return approved, settings, engine


def test_knowledge_api_ingest_list_inspect_invalidate(tmp_path: Path) -> None:
    approved, settings, engine = _bundle(tmp_path)
    app = create_app(settings, engine=engine, start_watcher=False)
    with TestClient(app) as client:
        ingest = client.post(
            '/knowledge/sources',
            json={
                'source_type': 'technique_card',
                'path': str(approved / 'technique-card.md'),
                'title': 'GPU metric-search technique',
                'source_version': 'v1',
            },
        )
        assert ingest.status_code == 201, ingest.text
        source = ingest.json()
        assert source['source_type'] == 'technique_card'
        assert len(source['digest']) == 64
        assert source['title'] == 'GPU metric-search technique'

        listed = client.get('/knowledge/sources').json()['sources']
        assert any(item['source_id'] == source['source_id'] for item in listed)

        rebuilt = client.post('/knowledge/index/rebuild')
        assert rebuilt.status_code == 200
        assert rebuilt.json()['reindexed_sources'] >= 1

        removed = client.delete(
            f'/knowledge/sources/by-digest/{source["digest"]}'
        )
        assert removed.status_code == 200
        assert removed.json()['removed'] == 1
        listed = client.get('/knowledge/sources').json()['sources']
        assert listed == []


def test_knowledge_api_rejects_outside_path(tmp_path: Path) -> None:
    approved, settings, engine = _bundle(tmp_path)
    outside = tmp_path / 'outside' / 'notes.md'
    outside.parent.mkdir(parents=True)
    outside.write_text('Outside the allowlist.')
    app = create_app(settings, engine=engine, start_watcher=False)
    with TestClient(app) as client:
        response = client.post(
            '/knowledge/sources',
            json={
                'source_type': 'documentation',
                'path': str(outside),
            },
        )
        assert response.status_code == 409
        assert 'outside approved knowledge roots' in response.text


def test_context_packet_api_lists_and_inspects(tmp_path: Path) -> None:
    approved, settings, engine = _bundle(tmp_path)
    app = create_app(settings, engine=engine, start_watcher=False)
    with TestClient(app) as client:
        run = client.post(
            '/runs',
            json={
                'objective': 'Exercise the context packet inspection API.'
            },
        )
        assert run.status_code == 201
        run_id = run.json()['run_id']
        packets = client.get(f'/runs/{run_id}/context-packets').json()['packets']
        assert packets
        packet_id = packets[0]['packet_id']
        detail = client.get(f'/context-packets/{packet_id}')
        assert detail.status_code == 200
        assert detail.json()['run_id'] == run_id
        assert detail.json()['packet_id'] == packet_id
        assert 'index_version' in detail.json()


def _make_born_digital_pdf() -> bytes:
    import pymupdf

    doc = pymupdf.Document()
    body_lines = [
        'Section one: consensus clustering stabilizes bootstrap replicates',
        'across many initializations of the k-means procedure.',
        'Adjusted rand index tracks agreement between replicate partitions.',
        'Second section: fixed-k baselines anchor the stability comparison',
        'and silhouette diagnostics summarize separation quality.',
    ]
    for index, line in enumerate(body_lines):
        page = doc.new_page()
        page.insert_text((72, 200 + 20 * index), line)
    data = doc.tobytes()
    doc.close()
    return data


def test_upload_endpoint_accepts_text_and_pdf(tmp_path: Path) -> None:
    approved, settings, engine = _bundle(tmp_path)
    app = create_app(settings, engine=engine, start_watcher=False)
    with TestClient(app) as client:
        text_response = client.post(
            '/knowledge/sources/upload',
            files={
                'file': (
                    'notes.txt',
                    b'Uploaded note: prefer consensus clustering for stability.',
                )
            },
            data={'source_type': 'documentation'},
        )
        assert text_response.status_code == 201, text_response.text
        source = text_response.json()
        assert source['canonical_uri'] == 'upload://notes.txt'
        assert source['access_policy'] == 'run-approved'

        pdf_response = client.post(
            '/knowledge/sources/upload',
            files={
                'file': ('methods.pdf', _make_born_digital_pdf()),
            },
            data={'source_type': 'paper', 'title': 'Methods PDF'},
        )
        assert pdf_response.status_code == 201, pdf_response.text
        assert pdf_response.json()['title'] == 'Methods PDF'

        hits = engine.knowledge.store.search_knowledge_chunks(
            'consensus clustering', limit=3
        )
        assert hits, 'extracted PDF text must be retrievable'

        # Re-uploading identical content deduplicates to the same source.
        repeat = client.post(
            '/knowledge/sources/upload',
            files={
                'file': (
                    'notes.txt',
                    b'Uploaded note: prefer consensus clustering for stability.',
                )
            },
            data={'source_type': 'documentation'},
        )
        assert repeat.status_code == 201
        assert repeat.json()['source_id'] == source['source_id']


def test_upload_endpoint_rejects_oversize_secret_and_binary(
    tmp_path: Path, monkeypatch
) -> None:
    approved, settings, engine = _bundle(tmp_path)
    settings.knowledge_max_source_bytes = 512
    settings.operator_api_token = 'live-operator-token-value-abc123'
    engine.knowledge.max_source_bytes = 512
    app = create_app(settings, engine=engine, start_watcher=False)
    with TestClient(app) as client:
        oversize = client.post(
            '/knowledge/sources/upload',
            files={'file': ('big.txt', b'x' * 513)},
            data={'source_type': 'documentation'},
        )
        assert oversize.status_code == 413

        # Operator uploads check LIVE secret VALUES, not the broad heuristic:
        # a real configured value is refused...
        secret = client.post(
            '/knowledge/sources/upload',
            files={
                'file': (
                    'config-notes.txt',
                    b'deployment notes\noperator token: live-operator-token-value-abc123\n',
                )
            },
            data={'source_type': 'documentation'},
        )
        assert secret.status_code == 409
        assert 'secret' in secret.text

        # ...while credential-shaped prose that is NOT a live value is fine.
        prose = client.post(
            '/knowledge/sources/upload',
            files={
                'file': (
                    'textbook-notes.txt',
                    b'password managers, api_key=, token = nn.Parameter examples',
                )
            },
            data={'source_type': 'documentation'},
        )
        assert prose.status_code == 201, prose.text

        binary = client.post(
            '/knowledge/sources/upload',
            files={'file': ('blob.bin', b'\x00\x01\x02\xff\xfe\xfd')},
            data={'source_type': 'documentation'},
        )
        assert binary.status_code == 409
        assert 'not a PDF and not UTF-8' in binary.text


def test_upload_endpoint_requires_operator_token(tmp_path: Path) -> None:
    approved, settings, engine = _bundle(tmp_path)
    settings.require_operator_auth = True
    settings.operator_api_token = 'expected-token'
    app = create_app(settings, engine=engine, start_watcher=False)
    with TestClient(app) as client:
        denied = client.post(
            '/knowledge/sources/upload',
            files={'file': ('notes.txt', b'hello')},
            data={'source_type': 'documentation'},
        )
        assert denied.status_code in (401, 403)

        allowed = client.post(
            '/knowledge/sources/upload',
            files={'file': ('notes.txt', b'hello from operator')},
            data={'source_type': 'documentation'},
            headers={'X-Glasslab-Operator-Token': 'expected-token'},
        )
        assert allowed.status_code == 201


def test_chat_returns_cited_research_answer(tmp_path: Path) -> None:
    _, settings, engine = _bundle(tmp_path)
    app = create_app(settings, engine=engine, start_watcher=False)
    with TestClient(app) as client:
        response = client.post(
            '/chat',
            json={'question': 'how does conformal prediction guarantee coverage'},
        )
        assert response.status_code == 201
        body = response.json()
        assert body['answer']
        assert body['citations'], 'a corpus-answerable question must cite sources'
        assert body['citations'][0]['knowledge_uri'].startswith('knowledge://')
        assert body['citations'][0]['excerpt']


def test_chat_requires_operator_token(tmp_path: Path) -> None:
    _, settings, engine = _bundle(tmp_path)
    settings.require_operator_auth = True
    settings.operator_api_token = 'expected-token'
    app = create_app(settings, engine=engine, start_watcher=False)
    with TestClient(app) as client:
        denied = client.post(
            '/chat',
            json={'question': 'what is metric learning'},
        )
        assert denied.status_code in (401, 403)

        allowed = client.post(
            '/chat',
            json={'question': 'what is metric learning'},
            headers={'X-Glasslab-Operator-Token': 'expected-token'},
        )
        assert allowed.status_code == 201


def test_chat_conversation_binding_and_view(tmp_path: Path) -> None:
    _, settings, engine = _bundle(tmp_path)
    app = create_app(settings, engine=engine, start_watcher=False)
    with TestClient(app) as client:
        first = client.post(
            '/chat',
            json={
                'question': 'how does conformal prediction guarantee coverage',
                'conversation_id': 'chat-p3',
            },
        )
        assert first.status_code == 201

        bound = client.post(
            '/chat/chat-p3/sources',
            json={'source_ids': ['source-1', 'source-2']},
        )
        assert bound.status_code == 200
        assert bound.json()['source_ids'] == ['source-1', 'source-2']

        second = client.post(
            '/chat',
            json={
                'question': 'and what about metric search',
                'conversation_id': 'chat-p3',
                'bind_source_ids': ['source-3'],
            },
        )
        assert second.status_code == 201

        view = client.get('/chat/chat-p3')
        assert view.status_code == 200
        body = view.json()
        assert body['sources'] == ['source-1', 'source-2', 'source-3']
        assert len(body['turns']) == 2
        assert body['turns'][-1]['question'] == 'and what about metric search'
        assert body['turns'][-1]['answer']
        assert body['turns'][-1]['status'] == 'completed'


def test_context_packet_html_page_renders_exact_text(tmp_path: Path) -> None:
    _, settings, engine = _bundle(tmp_path)
    app = create_app(settings, engine=engine, start_watcher=False)
    run = engine.create_run(RunCreateRequest(objective='what is a metric space'))
    with TestClient(app) as client:
        packet = engine.knowledge.retrieve(
            run_id=run.run_id,
            agent='honeydew',
            turn_number=1,
            turn_kind='research_answer',
            query='what is a metric space',
            run_scope=run.run_id,
            allowed_source_types=None,
        )
        response = client.get(
            f'/knowledge/packets/{packet.packet_id}'
        )
        assert response.status_code == 200
        assert 'text/html' in response.headers['content-type']
        assert packet.exact_text_supplied[:60] in response.text
        assert 'Ranked sources' in response.text


def _seed_chat(client, conversation_id: str) -> None:
    for question in (
        'how does conformal prediction guarantee coverage',
        'and what about metric search',
    ):
        response = client.post(
            '/chat',
            json={
                'question': question,
                'conversation_id': conversation_id,
            },
        )
        assert response.status_code == 201


def test_chat_promote_creates_seeded_run(tmp_path: Path) -> None:
    _, settings, engine = _bundle(tmp_path)
    app = create_app(settings, engine=engine, start_watcher=False)
    with TestClient(app) as client:
        _seed_chat(client, 'chat-p4')
        client.post(
            '/chat/chat-p4/sources',
            json={'source_ids': ['source-1']},
        )
        promoted = client.post(
            '/chat/chat-p4/promote',
            json={'objective': 'Run the promoted conversation as a study.'},
        )
        assert promoted.status_code == 200
        body = promoted.json()
        assert body['run_id']
        assert body['seed_source_ids'] == ['source-1']
        assert body['seed_context']
        assert 'metric search' in body['seed_context'] or 'conformal' in body['seed_context']

        # second promote is rejected with the prior run id
        again = client.post(
            '/chat/chat-p4/promote',
            json={'objective': 'Duplicate attempt.'},
        )
        assert again.status_code in (400, 409)
        assert body['run_id'] in again.text


def test_chat_promote_requires_operator_and_turns(tmp_path: Path) -> None:
    _, settings, engine = _bundle(tmp_path)
    settings.require_operator_auth = True
    settings.operator_api_token = 'expected-token'
    app = create_app(settings, engine=engine, start_watcher=False)
    with TestClient(app) as client:
        denied = client.post(
            '/chat/chat-auth/promote',
            json={'objective': 'Should be denied without a token.'},
        )
        assert denied.status_code in (401, 403)

        allowed = client.post(
            '/chat/chat-auth/promote',
            json={'objective': 'Should fail: no turns yet.'},
            headers={'X-Glasslab-Operator-Token': 'expected-token'},
        )
        assert allowed.status_code in (400, 409)
        assert 'no answered turns' in allowed.text


def test_chat_promote_thread_binds_discord_thread(tmp_path: Path) -> None:
    _, settings, engine = _bundle(tmp_path)
    app = create_app(settings, engine=engine, start_watcher=False)
    with TestClient(app) as client:
        _seed_chat(client, 'discord-thread-987654321')
        promoted = client.post(
            '/chat/discord-thread-987654321/promote',
            json={'objective': 'Thread conversation becomes a run.'},
        )
        assert promoted.status_code == 200
        assert promoted.json()['discord_thread_id'] == '987654321'





def test_promote_with_investigation_tags_conversation_and_run(tmp_path: Path) -> None:
    _, settings, engine = _bundle(tmp_path)
    app = create_app(settings, engine=engine, start_watcher=False)
    with TestClient(app) as client:
        _seed_chat(client, 'chat-inv')
        promoted = client.post(
            '/chat/chat-inv/promote',
            json={
                'objective': 'Run the promoted conversation in an investigation.',
                'investigation_id': 'inv-abc123',
            },
        )
        assert promoted.status_code == 200
        assert promoted.json()['investigation_id'] == 'inv-abc123'
        conversation_view = client.get('/chat/chat-inv')
        assert conversation_view.status_code == 200
