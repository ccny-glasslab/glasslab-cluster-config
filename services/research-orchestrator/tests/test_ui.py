"""Read-only corpus UI page: panes, chat, escaping, citation badges, and CSP.

The page is server-rendered with no client JavaScript of its own and no
external resource, so these tests drive it with ``TestClient`` and assert on
the escaped HTML: injected ``<script>`` payloads stay inert, ranked-source
URIs and filesystem paths are never emitted, and the evidence badge comes from
the deterministic citation locator rather than the stored (tautological)
``ranked_sources[].verified`` flag.

The corpus chat is a same-origin ``GET`` form (the loopback UI proxy forwards
only ``GET``/``HEAD`` and injects the operator token), so ``?q=…`` renders the
answer into the same page. Citation links preserve the question and select the
cited source in the side panel, which embeds the same-origin PDF viewer
iframe. The CSP tests pin the two deliberate deltas: ``form-action 'self'``
and ``frame-src 'self'``; everything else still denies by default, and the
document still carries no script and no external origin.

The tests use the repository's ``orchestrator_bundle`` fixture: a real
SqliteStore engine with a fake runtime, no live cluster, and no network.
"""

from __future__ import annotations

import html
import re
from hashlib import sha256
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient
import pytest

from app.corpus_rag.chat import CorpusChatService
from app.corpus_rag.contracts import RagChunkRecord
from app.schemas import (
    AgentName,
    ArtifactRecord,
    ContextPacket,
    KnowledgeSource,
    RunCreateRequest,
    RunRecord,
    SourceType,
    TurnKind,
)
from app.ui import register_ui_routes

OPERATOR_TOKEN = 'test-operator-token'
AUTH_HEADERS = {'X-Glasslab-Operator-Token': OPERATOR_TOKEN}
REPORT_REF = 'reports/report.md'
_BLOCK = 'The quick brown fox jumps over the lazy dog'
_ABSENT_EXCERPT = 'this excerpt appears in no context block'
_CHAT_QUESTION = 'resampling stability small samples'
_CHAT_CHUNK_TEXT = 'Resampling improves stability of small samples.'
_CHAT_TITLE = 'Resampling Handbook'


def _require_operator_token(
    supplied: str | None = Header(
        default=None,
        alias='X-Glasslab-Operator-Token',
    ),
) -> None:
    # Mirrors main.create_app's closure shape: the dependency is injected, not
    # imported, so the UI module never knows the token.
    if supplied is None or supplied != OPERATOR_TOKEN:
        raise HTTPException(
            status_code=401,
            detail='valid operator token required',
        )


def _ui_app(settings, engine, **route_options) -> FastAPI:
    app = FastAPI()
    register_ui_routes(
        app,
        engine=engine,
        settings=settings,
        require_operator=_require_operator_token,
        **route_options,
    )
    return app


def _client(settings, engine, **route_options) -> TestClient:
    return TestClient(_ui_app(settings, engine, **route_options))


def _seed_chat_corpus(
    engine,
    *,
    title: str = _CHAT_TITLE,
    text: str = _CHAT_CHUNK_TEXT,
    page_start: int | None = 3,
) -> KnowledgeSource:
    """One titled source with one retrievable chunk for the chat to cite."""
    source = KnowledgeSource(
        source_type=SourceType.DOCUMENTATION,
        canonical_uri='repo://docs/resampling.md',
        digest=sha256(title.encode()).hexdigest(),
        title=title,
    )
    engine.store.save_knowledge_source(source)
    engine.store.replace_rag_chunks(
        source.source_id,
        [
            RagChunkRecord(
                chunk_id=f'{source.source_id}::c0',
                source_id=source.source_id,
                kind='evidence_span',
                chunk_index=0,
                text=text,
                digest=sha256(text.encode()).hexdigest(),
                token_count=max(1, len(text.split())),
                page_start=page_start,
            )
        ],
    )
    return source


def _write_report(settings, run_id: str, body: bytes) -> ArtifactRecord:
    path = Path(settings.shared_mount_root) / run_id / REPORT_REF
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return ArtifactRecord(
        run_id=run_id,
        type='report',
        uri=f'artifact://{run_id}/{REPORT_REF}',
        sha256=sha256(body).hexdigest(),
        metadata={'path': str(path)},
    )


