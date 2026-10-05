"""Read-only corpus notebook: columns, tabs, chat, escaping, badges, and CSP.

The page is server-rendered with no client JavaScript of its own and no
external resource, so these tests drive it with ``TestClient`` and assert on
the escaped HTML: the three-column notebook (Sources with CSS-only tabs, Ask
the corpus, Viewer), the run file tree and digest-verified preview, injected
``<script>`` payloads staying inert, ranked-source URIs and filesystem paths
never being emitted, and the evidence badge coming from the deterministic
citation locator rather than the stored (tautological)
``ranked_sources[].verified`` flag.

The corpus chat is a persistent multi-turn conversation: the composer posts a
turn to ``/ui/chat``, which persists it and 303-redirects to
``/ui/?c=<id>#latest``; ``GET /ui/?c=`` replays it. The answer's valid ``[n]``
ordinals become inline superscript citation markers (there is no end-of-answer
reference list), each with a CSS-only hover/focus preview card that carries the
source title, the verdict badge, and a "View source" affordance. The marker's
link preserves the conversation and selects the cited source in the side panel,
which embeds the same-origin PDF viewer iframe. The CSP tests pin the two
deliberate deltas: ``form-action 'self'`` and ``frame-src 'self'``; everything
else still denies by default, and the document still carries no script and no
external origin.

The tests use the repository's ``orchestrator_bundle`` fixture: a real
SqliteStore engine with a fake runtime, no live cluster, and no network.
"""

from __future__ import annotations

import html
import io
import json
import re
import shutil
import zipfile
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from typing import Literal
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient
import pymupdf
import pytest

