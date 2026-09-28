"""Read-only corpus notebook: columns, tabs, chat, escaping, badges, and CSP.

The page is server-rendered with no client JavaScript of its own and no
external resource, so these tests drive it with ``TestClient`` and assert on
the escaped HTML: the three-column notebook (Sources with CSS-only tabs, Ask
the corpus, Viewer), the run file tree and digest-verified preview, injected
``<script>`` payloads staying inert, ranked-source URIs and filesystem paths
never being emitted, and the evidence badge coming from the deterministic
citation locator rather than the stored (tautological)
``ranked_sources[].verified`` flag.

The corpus chat is a same-origin ``GET`` form (the loopback UI proxy forwards
only ``GET``/``HEAD`` and injects the operator token), so ``?q=…`` renders the
answer into the same page. The answer's valid ``[n]`` ordinals become inline
superscript citation markers (there is no end-of-answer reference list), each
with a CSS-only hover/focus preview card that carries the source title, the
verdict badge, and a "View source" affordance. The marker's link preserves the
question and selects the cited source in the side panel, which embeds the
same-origin PDF viewer iframe. The CSP tests pin the two deliberate deltas:
``form-action 'self'`` and ``frame-src 'self'``; everything else still denies
by default, and the document still carries no script and no external origin.

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
from app.ui import _render_chat_answer, register_ui_routes
from app.ui_chat import ChatAnswer, ChatCitation

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
    canonical_uri: str = 'repo://docs/resampling.md',
) -> KnowledgeSource:
    """One titled source with one retrievable chunk for the chat to cite."""
    source = KnowledgeSource(
        source_type=SourceType.DOCUMENTATION,
        canonical_uri=canonical_uri,
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


def _write_artifact(
    settings,
    run_id: str,
    ref: str,
    body: bytes,
    *,
    artifact_type: str = 'report',
) -> ArtifactRecord:
    path = Path(settings.shared_mount_root) / run_id / ref
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return ArtifactRecord(
        run_id=run_id,
        type=artifact_type,
        uri=f'artifact://{run_id}/{ref}',
        sha256=sha256(body).hexdigest(),
        metadata={'path': str(path)},
    )


def _write_report(settings, run_id: str, body: bytes) -> ArtifactRecord:
    return _write_artifact(settings, run_id, REPORT_REF, body)


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


def test_ui_requires_operator_token_and_renders_three_columns(
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
    text = authenticated.text
    # The NotebookLM-style notebook: a static masthead plus Sources, Ask the
    # corpus, and Viewer columns in one grid.
    assert '<main class="notebook">' in text
    for column in ('id="sources"', 'id="ask"', 'id="viewer"'):
        assert column in text
    for heading in (
        '<h2>Sources</h2>',
        '<h2>Ask the corpus</h2>',
        '<h2>Viewer</h2>',
    ):
        assert heading in text
    # The former top-level Document and Evidence inspector panes are gone; the
    # artifact/file navigation moved to the Viewer and the evidence inspector
    # is folded into the Sources column.
    assert 'id="document"' not in text
    assert 'id="evidence"' not in text
    assert '<h2>Document</h2>' not in text
    assert '<h2>Evidence inspector</h2>' not in text
    assert '<h3>Evidence inspector</h3>' in text
    # Default view: the viewer asks for a selection instead of rendering one.
    assert 'Select a source or run.' in text
    # Links stay root-relative: the page is reached through the loopback proxy,
    # never rewritten to an absolute 127.0.0.1:18080/19090 origin.
    assert f'/ui/?run={run.run_id}' in text
    assert '127.0.0.1' not in text


@pytest.mark.parametrize(
    ('params', 'checked'),
    [
        ({}, 'tab-runs'),
        ({'run': 'run-placeholder'}, 'tab-runs'),
        ({'run': 'run-placeholder', 'ref': REPORT_REF}, 'tab-runs'),
        ({'source': 'source-placeholder'}, 'tab-corpus'),
        ({'packet': 'packet-placeholder'}, 'tab-packets'),
    ],
)
def test_ui_server_selects_the_initial_sources_tab(
    orchestrator_bundle,
    params: dict[str, str],
    checked: str,
) -> None:
    """The checked radio must reflect the selection, since no script can.

    A request for a source or an inspected packet has to land on the tab that
    renders its content; a run or file selection stays on the runs tab.
    """
    settings, _, _, _, engine = orchestrator_bundle

    with _client(settings, engine) as client:
        response = client.get('/ui/', params=params, headers=AUTH_HEADERS)

    assert response.status_code == 200
    for tab_id in ('tab-runs', 'tab-corpus', 'tab-packets'):
        marker = f'id="{tab_id}" checked>' if tab_id == checked else f'id="{tab_id}">'
        assert marker in response.text
    # The strip is a radio group plus labels: the only mechanism that swaps
    # panels is the :checked sibling selector in the nonced stylesheet.
    for tab_id, label, panel_id in (
        ('tab-runs', 'Runs', 'panel-runs'),
        ('tab-corpus', 'Corpus sources', 'panel-corpus'),
        ('tab-packets', 'Context packets', 'panel-packets'),
    ):
        assert f'<label class="tab" for="{tab_id}">{label}</label>' in response.text
        assert f'id="{panel_id}"' in response.text
    assert '#tab-packets:checked ~ .tab-panels > #panel-packets{display:block}' in (
        response.text
    )
    assert '.tab-panel{display:none}' in response.text
    assert '<script' not in response.text


def test_ui_run_selection_renders_file_tree_and_verified_preview(
    orchestrator_bundle,
) -> None:
    settings, _, _, _, engine = orchestrator_bundle
    run = engine.create_run(RunCreateRequest(objective='show the artifact tree'))
    engine.store.save_artifact(
        _write_report(settings, run.run_id, b'# Findings\n')
    )
    engine.store.save_artifact(
        _write_artifact(
            settings,
            run.run_id,
            'protocol/plan.md',
            b'# Plan\n',
            artifact_type='protocol',
        )
    )
    engine.store.save_artifact(
        _write_artifact(
            settings,
            run.run_id,
            'beacon/heartbeat.txt',
            b'beating\n',
            artifact_type='beacon',
        )
    )
    engine.store.save_artifact(
        _write_artifact(
            settings,
            run.run_id,
            'plots/seed-1/loss.txt',
            b'0.5\n',
            artifact_type='plot',
        )
    )

    with _client(settings, engine) as client:
        tree = client.get(
            '/ui/',
            params={'run': run.run_id},
            headers=AUTH_HEADERS,
        )
        preview = client.get(
            '/ui/',
            params={'run': run.run_id, 'ref': REPORT_REF},
            headers=AUTH_HEADERS,
        )

    assert tree.status_code == 200
    # Folders are native <details> disclosures grouped by path segment; the
    # tree lives in the Viewer column, not the Sources column.
    viewer = tree.text.split('id="viewer"', 1)[1]
    assert '<details class="tree-folder" open>' in viewer
    for folder in ('reports/', 'protocol/', 'beacon/', 'seed-1/'):
        assert f'<summary>{folder}</summary>' in viewer
    # Linkable artifacts are links into the viewer; non-linkable refs are
    # listed without a preview link (the preview stays policy-gated).
    assert 'ref=reports%2Freport.md' in viewer
    assert 'ref=plots%2Fseed-1%2Floss.txt' in viewer
    assert 'plan.md' in viewer
    assert 'heartbeat.txt' in viewer
    assert 'ref=protocol%2Fplan.md' not in viewer
    assert 'ref=beacon%2Fheartbeat.txt' not in viewer
    assert (
        'Select a file in the tree to preview its digest-verified text.'
        in viewer
    )

    assert preview.status_code == 200
    assert '<h3>Preview</h3>' in preview.text
    assert '# Findings' in preview.text
    assert 'aria-current="page"' in preview.text


def test_ui_source_selection_renders_pdf_iframe_in_viewer(
    orchestrator_bundle,
    tmp_path,
) -> None:
    settings, _, _, _, engine = orchestrator_bundle
    raw_root = tmp_path / 'rag-raw'
    settings = settings.model_copy(
        update={'corpus_rag_raw_root': str(raw_root)}
    )
    pdf_path = raw_root / 'handbook.pdf'
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    pdf_path.write_bytes(b'%PDF-1.4\n%%EOF\n')
    source = _seed_chat_corpus(engine, canonical_uri=pdf_path.as_uri())

    with _client(settings, engine) as client:
        response = client.get(
            '/ui/',
            params={'source': source.source_id},
            headers=AUTH_HEADERS,
        )

    assert response.status_code == 200
    viewer = response.text.split('id="viewer"', 1)[1]
    assert viewer.count('<iframe') == 1
    assert f'source={source.source_id}' in html.unescape(viewer)
    assert 'id="tab-corpus" checked>' in response.text
    # The selected row is marked current in the corpus source table.
    assert 'aria-current="page"' in response.text


def test_ui_evidence_inspector_renders_inside_sources_panel(
    orchestrator_bundle,
) -> None:
    settings, _, _, _, engine = orchestrator_bundle
    run = engine.create_run(RunCreateRequest(objective='inspect evidence'))
    packet = _save_packet(
        engine,
        run,
        blocks=[_BLOCK],
        ranked_sources=[
            {'source_id': 'src-1', 'digest': 'a' * 64, 'score': 0.75}
        ],
    )

    with _client(settings, engine) as client:
        response = client.get(
            '/ui/',
            params={
                'run': run.run_id,
                'packet': packet.packet_id,
                'excerpt': _BLOCK,
            },
            headers=AUTH_HEADERS,
        )

    assert response.status_code == 200
    text = response.text
    panel_start = text.index('id="panel-packets"')
    panel_end = text.index('</aside>')
    ask_start = text.index('id="ask"')
    evidence = text.index('<h3>Evidence inspector</h3>')
    ranked = text.index('<h3>Ranked sources</h3>')
    # The evidence inspector is part of the Sources column: between the
    # Context packets panel and the chat column, never a top-level pane.
    assert panel_start < evidence < panel_end
    assert panel_start < ranked < panel_end
    assert panel_end < ask_start
    assert 'id="tab-packets" checked>' in text
    assert '✓ exact' in text


def test_ui_page_has_no_script_and_no_inline_style(
    orchestrator_bundle,
) -> None:
    settings, _, _, _, engine = orchestrator_bundle
    run = engine.create_run(RunCreateRequest(objective='stay script-free'))
    engine.store.save_artifact(
        _write_report(settings, run.run_id, b'# Findings\n')
    )

    with _client(settings, engine) as client:
        response = client.get(
            '/ui/',
            params={'run': run.run_id, 'ref': REPORT_REF, 'q': _CHAT_QUESTION},
            headers=AUTH_HEADERS,
        )

    assert response.status_code == 200
    text = response.text
    assert '<script' not in text
    assert '<noscript' not in text
    # All CSS is in the nonced stylesheet: no element carries style="…" and no
    # element carries an event-handler attribute.
    assert re.search(r'\sstyle="', text) is None
    assert re.search(r'\son(click|change|load|error|focus|submit)=', text) is None


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


def test_ui_head_200(orchestrator_bundle) -> None:
    """HEAD on the page route answers 200, matching the GET-only read route."""
    settings, _, _, _, engine = orchestrator_bundle

    with _client(settings, engine) as client:
        response = client.head('/ui/', headers=AUTH_HEADERS)

    assert response.status_code == 200


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


def test_ui_csp_sign_off_directives(orchestrator_bundle) -> None:
    """Pin the exact page CSP signed off for the corpus chat (#618).

    The chat is a zero-JS same-origin GET form, so ``form-action 'self'`` is
    required; the cited-source iframe needs ``frame-src 'self'``. Everything
    else stays default-deny, and there is deliberately no ``script-src``.
    """
    settings, _, _, _, engine = orchestrator_bundle

    with _client(settings, engine) as client:
        response = client.get('/ui/', headers=AUTH_HEADERS)

    assert response.status_code == 200
    csp = response.headers['content-security-policy']
    assert "default-src 'none'" in csp
    assert "form-action 'self'" in csp
    assert "frame-src 'self'" in csp
    assert 'script-src' not in csp


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
    # The marker's preview card links the store-resolved title, carries the
    # live locator verdict badge and the "View source" affordance; the excerpt
    # is quoted inline in the answer itself.
    assert f'<span class="cite-title">{_CHAT_TITLE}</span>' in response.text
    assert '<sup>1</sup>' in response.text
    assert _CHAT_CHUNK_TEXT in response.text
    assert '>✓ exact</span>' in response.text
    assert 'knowledge://' not in response.text


def test_ui_chat_inline_markers_replace_the_footnote_list(
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
    text = response.text
    # NotebookLM-style inline citation: the ordinal renders as a superscript
    # marker inside the marker anchor, right at the ``[1]`` position in the
    # answer text, and the old end-of-answer reference list is gone.
    assert re.search(
        r'<a class="cite" href="[^"]+"><sup>1</sup>', text
    ) is not None
    assert 'chat-citations' not in text
    assert '<ul class="chat-citations">' not in text
    # The raw bracketed ordinal is never emitted as literal answer text.
    assert '[1]' not in text
    # The card is revealed by CSS only, and the page still carries no script.
    assert '.cite:hover .cite-card,.cite:focus-within .cite-card' in text
    assert '<script' not in text


def test_ui_chat_marker_card_carries_title_badge_and_view_source(
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

    text = response.text
    assert 'class="cite-card"' in text
    assert f'<span class="cite-title">{_CHAT_TITLE}</span>' in text
    assert 'class="cite-badge badge-exact"' in text
    assert '<span class="cite-cta">View source</span>' in text
    # No inline style may carry the card geometry: all CSS stays in the
    # nonced stylesheet.
    assert re.search(r'\sstyle="', text) is None


def test_ui_chat_escapes_malicious_citation_title(orchestrator_bundle) -> None:
    settings, _, _, _, engine = orchestrator_bundle
    _seed_chat_corpus(engine, title='"><img src=x onerror=alert(1)>')
    chat_service = CorpusChatService(engine.store)

    with _client(settings, engine, chat_service=chat_service) as client:
        response = client.get(
            '/ui/',
            params={'q': _CHAT_QUESTION},
            headers=AUTH_HEADERS,
        )

    assert response.status_code == 200
    assert '<img' not in response.text
    assert '&lt;img src=x onerror=alert(1)&gt;' in response.text
    assert re.search(r'onerror="', response.text) is None
    assert '<script' not in response.text


def test_render_chat_answer_maps_valid_ordinals_and_keeps_invalid_literal(
) -> None:
    answer = ChatAnswer(
        answer='[1] "alpha" [2] "beta" [3] "gamma" [0] "zero" [999] "x" [9x]',
        citations=[
            ChatCitation(
                source_id='src-a',
                title='Alpha',
                excerpt='alpha',
                verdict='exact',
                page=0,
            ),
            ChatCitation(
                source_id='src-b',
                title='Beta',
                excerpt='beta',
                verdict='fuzzy',
                page=4,
            ),
        ],
    )

    rendered = _render_chat_answer('why?', answer)

    assert rendered.count('<a class="cite"') == 2
    assert '<sup>1</sup>' in rendered
    assert '<sup>2</sup>' in rendered
    # Out-of-range ordinals and malformed brackets stay literal text.
    for literal in ('[3]', '[0]', '[999]', '[9x]'):
        assert literal in rendered
    # [2] links to citations[1] with its 1-based human page number.
    second = re.search(
        r'<a class="cite" href="([^"]+)"><sup>2</sup>', rendered
    )
    assert second is not None
    query = parse_qs(urlsplit(html.unescape(second.group(1))).query)
    assert query['q'] == ['why?']
    assert query['source'] == ['src-b']
    assert query['page'] == ['5']
    assert query['excerpt'] == ['beta']
    # [1] carries the first citation's title and verdict in its card.
    first_card = re.search(
        r'<a class="cite" href="[^"]+"><sup>1</sup>(.*?)</a>', rendered
    )
    assert first_card is not None
    assert '<span class="cite-title">Alpha</span>' in first_card.group(1)
    assert 'class="cite-badge badge-exact"' in first_card.group(1)
    # With no citations there is no valid ordinal: the bracket stays literal.
    assert (
        _render_chat_answer('q', ChatAnswer(answer='[1] no citations'))
        == '[1] no citations'
    )


def test_render_chat_answer_escapes_hostile_citation_fields() -> None:
    payload = '<script>alert(1)</script>'
    answer = ChatAnswer(
        answer=f'[1] "safe" {payload}',
        citations=[
            ChatCitation(
                source_id='src-a',
                title='</span><img src=x onerror=alert(1)>',
                excerpt=f'quote {payload}',
                verdict='exact',
                page=0,
            )
        ],
    )

    rendered = _render_chat_answer('q', answer)

    assert '<script' not in rendered
    assert '<img' not in rendered
    assert '<a class="cite" href=' in rendered
    assert '&lt;script&gt;alert(1)&lt;/script&gt;' in rendered
    assert '&lt;/span&gt;&lt;img src=x onerror=alert(1)&gt;' in rendered
    assert re.search(r'onerror="', rendered) is None


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
    tmp_path,
) -> None:
    settings, _, _, _, engine = orchestrator_bundle
    raw_root = tmp_path / 'rag-raw'
    settings = settings.model_copy(
        update={'corpus_rag_raw_root': str(raw_root)}
    )
    pdf_path = raw_root / 'resampling.pdf'
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    pdf_path.write_bytes(b'%PDF-1.4\n%%EOF\n')
    source = _seed_chat_corpus(engine, canonical_uri=pdf_path.as_uri())
    chat_service = CorpusChatService(engine.store)

    with _client(settings, engine, chat_service=chat_service) as client:
        page = client.get(
            '/ui/',
            params={'q': _CHAT_QUESTION},
            headers=AUTH_HEADERS,
        )
        # The chat citation is the source link that also carries the question;
        # the corpus index links a source without q.
        match = re.search(r'href="(/ui/\?[^"]*q=[^"]*source=[^"]*)"', page.text)
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


def test_cited_source_omits_iframe_when_unresolvable(
    orchestrator_bundle,
    tmp_path,
) -> None:
    """A cited source that cannot resolve must not emit a dead viewer iframe.

    The iframe points at the viewer wrapper, which would then fetch a 404
    document; instead the page states that the document is unavailable.
    """
    settings, _, _, _, engine = orchestrator_bundle
    raw_root = tmp_path / 'rag-raw'
    settings = settings.model_copy(
        update={'corpus_rag_raw_root': str(raw_root)}
    )
    unresolvable = KnowledgeSource(
        source_type=SourceType.PAPER,
        canonical_uri='upload://x',
        digest='a' * 64,
        title='Uploaded paper',
    )
    engine.store.save_knowledge_source(unresolvable)
    pdf_path = raw_root / 'ok.pdf'
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    pdf_path.write_bytes(b'%PDF-1.4\n%%EOF\n')
    resolvable = KnowledgeSource(
        source_type=SourceType.PAPER,
        canonical_uri=pdf_path.as_uri(),
        digest='b' * 64,
        title='Local paper',
    )
    engine.store.save_knowledge_source(resolvable)

    with _client(settings, engine) as client:
        missing = client.get(
            '/ui/',
            params={'source': unresolvable.source_id},
            headers=AUTH_HEADERS,
        )
        present = client.get(
            '/ui/',
            params={'source': resolvable.source_id},
            headers=AUTH_HEADERS,
        )

    assert missing.status_code == 200
    assert '<iframe' not in missing.text
    assert 'The cited source document is not available.' in missing.text
    assert 'upload://' not in missing.text
    assert present.status_code == 200
    assert '<iframe' in present.text


def test_ui_cited_source_renders_stored_text_when_pdf_missing(
    orchestrator_bundle,
) -> None:
    """A source with no servable PDF falls back to its extracted rag chunks."""
    settings, _, _, _, engine = orchestrator_bundle
    source = _seed_chat_corpus(engine)

    with _client(settings, engine) as client:
        response = client.get(
            '/ui/',
            params={'source': source.source_id},
            headers=AUTH_HEADERS,
        )

    assert response.status_code == 200
    viewer = response.text.split('id="viewer"', 1)[1]
    # No dead-end iframe and no dead-end message: the stored text renders in
    # the viewer with the source title and the chunk's provenance line.
    assert '<iframe' not in viewer
    assert '<div class="source-text">' in viewer
    assert f'<p class="source-title">{_CHAT_TITLE}</p>' in viewer
    assert f'<p class="chunk-text">{_CHAT_CHUNK_TEXT}</p>' in viewer
    assert '<p class="chunk-meta muted">page 3</p>' in viewer
    assert 'The cited source document is not available.' not in response.text


def test_ui_cited_source_text_orders_chunks_by_chunk_index(
    orchestrator_bundle,
) -> None:
    settings, _, _, _, engine = orchestrator_bundle
    source = KnowledgeSource(
        source_type=SourceType.PAPER,
        canonical_uri='repo://docs/two-chunks.md',
        digest='d' * 64,
        title='Two chunks',
    )
    engine.store.save_knowledge_source(source)
    # Inserted out of order: the store's (source_id, chunk_index) ordering
    # must put the reader back in chunk-index order.
    engine.store.replace_rag_chunks(
        source.source_id,
        [
            RagChunkRecord(
                chunk_id=f'{source.source_id}::c1',
                source_id=source.source_id,
                kind='evidence_span',
                chunk_index=1,
                text='second chunk',
                digest=sha256(b'second chunk').hexdigest(),
                token_count=2,
            ),
            RagChunkRecord(
                chunk_id=f'{source.source_id}::c0',
                source_id=source.source_id,
                kind='evidence_span',
                chunk_index=0,
                text='first chunk',
                digest=sha256(b'first chunk').hexdigest(),
                token_count=2,
            ),
        ],
    )

    with _client(settings, engine) as client:
        response = client.get(
            '/ui/',
            params={'source': source.source_id},
            headers=AUTH_HEADERS,
        )

    assert response.status_code == 200
    text = response.text
    assert 'first chunk' in text
    assert 'second chunk' in text
    assert text.index('first chunk') < text.index('second chunk')


def test_ui_cited_source_text_marks_the_cited_excerpt(
    orchestrator_bundle,
) -> None:
    settings, _, _, _, engine = orchestrator_bundle
    source = _seed_chat_corpus(engine)

    with _client(settings, engine) as client:
        response = client.get(
            '/ui/',
            params={
                'source': source.source_id,
                'excerpt': 'STABILITY OF SMALL SAMPLES',
            },
            headers=AUTH_HEADERS,
        )

    assert response.status_code == 200
    text = response.text
    # The callout quotes the excerpt; the case-insensitive match inside the
    # chunk text is wrapped in a server-rendered mark, never a script.
    assert '<div class="excerpt-callout">' in text
    assert '<strong>Cited excerpt</strong>' in text
    assert '<p class="excerpt-text">STABILITY OF SMALL SAMPLES</p>' in text
    assert '<mark>stability of small samples</mark>' in text
    assert '<script' not in text
    assert re.search(r'\sstyle="', text) is None


def test_ui_cited_source_without_chunks_states_no_text_stored(
    orchestrator_bundle,
) -> None:
    settings, _, _, _, engine = orchestrator_bundle
    source = KnowledgeSource(
        source_type=SourceType.DOCUMENTATION,
        canonical_uri='repo://docs/empty.md',
        digest='c' * 64,
        title='Empty source',
    )
    engine.store.save_knowledge_source(source)

    with _client(settings, engine) as client:
        response = client.get(
            '/ui/',
            params={'source': source.source_id},
            headers=AUTH_HEADERS,
        )

    assert response.status_code == 200
    assert 'No extracted text is stored for this source.' in response.text
    assert 'The cited source document is not available.' in response.text
    assert '<iframe' not in response.text


def test_ui_cited_source_text_viewer_stays_zero_js(
    orchestrator_bundle,
) -> None:
    settings, _, _, _, engine = orchestrator_bundle
    source = _seed_chat_corpus(engine)

    with _client(settings, engine) as client:
        response = client.get(
            '/ui/',
            params={
                'source': source.source_id,
                'excerpt': _CHAT_CHUNK_TEXT,
            },
            headers=AUTH_HEADERS,
        )

    assert response.status_code == 200
    text = response.text
    assert '<script' not in text
    assert '<noscript' not in text
    # All CSS is in the nonced stylesheet: no element carries style="…" and no
    # element carries an event-handler attribute.
    assert re.search(r'\sstyle="', text) is None
    assert re.search(r'\son(click|change|load|error|focus|submit)=', text) is None


def test_ui_cited_source_text_escapes_hostile_chunk_and_excerpt(
    orchestrator_bundle,
) -> None:
    settings, _, _, _, engine = orchestrator_bundle
    payload = '<script>alert(1)</script>'
    source = _seed_chat_corpus(engine, text=f'Extracted text {payload} here')

    with _client(settings, engine) as client:
        response = client.get(
            '/ui/',
            params={'source': source.source_id, 'excerpt': payload},
            headers=AUTH_HEADERS,
        )

    assert response.status_code == 200
    text = response.text
    assert '<script' not in text
    # Both the callout and the marked body span carry escaped text only.
    assert (
        '<p class="excerpt-text">&lt;script&gt;alert(1)&lt;/script&gt;</p>'
        in text
    )
    assert '<mark>&lt;script&gt;alert(1)&lt;/script&gt;</mark>' in text


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
    match = re.search(r'href="(/ui/\?[^"]*q=[^"]*source=[^"]*)"', page.text)
    assert match is not None
    citation_query = parse_qs(urlsplit(html.unescape(match.group(1))).query)
    assert 'page' not in citation_query