def _packet_text(*blocks: str) -> str:
    sections = []
    for offset, block in enumerate(blocks, start=1):
        sections.append(
            '<knowledge-context '
            f'source="src-{offset}" kind="prose" score="0.500" '
            f'scope="approved" uri="knowledge://src-{offset}" '
            f'digest="{"a" * 64}">\n{block}\n</knowledge-context>'
        )
    return '\n\n'.join(sections)


def _save_packet(
    engine,
    run: RunRecord,
    *,
    blocks: list[str],
    ranked_sources: list[dict],
) -> ContextPacket:
    packet = ContextPacket(
        run_id=run.run_id,
        agent=AgentName.HONEYDEW,
        turn_number=1,
        turn_kind=TurnKind.RESEARCH_ANSWER,
        query='what did the source say',
        index_version='v1',
        ranked_sources=ranked_sources,
        exact_text_supplied=_packet_text(*blocks),
        token_budget=2048,
    )
    engine.store.save_context_packet(packet)
    return packet


def test_ui_requires_operator_token_and_renders_three_panes(
    orchestrator_bundle,
) -> None:
    settings, _, _, _, engine = orchestrator_bundle
    run = engine.create_run(RunCreateRequest(objective='render the corpus ui'))

    with _client(settings, engine) as client:
        unauthenticated = client.get('/ui/')
        authenticated = client.get('/ui/', headers=AUTH_HEADERS)

    assert unauthenticated.status_code == 401
    assert authenticated.status_code == 200
    assert authenticated.headers['content-type'].startswith('text/html')
    for heading in (
        '<h2>Sources</h2>',
        '<h2>Document</h2>',
        '<h2>Evidence inspector</h2>',
    ):
        assert heading in authenticated.text
    # Links stay root-relative: the page is reached through the loopback proxy,
    # never rewritten to an absolute 127.0.0.1:18080/19090 origin.
    assert f'/ui/?run={run.run_id}' in authenticated.text
    assert '127.0.0.1' not in authenticated.text


def test_register_ui_routes_adds_exactly_one_ui_route(orchestrator_bundle) -> None:
    settings, _, _, _, engine = orchestrator_bundle
    app = _ui_app(settings, engine)

    ui_routes = [
        route for route in app.routes if getattr(route, 'path', None) == '/ui/'
    ]

    assert len(ui_routes) == 1
    assert 'GET' in ui_routes[0].methods


def test_ui_escapes_report_body_and_context_block(orchestrator_bundle) -> None:
    settings, _, _, _, engine = orchestrator_bundle
    run = engine.create_run(RunCreateRequest(objective='escape injected markup'))
    payload = '<script>alert(1)</script>'
    engine.store.save_artifact(
        _write_report(settings, run.run_id, f'# Findings\n\n{payload}\n'.encode())
    )
    packet = _save_packet(
        engine,
        run,
        blocks=[f'passage {payload}'],
        ranked_sources=[{'source_id': 'src-1', 'digest': 'a' * 64, 'score': 0.5}],
    )

    with _client(settings, engine) as client:
        response = client.get(
            '/ui/',
            params={
                'run': run.run_id,
                'ref': REPORT_REF,
                'packet': packet.packet_id,
                'excerpt': payload,
            },
            headers=AUTH_HEADERS,
        )

    assert response.status_code == 200
    assert '<script>' not in response.text
    assert '&lt;script&gt;alert(1)&lt;/script&gt;' in response.text
    assert '# Findings' in response.text