from app.corpus_rag.chat import CorpusChatService
from app.corpus_rag.contracts import RagChunkRecord
from app.knowledge_manager import KnowledgeError
from app.schemas import (
    ActionRecord,
    AgentName,
    AgentTurnResult,
    ApprovalStatus,
    ArtifactRecord,
    ContextPacket,
    KnowledgeSource,
    PolicyClassification,
    RunCreateRequest,
    RunRecord,
    RunState,
    SourceType,
    TurnKind,
    TurnRecord,
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


def _ask(
    client: TestClient,
    question: str,
    *,
    conversation_id: str = '',
):
    """Post one chat turn and follow the 303 to the rendered conversation."""
    response = client.post(
        '/ui/chat',
        data={'q': question, 'c': conversation_id},
        headers=AUTH_HEADERS,
        follow_redirects=False,
    )
    if response.status_code != 303:
        return response
    return client.get(response.headers['location'], headers=AUTH_HEADERS)


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


class _ScriptedLlm:
    """Duck-typed ``complete`` provider returning a fixed JSON string."""

    def __init__(self, response: str) -> None:
        self._response = response

    def complete(self, *, system: str, user: str) -> str:
        return self._response


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


def _seed_turn(
    engine,
    run_id: str,
    *,
    agent: AgentName = AgentName.HONEYDEW,
    status: Literal['running', 'completed', 'failed', 'aborted'] = 'completed',
    kind: TurnKind = TurnKind.PROTOCOL_DRAFT,
    summary: str = 'drafted the protocol',
    error: str | None = None,
    created_at: datetime | None = None,
) -> TurnRecord:
    """Persist one agent turn through the real store, as the engine does."""
    started = created_at or datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    turn = TurnRecord(
        run_id=run_id,
        agent=agent,
        input_event={'event': 'agent_turn'},
        structured_output=AgentTurnResult(kind=kind, summary=summary),
        status=status,
        error=error,
        created_at=started,
        updated_at=started + timedelta(minutes=5),
    )
    engine.store.save_turn(turn)
    return turn


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


def test_ui_narrow_layout_bounds_the_corpus_and_text_panels(
    orchestrator_bundle,
) -> None:
    """Below 1100px the two content-sized regions keep their internal scroller.

    The narrow media query once returned the tab panels to document flow,
    which let a ~1400-row corpus table grow the document to tens of thousands
    of pixels; the panels and the extracted-text reader must stay bounded at
    every width.
    """
    settings, _, _, _, engine = orchestrator_bundle

    with _client(settings, engine) as client:
        response = client.get('/ui/', headers=AUTH_HEADERS)

    assert response.status_code == 200
    assert (
        '.tab-panels{overflow:auto;max-block-size:65vh;'
        'overscroll-behavior:contain}' in response.text
    )
    assert '.source-text{max-block-size:65vh}' in response.text
    assert '.column-body{overflow:visible}' in response.text
    assert (
        '.column-body.is-fill{display:block;overflow:visible}' in response.text
    )
    assert '.column-body,.tab-panels{overflow:visible}' not in response.text
    # The Viewer keeps its tab panels in document flow when stacked, and the
    # fill panel drops its fixed height so the 70vh PDF iframe is not clipped.
    assert '#viewer .tab-panels{max-block-size:none;overflow:visible}' in (
        response.text
    )
    assert (
        '#rtab-viewer:checked ~ .tab-panels > #rpanel-viewer.is-fill'
        '{display:block;' in response.text
    )


def test_ui_right_pane_renders_turns_and_viewer_tabs(
    orchestrator_bundle,
) -> None:
    """The Viewer column is a second zero-JS tab strip: Turns | Viewer."""
    settings, _, _, _, engine = orchestrator_bundle

    with _client(settings, engine) as client:
        response = client.get('/ui/', headers=AUTH_HEADERS)

    assert response.status_code == 200
    text = response.text
    assert 'name="right-tab"' in text
    for tab_id, label, panel_id in (
        ('rtab-turns', 'Turns', 'rpanel-turns'),
        ('rtab-viewer', 'Viewer', 'rpanel-viewer'),
    ):
        assert f'<label class="tab" for="{tab_id}">{label}</label>' in text
        assert f'id="{panel_id}"' in text
    # The swap is the same :checked sibling mechanism as the Sources strip.
    assert (
        '#rtab-turns:checked ~ .tab-panels > #rpanel-turns,' in text
    )
    assert (
        '#rtab-viewer:checked ~ .tab-panels > #rpanel-viewer{display:block}'
        in text
    )
    # The cited-source PDF fill survives the tab wrapper: the checked panel is
    # a full-height flex column for the .is-fill body.
    assert (
        '#rtab-viewer:checked ~ .tab-panels > #rpanel-viewer.is-fill'
        '{display:flex;' in text
    )
    assert 'flex-direction:column;block-size:100%;overflow:hidden}' in text
    assert 'Select a run to list its agent turns.' in text
    assert '<script' not in text
    assert re.search(r'\sstyle="', text) is None


@pytest.mark.parametrize(
    ('params', 'checked'),
    [
        ({}, 'rtab-viewer'),
        ({'run': 'run-placeholder'}, 'rtab-turns'),
        ({'run': 'run-placeholder', 'ref': REPORT_REF}, 'rtab-viewer'),
        ({'ref': REPORT_REF}, 'rtab-viewer'),
        ({'source': 'source-placeholder'}, 'rtab-viewer'),
    ],
)
def test_ui_server_selects_the_initial_right_tab(
    orchestrator_bundle,
    params: dict[str, str],
    checked: str,
) -> None:
    """A run selection opens Turns; a source or file selection opens Viewer."""
    settings, _, _, _, engine = orchestrator_bundle

    with _client(settings, engine) as client:
        response = client.get('/ui/', params=params, headers=AUTH_HEADERS)

    assert response.status_code == 200
    for tab_id in ('rtab-turns', 'rtab-viewer'):
        marker = (
            f'id="{tab_id}" checked>'
            if tab_id == checked
            else f'id="{tab_id}">'
        )
        assert marker in response.text


def test_ui_run_selection_lists_agent_turns(
    orchestrator_bundle,
) -> None:
    """A selected run lists its redacted TurnSummaries in storage order."""
    settings, _, _, _, engine = orchestrator_bundle
    run = engine.create_run(RunCreateRequest(objective='list the turns'))
    first = _seed_turn(
        engine,
        run.run_id,
        agent=AgentName.HONEYDEW,
        status='completed',
        kind=TurnKind.PROTOCOL_DRAFT,
        summary='drafted the protocol for review',
        created_at=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
    )
    _seed_turn(
        engine,
        run.run_id,
        agent=AgentName.BEAKER,
        status='failed',
        kind=TurnKind.IMPLEMENTATION_PLAN,
        summary='planned the implementation matrix',
        error='RuntimeError: model endpoint unavailable',
        created_at=datetime(2026, 1, 1, 13, 0, tzinfo=timezone.utc),
    )

    with _client(settings, engine) as client:
        response = client.get(
            '/ui/',
            params={'run': run.run_id},
            headers=AUTH_HEADERS,
        )

    assert response.status_code == 200
    text = response.text
    assert 'id="rtab-turns" checked>' in text
    assert 'id="rtab-viewer">' in text
    turns_panel = text[
        text.index('id="rpanel-turns"') : text.index('id="rpanel-viewer"')
    ]
    assert '<strong>Honeydew</strong>' in turns_panel
    assert '<strong>Beaker</strong>' in turns_panel
    assert '<code>protocol_draft</code>' in turns_panel
    assert '<code>implementation_plan</code>' in turns_panel
    assert '<code>completed</code>' in turns_panel
    assert '<code>failed</code>' in turns_panel
    assert 'drafted the protocol for review' in turns_panel
    assert 'planned the implementation matrix' in turns_panel
    assert 'RuntimeError: model endpoint unavailable' in turns_panel
    assert first.created_at.isoformat() in turns_panel
    assert first.updated_at.isoformat() in turns_panel
    assert turns_panel.index('drafted the protocol') < turns_panel.index(
        'planned the implementation'
    )
    # Only summarize_turns output is rendered; the raw input_event stays out.
    assert 'agent_turn' not in turns_panel
    assert re.search(r'\sstyle="', text) is None


def test_ui_turns_panel_empty_states(orchestrator_bundle) -> None:
    settings, _, _, _, engine = orchestrator_bundle

    with _client(settings, engine) as client:
        no_selection = client.get('/ui/', headers=AUTH_HEADERS)
        empty_run = client.get(
            '/ui/',
            params={'run': 'run-with-no-turns'},
            headers=AUTH_HEADERS,
        )

    assert 'Select a run to list its agent turns.' in no_selection.text
    assert 'id="rtab-viewer" checked>' in no_selection.text
    assert 'no agent turns recorded for this run.' in empty_run.text
    assert 'id="rtab-turns" checked>' in empty_run.text


def test_ui_turns_panel_escapes_agent_output(orchestrator_bundle) -> None:
    settings, _, _, _, engine = orchestrator_bundle
    run = engine.create_run(RunCreateRequest(objective='escape the turns'))
    payload = '<script>alert(1)</script>'
    _seed_turn(
        engine,
        run.run_id,
        kind=TurnKind.EXPERIMENT_ANALYSIS,
        summary=f'analysis {payload}',
        error=f'failure {payload}',
    )

    with _client(settings, engine) as client:
        response = client.get(
            '/ui/',
            params={'run': run.run_id},
            headers=AUTH_HEADERS,
        )

    assert response.status_code == 200
    text = response.text
    assert '<script' not in text
    assert '&lt;script&gt;alert(1)&lt;/script&gt;' in text
    assert re.search(r'\sstyle="', text) is None
    csp = response.headers['content-security-policy']
    assert "default-src 'none'" in csp
    assert 'script-src' not in csp


def test_ui_pdf_source_keeps_the_viewer_panel_fill(
    orchestrator_bundle,
    tmp_path,
) -> None:
    """The cited-source body lives in the .is-fill tab panel, unchanged."""
    settings, _, _, _, engine = orchestrator_bundle
    raw_root = tmp_path / 'rag-raw'
    settings = settings.model_copy(
        update={'corpus_rag_raw_root': str(raw_root)}
    )
    pdf_path = raw_root / 'handbook.pdf'
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    pdf_path.write_bytes(b'%PDF-1.4\n%%EOF\n')
    source = _seed_chat_corpus(engine, canonical_uri=pdf_path.as_uri())
    text_source = _seed_chat_corpus(
        engine,
        title='Extracted Only',
        text='stored extracted text only',
        canonical_uri='doc://extracted-only',
    )
    run = engine.create_run(RunCreateRequest(objective='run without a pdf'))

    with _client(settings, engine) as client:
        pdf_page = client.get(
            '/ui/',
            params={'source': source.source_id},
            headers=AUTH_HEADERS,
        )
        text_page = client.get(
            '/ui/',
            params={'source': text_source.source_id},
            headers=AUTH_HEADERS,
        )
        run_page = client.get(
            '/ui/',
            params={'run': run.run_id},
            headers=AUTH_HEADERS,
        )

    assert pdf_page.status_code == 200
    assert 'id="rtab-viewer" checked>' in pdf_page.text
    assert (
        '<section class="tab-panel is-fill" id="rpanel-viewer">'
        in pdf_page.text
    )
    assert '<div class="column-body is-fill">' in pdf_page.text
    assert '<div class="pdf-viewer">' in pdf_page.text
    # The extracted-text fallback uses the same fill panel so .source-text
    # keeps scrolling internally instead of collapsing.
    assert (
        '<section class="tab-panel is-fill" id="rpanel-viewer">'
        in text_page.text
    )
    assert '<div class="source-text">' in text_page.text
    # A run selection is not the fill case: its tree scrolls in .tab-panels.
    assert (
        '<section class="tab-panel" id="rpanel-viewer">' in run_page.text
    )


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
            params={'run': run.run_id, 'ref': REPORT_REF},
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


def test_ui_chat_form_posts_and_persists_turns(orchestrator_bundle) -> None:
    settings, _, _, _, engine = orchestrator_bundle
    _seed_chat_corpus(engine)
    chat_service = CorpusChatService(engine.store)

    with _client(settings, engine, chat_service=chat_service) as client:
        page = client.get('/ui/', headers=AUTH_HEADERS)
        answered = _ask(client, _CHAT_QUESTION)

    assert page.status_code == 200
    assert '<section class="pane" id="ask">' in page.text
    assert '<h2>Ask the corpus</h2>' in page.text
    assert '<form method="post" action="/ui/chat"' in page.text
    assert 'name="q"' in page.text
    # A fresh page shows no turn; a posted question persists a turn and the
    # 303 replays it from the stored conversation.
    assert _CHAT_CHUNK_TEXT not in page.text
    assert answered.status_code == 200
    assert _CHAT_CHUNK_TEXT in answered.text
    assert 'id="latest"' in answered.text


def test_ui_chat_renders_source_title_excerpt_and_badge(
    orchestrator_bundle,
) -> None:
    settings, _, _, _, engine = orchestrator_bundle
    _seed_chat_corpus(engine)
    chat_service = CorpusChatService(engine.store)

    with _client(settings, engine, chat_service=chat_service) as client:
        response = _ask(client, _CHAT_QUESTION)

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
        response = _ask(client, _CHAT_QUESTION)

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
        response = _ask(client, _CHAT_QUESTION)

    text = response.text
    assert 'class="cite-card"' in text
    assert f'<span class="cite-title">{_CHAT_TITLE}</span>' in text
    assert 'class="cite-badge badge-exact"' in text
    assert '<span class="cite-cta">View source</span>' in text
    # No inline style may carry the card geometry: all CSS stays in the
    # nonced stylesheet.
    assert re.search(r'\sstyle="', text) is None


def test_ui_chat_llm_answer_renders_inline_citation_markers(
    orchestrator_bundle,
) -> None:
    settings, _, _, _, engine = orchestrator_bundle
    _seed_chat_corpus(engine)
    chat_service = CorpusChatService(
        engine.store,
        llm=_ScriptedLlm(
            '{"answer": "Resampling improves stability of small samples [1].",'
            ' "citations": [{"evidence_id": "E1"}]}'
        ),
    )

    with _client(settings, engine, chat_service=chat_service) as client:
        response = _ask(client, _CHAT_QUESTION)

    assert response.status_code == 200
    text = response.text
    assert re.search(
        r'<a class="cite" href="[^"]+"><sup>1</sup>', text
    ) is not None
    assert 'class="cite-card"' in text
    assert f'<span class="cite-title">{_CHAT_TITLE}</span>' in text
    assert 'class="cite-badge badge-exact"' in text
    assert 'chat-citations' not in text
    assert '[1]' not in text


def test_render_chat_answer_keeps_out_of_range_marker_literal() -> None:
    answer = ChatAnswer(
        answer='Only one source [1] but a stray marker [2] stays literal.',
        citations=[
            ChatCitation(
                source_id='src-a',
                title='Alpha',
                excerpt='alpha',
                verdict='exact',
                page=0,
            )
        ],
    )

    rendered = _render_chat_answer('q', answer)

    assert rendered.count('<a class="cite"') == 1
    assert '<sup>1</sup>' in rendered
    assert '[2]' in rendered


def test_ui_chat_escapes_malicious_citation_title(orchestrator_bundle) -> None:
    settings, _, _, _, engine = orchestrator_bundle
    _seed_chat_corpus(engine, title='"><img src=x onerror=alert(1)>')
    chat_service = CorpusChatService(engine.store)

    with _client(settings, engine, chat_service=chat_service) as client:
        response = _ask(client, _CHAT_QUESTION)

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
        response = _ask(client, _CHAT_QUESTION)

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
        response = _ask(client, question)

    assert response.status_code == 200
    assert '<script' not in response.text
    assert '&lt;script&gt;alert(1)&lt;/script&gt;' in response.text
    assert secret not in response.text
    assert '[REDACTED]' in response.text
    # Ordinary prose around the payload and the secret survives unchanged.
    assert 'resampling stability with' in response.text
    assert 'improves stability of' in response.text


def test_ui_citation_link_preserves_conversation_and_targets_source_panel(
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
        page = _ask(client, _CHAT_QUESTION)
        # The chat citation is the source link that also carries the
        # conversation; the corpus index links a source without c.
        match = re.search(r'href="(/ui/\?[^"]*c=[^"]*source=[^"]*)"', page.text)
        assert match is not None
        citation_href = html.unescape(match.group(1))
        followed = client.get(citation_href, headers=AUTH_HEADERS)

    assert page.status_code == 200
    citation_query = parse_qs(urlsplit(citation_href).query)
    assert citation_query['c'] and citation_query['c'][0]
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


def test_ui_cited_source_text_truncates_past_the_chunk_cap(
    orchestrator_bundle,
    monkeypatch,
) -> None:
    settings, _, _, _, engine = orchestrator_bundle
    monkeypatch.setattr('app.ui._MAX_SOURCE_TEXT_CHUNKS', 3)
    source = KnowledgeSource(
        source_type=SourceType.PAPER,
        canonical_uri='repo://docs/long.md',
        digest='e' * 64,
        title='Long source',
    )
    engine.store.save_knowledge_source(source)
    engine.store.replace_rag_chunks(
        source.source_id,
        [
            RagChunkRecord(
                chunk_id=f'{source.source_id}::c{index}',
                source_id=source.source_id,
                kind='evidence_span',
                chunk_index=index,
                text=f'chunk number {index}',
                digest=sha256(f'chunk number {index}'.encode()).hexdigest(),
                token_count=3,
            )
            for index in range(5)
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
    # Exactly the cap renders, in chunk-index order, and the overflow is
    # disclosed instead of silently dropped.
    assert text.count('<article class="source-chunk">') == 3
    for index in range(3):
        assert f'chunk number {index}' in text
    for index in range(3, 5):
        assert f'chunk number {index}' not in text
    assert (
        'Showing the first 3 extracted chunks; this source has more.' in text
    )
    assert '<script' not in text


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
        page = _ask(client, _CHAT_QUESTION)

    assert page.status_code == 200
    match = re.search(r'href="(/ui/\?[^"]*c=[^"]*source=[^"]*)"', page.text)
    assert match is not None
    citation_query = parse_qs(urlsplit(html.unescape(match.group(1))).query)
    assert 'page' not in citation_query


def _raw_settings(settings, tmp_path):
    return settings.model_copy(
        update={'corpus_rag_raw_root': str(tmp_path / 'rag-raw')}
    )


def _make_real_pdf(text: str = 'Bootstrap resampling estimates uncertainty'):
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 72), text)
    return document.tobytes()


def test_ui_sources_column_renders_zero_js_upload_form(
    orchestrator_bundle,
) -> None:
    settings, _, _, _, engine = orchestrator_bundle

    with _client(settings, engine) as client:
        response = client.get('/ui/', headers=AUTH_HEADERS)

    assert response.status_code == 200
    text = response.text
    sources = text.split('id="sources"', 1)[1].split('id="ask"', 1)[0]
    # The form is a native multipart POST in the Sources column header: no
    # script, no inline style, and the same-origin form-action already allows
    # it. The upload CSS lives in the nonced stylesheet like every other rule.
    assert (
        '<form method="post" action="/ui/sources/upload" '
        'enctype="multipart/form-data" class="upload-form">' in sources
    )
    assert 'name="file"' in sources
    assert 'accept="application/pdf"' in sources
    assert '.upload-form{' in text
    assert '<script' not in text
    assert re.search(r'\sstyle="', text) is None


def test_ui_upload_requires_operator_token(orchestrator_bundle) -> None:
    settings, _, _, _, engine = orchestrator_bundle

    with _client(settings, engine) as client:
        response = client.post(
            '/ui/sources/upload',
            files={'file': ('book.pdf', b'%PDF-1.4\n%%EOF\n')},
        )

    assert response.status_code == 401


def test_ui_upload_rejects_missing_file(orchestrator_bundle) -> None:
    settings, _, _, _, engine = orchestrator_bundle

    with _client(settings, engine) as client:
        response = client.post('/ui/sources/upload', headers=AUTH_HEADERS)

    assert response.status_code == 422


def test_ui_upload_rejects_non_pdf(orchestrator_bundle, tmp_path) -> None:
    settings, _, _, _, engine = orchestrator_bundle
    settings = _raw_settings(settings, tmp_path)

    with _client(settings, engine) as client:
        response = client.post(
            '/ui/sources/upload',
            files={'file': ('notes.txt', b'not a pdf at all')},
            headers=AUTH_HEADERS,
        )

    assert response.status_code == 415
    assert 'Only PDF files can be uploaded.' in response.text
    assert not (tmp_path / 'rag-raw').exists()


def test_ui_upload_rejects_oversized_body(
    orchestrator_bundle,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, _, _, _, engine = orchestrator_bundle
    settings = _raw_settings(settings, tmp_path)
    monkeypatch.setattr('app.ui.MAXIMUM_UI_UPLOAD_BYTES', 16)

    with _client(settings, engine) as client:
        response = client.post(
            '/ui/sources/upload',
            files={'file': ('big.pdf', b'%PDF-1.4' + b'x' * 32)},
            headers=AUTH_HEADERS,
        )

    assert response.status_code == 413
    assert 'exceeds the upload size limit' in response.text
    assert not (tmp_path / 'rag-raw').exists()


def test_ui_upload_success_redirects_and_calls_ingest(
    orchestrator_bundle,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, _, _, _, engine = orchestrator_bundle
    settings = _raw_settings(settings, tmp_path)
    pdf = b'%PDF-1.4\n%\xe2\xe3\xcf\xd3\ntrailer\n%%EOF\n'
    calls: list[dict] = []

    def fake_ingest(**kwargs: object) -> SimpleNamespace:
        calls.append(kwargs)
        return SimpleNamespace(source_id='src-uploaded')

    monkeypatch.setattr('app.ui.ingest_document', fake_ingest)

    with _client(settings, engine) as client:
        response = client.post(
            '/ui/sources/upload',
            files={'file': ('textbook.pdf', pdf, 'application/pdf')},
            headers=AUTH_HEADERS,
            follow_redirects=False,
        )

    assert response.status_code == 303
    # The corpus tab is selected by the source selection the redirect carries,
    # so the reload lands on the new source row and its viewer.
    assert response.headers['location'] == '/ui/?source=src-uploaded'
    assert len(calls) == 1
    assert calls[0]['title'] == 'textbook.pdf'
    assert calls[0]['doc_type'] == 'book'
    staged = tmp_path / 'rag-raw' / f'{sha256(pdf).hexdigest()}.pdf'
    assert staged.read_bytes() == pdf
    assert calls[0]['canonical_uri'] == staged.resolve().as_uri()


def test_ui_upload_ingest_failure_removes_staged_bytes(
    orchestrator_bundle,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, _, _, _, engine = orchestrator_bundle
    settings = _raw_settings(settings, tmp_path)
    pdf = b'%PDF-1.4\n%%EOF\n'

    def reject(**kwargs: object) -> None:
        raise KnowledgeError('document matches secret pattern')

    monkeypatch.setattr('app.ui.ingest_document', reject)

    with _client(settings, engine) as client:
        response = client.post(
            '/ui/sources/upload',
            files={'file': ('book.pdf', pdf, 'application/pdf')},
            headers=AUTH_HEADERS,
        )

    assert response.status_code == 400
    assert 'The document could not be ingested.' in response.text
    # The internal reason never reaches the client, and the rejected bytes are
    # deleted so the stored canonical URI cannot serve them.
    assert 'secret pattern' not in response.text
    assert list((tmp_path / 'rag-raw').iterdir()) == []


def test_ui_upload_ingests_real_pdf_and_lists_openable_source(
    orchestrator_bundle,
    tmp_path,
) -> None:
    settings, _, _, _, engine = orchestrator_bundle
    settings = _raw_settings(settings, tmp_path)
    pdf = _make_real_pdf()

    with _client(settings, engine) as client:
        response = client.post(
            '/ui/sources/upload',
            files={'file': ('methods.pdf', pdf, 'application/pdf')},
            headers=AUTH_HEADERS,
            follow_redirects=False,
        )
        assert response.status_code == 303
        listing = client.get(
            response.headers['location'], headers=AUTH_HEADERS
        )

    assert listing.status_code == 200
    # The success redirect lands on the corpus tab with the new source row and
    # a resolvable PDF viewer iframe over the staged bytes.
    assert 'id="tab-corpus" checked>' in listing.text
    assert 'methods.pdf' in listing.text
    assert '<iframe' in listing.text


def test_ui_upload_disabled_shows_note_and_refuses_post(
    orchestrator_bundle,
) -> None:
    settings, _, _, _, engine = orchestrator_bundle
    settings = settings.model_copy(update={'ui_upload_enabled': False})

    with _client(settings, engine) as client:
        page = client.get('/ui/', headers=AUTH_HEADERS)
        post = client.post(
            '/ui/sources/upload',
            files={'file': ('book.pdf', b'%PDF-1.4\n%%EOF\n')},
            headers=AUTH_HEADERS,
        )

    assert page.status_code == 200
    assert 'Source upload is not enabled on this deployment.' in page.text
    assert 'action="/ui/sources/upload"' not in page.text
    assert post.status_code == 404


def _seed_pending_action(
    engine,
    run_id: str,
    *,
    policy: PolicyClassification = PolicyClassification.HUMAN_APPROVAL,
    honeydew_approved: bool = True,
) -> ActionRecord:
    """A pending human gate whose type matches no engine resume branch, so
    approving or rejecting it is a state-gated no-op past the status update."""
    action = ActionRecord(
        run_id=run_id,
        proposed_by=AgentName.BEAKER,
        type='test_gate',
        policy_classification=policy,
        approval_status=ApprovalStatus.PENDING,
        honeydew_approved=honeydew_approved,
        reason='run the bounded matrix',
        idempotency_key=f'test-gate-{run_id}',
    )
    return engine.store.save_action(action)


def test_ui_renders_launch_and_gate_controls(orchestrator_bundle) -> None:
    settings, _, _, _, engine = orchestrator_bundle
    run = engine.create_run(RunCreateRequest(objective='inspect launch controls'))
    _seed_pending_action(engine, run.run_id)

    with _client(settings, engine) as client:
        page = client.get('/ui/', params={'run': run.run_id}, headers=AUTH_HEADERS)

    assert page.status_code == 200
    assert '<form method="post" action="/ui/runs"' in page.text
    assert 'name="objective"' in page.text
    assert f'/ui/runs/{run.run_id}/control' in page.text
    assert '/ui/actions/' in page.text and '/decide' in page.text
    assert '<script' not in page.text
    assert re.search(r'\sstyle="', page.text) is None


def test_ui_gate_defers_until_honeydew_approves(orchestrator_bundle) -> None:
    settings, _, _, _, engine = orchestrator_bundle
    run = engine.create_run(RunCreateRequest(objective='defer the double gate'))
    _seed_pending_action(
        engine,
        run.run_id,
        policy=PolicyClassification.HONEYDEW_AND_HUMAN_APPROVAL,
        honeydew_approved=False,
    )

    with _client(settings, engine) as client:
        page = client.get('/ui/', params={'run': run.run_id}, headers=AUTH_HEADERS)

    assert page.status_code == 200
    gates = re.findall(
        r'<article class="gate">.*?</article>', page.text, re.DOTALL
    )
    deferred = [gate for gate in gates if 'awaiting Honeydew' in gate]
    assert len(deferred) == 1
    assert 'run the bounded matrix' in deferred[0]
    assert '<form' not in deferred[0]


def test_ui_launch_starts_a_run(orchestrator_bundle) -> None:
    settings, _, _, _, engine = orchestrator_bundle
    before = len(engine.store.list_runs())

    with _client(settings, engine) as client:
        response = client.post(
            '/ui/runs',
            data={'objective': 'launch from the zero-JS form'},
            headers=AUTH_HEADERS,
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers['location'].startswith('/ui/?run=')
    assert len(engine.store.list_runs()) == before + 1


def test_ui_launch_rejects_short_objective(orchestrator_bundle) -> None:
    settings, _, _, _, engine = orchestrator_bundle
    before = len(engine.store.list_runs())

    with _client(settings, engine) as client:
        response = client.post(
            '/ui/runs',
            data={'objective': 'tiny'},
            headers=AUTH_HEADERS,
        )

    assert response.status_code == 400
    assert 'Could not start the run' in response.text
    assert len(engine.store.list_runs()) == before


def test_ui_launch_rejects_orphan_contract_id(orchestrator_bundle) -> None:
    settings, _, _, _, engine = orchestrator_bundle

    with _client(settings, engine) as client:
        response = client.post(
            '/ui/runs',
            data={
                'objective': 'objective with an orphan contract',
                'contract_id': 'c1',
            },
            headers=AUTH_HEADERS,
        )

    assert response.status_code == 400
    assert 'Could not start the run' in response.text


def test_ui_launch_requires_operator(orchestrator_bundle) -> None:
    settings, _, _, _, engine = orchestrator_bundle

    with _client(settings, engine) as client:
        response = client.post('/ui/runs', data={'objective': 'unauthorized'})

    assert response.status_code == 401


def test_ui_run_control_applies_and_rejects_unknown(orchestrator_bundle) -> None:
    settings, _, _, _, engine = orchestrator_bundle
    run = engine.create_run(RunCreateRequest(objective='control the run'))

    with _client(settings, engine) as client:
        paused = client.post(
            f'/ui/runs/{run.run_id}/control',
            data={'action': 'pause'},
            headers=AUTH_HEADERS,
            follow_redirects=False,
        )
        unknown = client.post(
            f'/ui/runs/{run.run_id}/control',
            data={'action': 'explode'},
            headers=AUTH_HEADERS,
        )

    assert paused.status_code == 303
    assert paused.headers['location'] == f'/ui/?run={run.run_id}'
    assert engine.store.get_run(run.run_id).state == RunState.PAUSED
    assert unknown.status_code == 400


def test_ui_decide_gate_approves_pending_action(orchestrator_bundle) -> None:
    settings, _, _, _, engine = orchestrator_bundle
    run = engine.create_run(RunCreateRequest(objective='approve the gate'))
    action = _seed_pending_action(engine, run.run_id)

    with _client(settings, engine) as client:
        response = client.post(
            f'/ui/actions/{action.action_id}/decide',
            data={
                'decision': 'approve',
                'reviewer': 'operator',
                'reason': 'looks good',
            },
            headers=AUTH_HEADERS,
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers['location'] == f'/ui/?run={run.run_id}'
    stored = engine.store.get_action(action.action_id)
    assert stored.approval_status == ApprovalStatus.APPROVED
    assert stored.reviewer == 'operator'


def test_ui_decide_gate_validates_before_acting(orchestrator_bundle) -> None:
    settings, _, _, _, engine = orchestrator_bundle
    run = engine.create_run(RunCreateRequest(objective='validate the gate'))
    action = _seed_pending_action(engine, run.run_id)
    decide = f'/ui/actions/{action.action_id}/decide'

    with _client(settings, engine) as client:
        no_reviewer = client.post(
            decide,
            data={'decision': 'approve', 'reviewer': '', 'reason': 'x'},
            headers=AUTH_HEADERS,
        )
        bad_decision = client.post(
            decide,
            data={'decision': 'maybe', 'reviewer': 'op', 'reason': 'x'},
            headers=AUTH_HEADERS,
        )
        reject_without_reason = client.post(
            decide,
            data={'decision': 'reject', 'reviewer': 'op', 'reason': ''},
            headers=AUTH_HEADERS,
        )

    assert no_reviewer.status_code == 400
    assert bad_decision.status_code == 400
    assert reject_without_reason.status_code == 400
    stored = engine.store.get_action(action.action_id)
    assert stored.approval_status == ApprovalStatus.PENDING


class _RecordingLlm:
    """Duck-typed provider that records each synthesis prompt."""

    def __init__(self, response: str) -> None:
        self._response = response
        self.calls: list[str] = []

    def complete(self, *, system: str, user: str) -> str:
        self.calls.append(user)
        return self._response


def _conversation_id(page_text: str) -> str:
    match = re.search(r'name="c" value="([0-9a-f]+)"', page_text)
    assert match is not None
    return match.group(1)


def test_ui_chat_multi_turn_persists_and_replays(orchestrator_bundle) -> None:
    settings, _, _, _, engine = orchestrator_bundle
    _seed_chat_corpus(engine)
    chat_service = CorpusChatService(engine.store)

    with _client(settings, engine, chat_service=chat_service) as client:
        first = _ask(client, 'first question about resampling')
        conversation_id = _conversation_id(first.text)
        second = _ask(
            client,
            'second question',
            conversation_id=conversation_id,
        )

    assert first.status_code == 200
    assert second.status_code == 200
    # Both turns replay, oldest first, with the newest anchored for scroll.
    assert (
        second.text.index('first question about resampling')
        < second.text.index('second question')
    )
    assert 'id="latest"' in second.text
    stored = engine.store.get_ui_chat_conversation(conversation_id)
    assert stored is not None
    assert [turn.question for turn in stored.turns] == [
        'first question about resampling',
        'second question',
    ]
    # A fresh request without c starts an independent conversation.
    third = _ask(client, 'unrelated question')
    assert _conversation_id(third.text) != conversation_id


def test_ui_chat_feeds_prior_turns_to_synthesis(orchestrator_bundle) -> None:
    settings, _, _, _, engine = orchestrator_bundle
    _seed_chat_corpus(engine)
    llm = _RecordingLlm(
        '{"answer": "Resampling improves stability [1].",'
        ' "citations": [{"evidence_id": "E1"}]}'
    )
    chat_service = CorpusChatService(engine.store, llm=llm)

    with _client(settings, engine, chat_service=chat_service) as client:
        first = _ask(client, 'what is resampling')
        conversation_id = _conversation_id(first.text)
        _ask(
            client,
            'does resampling improve stability',
            conversation_id=conversation_id,
        )

    assert len(llm.calls) == 2
    assert 'Earlier in this conversation' not in llm.calls[0]
    assert 'Earlier in this conversation' in llm.calls[1]
    assert 'what is resampling' in llm.calls[1]
    assert 'Resampling improves stability' in llm.calls[1]


def test_ui_chat_requires_a_question(orchestrator_bundle) -> None:
    settings, _, _, _, engine = orchestrator_bundle
    _seed_chat_corpus(engine)
    chat_service = CorpusChatService(engine.store)

    with _client(settings, engine, chat_service=chat_service) as client:
        response = client.post(
            '/ui/chat',
            data={'q': '   '},
            headers=AUTH_HEADERS,
        )

    assert response.status_code == 400
    assert 'Nothing to ask' in response.text


def _notebook_bytes(cells: list[dict]) -> bytes:
    return json.dumps(
        {
            'nbformat': 4,
            'nbformat_minor': 5,
            'metadata': {},
            'cells': cells,
        }
    ).encode()


def test_ui_artifacts_zip_requires_operator_and_returns_zip(
    orchestrator_bundle,
) -> None:
    settings, _, _, _, engine = orchestrator_bundle
    run = engine.create_run(RunCreateRequest(objective='export the bundle'))
    engine.store.save_artifact(
        _write_report(settings, run.run_id, b'# Findings\n')
    )

    with _client(settings, engine) as client:
        unauthenticated = client.get(
            '/ui/artifacts.zip',
            params={'run': run.run_id},
        )
        response = client.get(
            '/ui/artifacts.zip',
            params={'run': run.run_id},
            headers=AUTH_HEADERS,
        )

    assert unauthenticated.status_code == 401
    assert response.status_code == 200
    assert response.headers['content-type'].startswith('application/zip')
    assert response.headers['content-disposition'].startswith('attachment;')
    assert response.headers['x-content-type-options'] == 'nosniff'
    assert response.headers['referrer-policy'] == 'no-referrer'
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        names = archive.namelist()
    assert 'artifact-manifest.json' in names


def test_ui_artifacts_zip_include_source_toggle(orchestrator_bundle) -> None:
    settings, _, _, _, engine = orchestrator_bundle
    run = engine.create_run(
        RunCreateRequest(objective='toggle the source archive')
    )
    engine.store.save_artifact(
        _write_report(settings, run.run_id, b'# Findings\n')
    )
    engine.store.save_artifact(
        _write_artifact(
            settings,
            run.run_id,
            'source.zip',
            b'task archive bytes',
            artifact_type='source.zip',
        )
    )

    with _client(settings, engine) as client:
        default = client.get(
            '/ui/artifacts.zip',
            params={'run': run.run_id},
            headers=AUTH_HEADERS,
        )
        sourced = client.get(
            '/ui/artifacts.zip',
            params={'run': run.run_id, 'include_source': 'true'},
            headers=AUTH_HEADERS,
        )

    assert default.status_code == 200
    with zipfile.ZipFile(io.BytesIO(default.content)) as archive:
        default_names = archive.namelist()
    assert not any(name.endswith('source.zip') for name in default_names)
    assert sourced.status_code == 200
    with zipfile.ZipFile(io.BytesIO(sourced.content)) as archive:
        sourced_names = archive.namelist()
    assert any(name.endswith('source.zip') for name in sourced_names)


def test_ui_artifacts_zip_unknown_run_and_empty_bundle_are_not_500(
    orchestrator_bundle,
) -> None:
    settings, _, _, _, engine = orchestrator_bundle
    run = engine.create_run(RunCreateRequest(objective='empty bundle'))
    # The fake runtime seeds protocol/proposal artifacts for every run; remove
    # their files so every recorded artifact is unavailable and nothing is
    # deliverable.
    shutil.rmtree(Path(settings.workspace_root) / run.run_id)

    with _client(settings, engine) as client:
        unknown = client.get(
            '/ui/artifacts.zip',
            params={'run': 'missing-run-id'},
            headers=AUTH_HEADERS,
        )
        empty = client.get(
            '/ui/artifacts.zip',
            params={'run': run.run_id},
            headers=AUTH_HEADERS,
        )

    for label, response in (('unknown', unknown), ('empty', empty)):
        assert 400 <= response.status_code < 500, (
            label,
            response.status_code,
        )
        assert 'Artifact bundle unavailable' in response.text
        assert '<script' not in response.text
        assert 'Traceback' not in response.text


def test_ui_run_view_links_the_artifact_bundle_download(
    orchestrator_bundle,
) -> None:
    settings, _, _, _, engine = orchestrator_bundle
    run = engine.create_run(RunCreateRequest(objective='link the bundle'))
    engine.store.save_artifact(
        _write_report(settings, run.run_id, b'# Findings\n')
    )

    with _client(settings, engine) as client:
        page = client.get(
            '/ui/',
            params={'run': run.run_id},
            headers=AUTH_HEADERS,
        )

    assert page.status_code == 200
    assert f'/ui/artifacts.zip?run={run.run_id}' in page.text


def test_ui_notebook_preview_renders_cells_and_escapes(
    orchestrator_bundle,
) -> None:
    settings, _, _, _, engine = orchestrator_bundle
    run = engine.create_run(RunCreateRequest(objective='render a notebook'))
    notebook = _notebook_bytes(
        [
            {
                'cell_type': 'markdown',
                'metadata': {},
                'source': ['# Analysis\n', '<script>alert(1)</script>\n'],
            },
            {
                'cell_type': 'code',
                'execution_count': 1,
                'metadata': {},
                'outputs': [
                    {
                        'output_type': 'stream',
                        'name': 'stdout',
                        'text': 'result <script>alert(2)</script>\n',
                    }
                ],
                'source': 'print(1)\n',
            },
        ]
    )
    engine.store.save_artifact(
        _write_artifact(
            settings,
            run.run_id,
            'reports/analysis.ipynb',
            notebook,
            artifact_type='analysis-notebook',
        )
    )

    with _client(settings, engine) as client:
        response = client.get(
            '/ui/',
            params={'run': run.run_id, 'ref': 'reports/analysis.ipynb'},
            headers=AUTH_HEADERS,
        )

    assert response.status_code == 200
    # Cells render as escaped text, not as one dumped JSON envelope.
    assert 'class="notebook-cell"' in response.text
    assert 'nbformat' not in response.text
    assert '&lt;script&gt;alert(1)&lt;/script&gt;' in response.text
    assert '&lt;script&gt;alert(2)&lt;/script&gt;' in response.text
    assert '<script' not in response.text
    assert '<img' not in response.text
    assert 'data:image' not in response.text


def test_ui_notebook_preview_malformed_json_is_graceful(
    orchestrator_bundle,
) -> None:
    settings, _, _, _, engine = orchestrator_bundle
    run = engine.create_run(RunCreateRequest(objective='break a notebook'))
    engine.store.save_artifact(
        _write_artifact(
            settings,
            run.run_id,
            'reports/broken.ipynb',
            b'{not valid json',
            artifact_type='analysis-notebook',
        )
    )

    with _client(settings, engine) as client:
        response = client.get(
            '/ui/',
            params={'run': run.run_id, 'ref': 'reports/broken.ipynb'},
            headers=AUTH_HEADERS,
        )

    assert response.status_code == 200
    assert 'Notebook unavailable' in response.text
    assert '{not valid json' not in response.text


def test_ui_notebook_preview_deeply_nested_json_is_graceful(
    orchestrator_bundle,
) -> None:
    settings, _, _, _, engine = orchestrator_bundle
    run = engine.create_run(RunCreateRequest(objective='nest a notebook'))
    # Valid JSON, but nested past the interpreter's recursion limit, so
    # ``json.loads`` raises RecursionError rather than JSONDecodeError.
    deeply_nested = ('{"x":' * 20000 + '1' + '}' * 20000).encode()
    engine.store.save_artifact(
        _write_artifact(
            settings,
            run.run_id,
            'reports/deep.ipynb',
            deeply_nested,
            artifact_type='analysis-notebook',
        )
    )

    with _client(settings, engine) as client:
        response = client.get(
            '/ui/',
            params={'run': run.run_id, 'ref': 'reports/deep.ipynb'},
            headers=AUTH_HEADERS,
        )

    assert response.status_code == 200
    assert 'Notebook unavailable' in response.text
    assert 'Traceback' not in response.text


def test_ui_notebook_preview_huge_integer_is_graceful(
    orchestrator_bundle,
) -> None:
    settings, _, _, _, engine = orchestrator_bundle
    run = engine.create_run(RunCreateRequest(objective='parse a huge integer'))
    # Valid JSON, but the metadata seed exceeds CPython's int-string digit
    # limit, so ``json.loads`` raises a plain ValueError rather than the
    # JSONDecodeError the renderer used to catch.
    engine.store.save_artifact(
        _write_artifact(
            settings,
            run.run_id,
            'reports/hugeint.ipynb',
            ('{"cells": [], "metadata": {"seed": ' + '9' * 6000 + '}}').encode(),
            artifact_type='analysis-notebook',
        )
    )
    # ``raise_server_exceptions=False`` observes the 500 as a status code
    # instead of letting the uncaught ValueError fail the test outright.
    client = TestClient(_ui_app(settings, engine), raise_server_exceptions=False)

    with client:
        response = client.get(
            '/ui/',
            params={'run': run.run_id, 'ref': 'reports/hugeint.ipynb'},
            headers=AUTH_HEADERS,
        )

    assert response.status_code == 200
    assert 'Notebook unavailable' in response.text
    assert 'Traceback' not in response.text


def test_ui_notebook_preview_lone_surrogate_is_graceful(
    orchestrator_bundle,
) -> None:
    settings, _, _, _, engine = orchestrator_bundle
    run = engine.create_run(RunCreateRequest(objective='decode a surrogate'))
    # The JSON escape ``\ud800`` parses cleanly into a lone surrogate, which
    # then cannot be UTF-8 encoded into the response. ``errors='replace'`` on
    # the initial file decode cannot help: the surrogate only appears after
    # JSON unescaping.
    surrogate_escape = b'{"cells":[{"cell_type":"code","source":["\\ud800"]}]}'
    engine.store.save_artifact(
        _write_artifact(
            settings,
            run.run_id,
            'reports/surrogate.ipynb',
            surrogate_escape,
            artifact_type='analysis-notebook',
        )
    )
    client = TestClient(_ui_app(settings, engine), raise_server_exceptions=False)

    with client:
        response = client.get(
            '/ui/',
            params={'run': run.run_id, 'ref': 'reports/surrogate.ipynb'},
            headers=AUTH_HEADERS,
        )

    assert response.status_code == 200
    body = response.content.decode('utf-8')
    assert 'Traceback' not in body
    assert 'UnicodeEncodeError' not in body


def test_ui_artifacts_zip_rejects_unsafe_run_id(orchestrator_bundle) -> None:
    settings, _, _, _, engine = orchestrator_bundle
    run = engine.create_run(RunCreateRequest(objective='guard the filename'))
    engine.store.save_artifact(
        _write_report(settings, run.run_id, b'# Findings\n')
    )
    # A run whose id carries a Content-Disposition injection must be refused,
    # not merely absent from the store; seed one so the header path is reached.
    unsafe_run_id = 'abc"; x="'
    engine.store.create_run(
        run.model_copy(update={'run_id': unsafe_run_id}),
        one_active_run=False,
    )
    engine.store.save_artifact(
        _write_artifact(settings, unsafe_run_id, REPORT_REF, b'# Findings\n')
    )

    with _client(settings, engine) as client:
        unsafe = client.get(
            '/ui/artifacts.zip',
            params={'run': unsafe_run_id},
            headers=AUTH_HEADERS,
        )
        valid = client.get(
            '/ui/artifacts.zip',
            params={'run': run.run_id},
            headers=AUTH_HEADERS,
        )

    assert 400 <= unsafe.status_code < 500
    assert 'Artifact bundle unavailable' in unsafe.text
    assert 'Traceback' not in unsafe.text
    assert '<script' not in unsafe.text
    assert 'x="' not in unsafe.text
    assert 'x=' not in unsafe.headers.get('content-disposition', '')
    assert 'filename="abc' not in unsafe.text

    assert valid.status_code == 200
    assert valid.headers['content-type'].startswith('application/zip')
    assert valid.headers['content-disposition'] == (
        f'attachment; filename="glasslab-{run.run_id[:12]}-artifacts.zip"'
    )


def test_ui_artifacts_zip_unreadable_artifact_is_not_500(
    orchestrator_bundle,
    monkeypatch,
) -> None:
    settings, _, _, _, engine = orchestrator_bundle
    run = engine.create_run(RunCreateRequest(objective='unreadable artifact'))

    def _raise_permission_error(**_kwargs):
        raise PermissionError('shared-mount path leaked /var/lib/glasslab')

    monkeypatch.setattr(
        'app.ui.build_run_artifact_bundle',
        _raise_permission_error,
    )

    with _client(settings, engine) as client:
        response = client.get(
            '/ui/artifacts.zip',
            params={'run': run.run_id},
            headers=AUTH_HEADERS,
        )

    assert response.status_code == 409
    assert 'Artifact bundle unavailable' in response.text
    assert 'Traceback' not in response.text
    assert '<script' not in response.text
    assert '/var/lib/glasslab' not in response.text


def test_ui_csp_has_no_img_src(orchestrator_bundle) -> None:
    settings, _, _, _, engine = orchestrator_bundle

    with _client(settings, engine) as client:
        response = client.get('/ui/', headers=AUTH_HEADERS)

    assert response.status_code == 200
    csp = response.headers['content-security-policy']
    assert 'img-src' not in csp