@pytest.mark.parametrize(
    ('excerpt', 'badge'),
    [
        (_BLOCK, '✓ exact'),
        ('The quick, brown fox jumps over the lazy dog.', '≈ fuzzy'),
        (_ABSENT_EXCERPT, '✗ unverified'),
    ],
)
def test_ui_evidence_badge_uses_live_locator_not_stored_flag(
    orchestrator_bundle,
    excerpt: str,
    badge: str,
) -> None:
    settings, _, _, _, engine = orchestrator_bundle
    run = engine.create_run(RunCreateRequest(objective='classify a citation'))
    packet = _save_packet(
        engine,
        run,
        blocks=[_BLOCK],
        ranked_sources=[
            {
                'source_id': 'src-1',
                'digest': 'a' * 64,
                'score': 0.75,
                'verified': True,
                'uri': 'knowledge://src-1',
            }
        ],
    )
    # The stored flag is a build-time tautology; the page must still classify
    # an absent excerpt as unverified.
    assert packet.ranked_sources[0]['verified'] is True

    with _client(settings, engine) as client:
        response = client.get(
            '/ui/',
            params={
                'run': run.run_id,
                'packet': packet.packet_id,
                'excerpt': excerpt,
            },
            headers=AUTH_HEADERS,
        )

    assert response.status_code == 200
    assert f'>{badge}</span>' in response.text
    for other in {'✓ exact', '≈ fuzzy', '✗ unverified'} - {badge}:
        assert f'>{other}</span>' not in response.text


def test_ui_never_renders_uris_paths_or_tokens(orchestrator_bundle) -> None:
    settings, _, _, _, engine = orchestrator_bundle
    run = engine.create_run(RunCreateRequest(objective='hide paths and tokens'))
    engine.store.save_artifact(
        _write_report(settings, run.run_id, b'# Report body\n')
    )
    engine.store.save_knowledge_source(
        KnowledgeSource(
            source_id='src-ui-1',
            source_type=SourceType.PAPER,
            canonical_uri=f'{settings.shared_mount_root}/corpus/private.txt',
            digest='b' * 64,
            title='Private corpus paper',
        )
    )
    packet = _save_packet(
        engine,
        run,
        blocks=[_BLOCK],
        ranked_sources=[
            {
                'source_id': 'src-ui-1',
                'digest': 'b' * 64,
                'score': 0.9,
                'verified': True,
                'uri': 'knowledge://src-ui-1',
            }
        ],
    )

    with _client(settings, engine) as client:
        response = client.get(
            '/ui/',
            params={
                'run': run.run_id,
                'ref': REPORT_REF,
                'packet': packet.packet_id,
                'excerpt': _BLOCK,
            },
            headers=AUTH_HEADERS,
        )

    assert response.status_code == 200
    assert 'knowledge://' not in response.text
    assert 'artifact://' not in response.text
    assert str(settings.shared_mount_root) not in response.text
    assert OPERATOR_TOKEN not in response.text


def test_ui_redacts_credential_formats_in_packet_free_text(
    orchestrator_bundle,
) -> None:
    settings, _, _, _, engine = orchestrator_bundle
    # Assembled at runtime so the synthetic key is never a literal credential
    # in the test source itself.
    leaked = 'gh' + 'p_' + 'a' * 36
    run = engine.create_run(RunCreateRequest(objective='redact ui free text'))
    packet = ContextPacket(
        run_id=run.run_id,
        agent=AgentName.HONEYDEW,
        turn_number=1,
        turn_kind=TurnKind.RESEARCH_ANSWER,
        query=f'password policy question mentioning {leaked}',
        index_version='v1',
        ranked_sources=[
            {'source_id': 'src-1', 'digest': 'a' * 64, 'score': 0.5}
        ],
        exact_text_supplied=_packet_text(
            f'password hygiene note with {leaked} inside'
        ),
        token_budget=2048,
    )
    engine.store.save_context_packet(packet)

    with _client(settings, engine) as client:
        response = client.get(
            '/ui/',
            params={
                'run': run.run_id,
                'packet': packet.packet_id,
                'excerpt': f'password hygiene note with {leaked} inside',
            },
            headers=AUTH_HEADERS,
        )

    assert response.status_code == 200
    assert leaked not in response.text
    assert '[REDACTED]' in response.text
    # Ordinary prose that merely discusses credentials survives unchanged.
    assert 'password policy question mentioning' in response.text
    assert 'password hygiene note with' in response.text


@pytest.mark.parametrize(
    'ref',
    [
        'runtime/session.log',
        'reports/../secret.md',
        'plots/%2e%2e/secret.png',
    ],
)
def test_ui_document_refuses_out_of_policy_ref(
    orchestrator_bundle,
    ref: str,
) -> None:
    settings, _, _, _, engine = orchestrator_bundle
    run = engine.create_run(RunCreateRequest(objective='refuse a private ref'))
    secret = b'private runtime session state'
    path = Path(settings.shared_mount_root) / run.run_id / 'runtime' / 'session.log'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(secret)
    engine.store.save_artifact(
        ArtifactRecord(
            run_id=run.run_id,
            type='runtime-session',
            uri=f'artifact://{run.run_id}/runtime/session.log',
            sha256=sha256(secret).hexdigest(),
            metadata={'path': str(path)},
        )
    )

    with _client(settings, engine) as client:
        response = client.get(
            '/ui/',
            params={'run': run.run_id, 'ref': ref},
            headers=AUTH_HEADERS,
        )

    assert response.status_code == 200
    assert 'Document unavailable' in response.text
    assert 'private runtime session state' not in response.text


def test_ui_csp_nonce_and_no_external_resources(orchestrator_bundle) -> None:
    settings, _, _, _, engine = orchestrator_bundle

    with _client(settings, engine) as client:
        response = client.get('/ui/', headers=AUTH_HEADERS)

    assert response.status_code == 200
    csp = response.headers['content-security-policy']
    assert "default-src 'none'" in csp
    assert "'nonce-" in csp
    assert 'unsafe-inline' not in csp
    assert 'http://' not in csp
    assert 'https://' not in csp
    # Deliberate deltas: the chat form submits a same-origin GET and the
    # cited-source PDF viewer is framed same-origin. Neither reopens a remote
    # origin, and form-action is no longer 'none'.
    assert "form-action 'self'" in csp
    assert "form-action 'none'" not in csp
    assert "frame-src 'self'" in csp
    assert response.headers['x-content-type-options'] == 'nosniff'
    assert response.headers['referrer-policy'] == 'no-referrer'

    # No script tag, and no external origin anywhere in the document.
    assert '<script' not in response.text
    assert 'http://' not in response.text
    assert 'https://' not in response.text

    nonce = csp.split("'nonce-", 1)[1].split("'", 1)[0]
    assert nonce
    assert f'<style nonce="{nonce}">' in response.text


def test_ui_ask_form_is_get_and_get_driven(orchestrator_bundle) -> None:
    settings, _, _, _, engine = orchestrator_bundle
    _seed_chat_corpus(engine)
    chat_service = CorpusChatService(engine.store)

    with _client(settings, engine, chat_service=chat_service) as client:
        page = client.get('/ui/', headers=AUTH_HEADERS)
        answered = client.get(
            '/ui/',
            params={'q': _CHAT_QUESTION},
            headers=AUTH_HEADERS,
        )

    assert page.status_code == 200
    assert '<section class="pane" id="ask">' in page.text
    assert '<h2>Ask the corpus</h2>' in page.text
    assert '<form method="get" action="/ui/"' in page.text
    assert 'name="q"' in page.text
    # GET-driven: the answer is rendered into the same page from ?q=… alone;
    # the question never needs a POST, which the loopback proxy would reject.
    assert answered.status_code == 200
    assert _CHAT_CHUNK_TEXT not in page.text
    assert _CHAT_CHUNK_TEXT in answered.text


def test_ui_chat_renders_source_title_excerpt_and_badge(
    orchestrator_bundle,
) -> None:
    settings, _, _, _, engine = orchestrator_bundle
    _seed_chat_corpus(engine)
    chat_service = CorpusChatService(engine.store)

    with _client(settings, engine, chat_service=chat_service) as client:
        response = client.get(
            '/ui/',
            params={'q': _CHAT_QUESTION},
            headers=AUTH_HEADERS,
        )

    assert response.status_code == 200
    # The citation links the store-resolved title, quotes the excerpt, and
    # carries the live locator verdict badge.
    assert f'{_CHAT_TITLE}</a>' in response.text
    assert _CHAT_CHUNK_TEXT in response.text
    assert '✓ exact' in response.text
    assert 'knowledge://' not in response.text


def test_ui_chat_empty_corpus_shows_no_evidence(orchestrator_bundle) -> None:
    settings, _, _, _, engine = orchestrator_bundle
    chat_service = CorpusChatService(engine.store)

    with _client(settings, engine, chat_service=chat_service) as client:
        response = client.get(
            '/ui/',
            params={'q': _CHAT_QUESTION},
            headers=AUTH_HEADERS,
        )

    assert response.status_code == 200
    assert (
        'No corpus evidence is available to answer that question.'
        in response.text
    )
    assert 'class="badge' not in response.text


def test_ui_chat_escapes_and_redacts_question_and_answer(
    orchestrator_bundle,
) -> None:
    settings, _, _, _, engine = orchestrator_bundle
    # Assembled at runtime so the synthetic key is never a literal credential
    # in the test source itself.
    secret = 'gh' + 'p_' + 'b' * 36
    payload = '<script>alert(1)</script>'
    _seed_chat_corpus(
        engine,
        text=f'Resampling {payload} improves stability of {secret} samples.',
    )
    chat_service = CorpusChatService(engine.store)
    question = f'{payload} resampling stability with {secret}'

    with _client(settings, engine, chat_service=chat_service) as client:
        response = client.get(
            '/ui/',
            params={'q': question},
            headers=AUTH_HEADERS,
        )

    assert response.status_code == 200
    assert '<script' not in response.text
    assert '&lt;script&gt;alert(1)&lt;/script&gt;' in response.text
    assert secret not in response.text
    assert '[REDACTED]' in response.text
    # Ordinary prose around the payload and the secret survives unchanged.
    assert 'resampling stability with' in response.text
    assert 'improves stability of' in response.text


def test_ui_citation_link_preserves_q_and_targets_source_panel(
    orchestrator_bundle,
) -> None:
    settings, _, _, _, engine = orchestrator_bundle
    source = _seed_chat_corpus(engine)
    chat_service = CorpusChatService(engine.store)

    with _client(settings, engine, chat_service=chat_service) as client:
        page = client.get(
            '/ui/',
            params={'q': _CHAT_QUESTION},
            headers=AUTH_HEADERS,
        )
        match = re.search(r'href="(/ui/\?[^"]*source=[^"]*)"', page.text)
        assert match is not None
        citation_href = html.unescape(match.group(1))
        followed = client.get(citation_href, headers=AUTH_HEADERS)

    assert page.status_code == 200
    citation_query = parse_qs(urlsplit(citation_href).query)
    assert citation_query['q'] == [_CHAT_QUESTION]
    assert citation_query['source'] == [source.source_id]
    # The chunk is 0-based (page_start=3); the emitted HTTP page is the
    # 1-based human page number the viewer and boxes route agree on.
    assert citation_query['page'] == ['4']
    assert citation_query['excerpt'] == [_CHAT_CHUNK_TEXT]

    # Selecting the citation selects the cited source in the side panel: the
    # page embeds the same-origin PDF viewer iframe at the cited page and
    # excerpt. The iframe is deliberately unsandboxed (the vendored viewer
    # needs same-origin module workers and blob URLs) and root-relative.
    assert followed.status_code == 200
    frame = re.search(r'<iframe[^>]*\bsrc="([^"]+)"[^>]*>', followed.text)
    assert frame is not None
    assert 'sandbox' not in frame.group(0)
    frame_src = html.unescape(frame.group(1))
    assert frame_src.startswith('/ui/pdf/assets/web/highlight.html?')
    frame_query = parse_qs(urlsplit(frame_src).query)
    assert frame_query['source'] == [source.source_id]
    assert frame_query['page'] == ['4']
    assert frame_query['excerpt'] == [_CHAT_CHUNK_TEXT]
    assert 'knowledge://' not in followed.text
    assert 'artifact://' not in followed.text


def test_ui_citation_omits_page_when_chunk_has_no_page(
    orchestrator_bundle,
) -> None:
    settings, _, _, _, engine = orchestrator_bundle
    _seed_chat_corpus(engine, page_start=None)
    chat_service = CorpusChatService(engine.store)

    with _client(settings, engine, chat_service=chat_service) as client:
        page = client.get(
            '/ui/',
            params={'q': _CHAT_QUESTION},
            headers=AUTH_HEADERS,
        )

    assert page.status_code == 200
    match = re.search(r'href="(/ui/\?[^"]*source=[^"]*)"', page.text)
    assert match is not None
    citation_query = parse_qs(urlsplit(html.unescape(match.group(1))).query)
    assert 'page' not in citation_query
