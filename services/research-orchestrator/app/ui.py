"""Read-only, server-rendered corpus and reports page for the operator UI.

One operator-gated route (``GET /ui/``) renders a no-JavaScript, three-pane
view over durable orchestrator state: the sources index (runs, corpus
sources, and the selected run's context packets), a digest-verified text
preview of a linkable run artifact, and a server-side citation inspector that
locates a supplied excerpt inside a ``ContextPacket``'s exact supplied text
using :mod:`app.citation_locator` (never the tautological stored
``ranked_sources[].verified`` flag).

When a corpus-chat service is injected, a full-width ``Ask the corpus``
section renders above the panes. It is a same-origin ``GET`` form (the
loopback UI proxy forwards only ``GET``/``HEAD``), so ``?q=…`` re-renders the
page with the answer; every citation links back with
``?q=&source=&page=&excerpt=``, and selecting one embeds the same-origin PDF
viewer iframe for the cited source. The page itself still emits no script and
no external resource.

The page is escape-first: every interpolated value passes through
:func:`html.escape`, the document body is shown as escaped text inside
``<pre>`` (never rendered markdown or HTML), and ranked-source URIs and
filesystem paths are never emitted. Links are root-relative so the page works
unchanged through the loopback UI proxy. The Content-Security-Policy keeps
``default-src 'none'`` and a per-response style nonce, and widens only
``form-action`` to ``'self'`` (the chat form) and adds ``frame-src 'self'``
(the viewer iframe); no remote origin can load.
"""

from __future__ import annotations

# allow: SIZE_OK — this is one escaped HTML/CSS response builder; most lines
# are literal markup and splitting the panes across modules would scatter a
# single response contract without reducing what a reviewer must hold.

from collections.abc import Callable
from dataclasses import dataclass
import html
import secrets
from typing import TYPE_CHECKING
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, Query
from fastapi.responses import HTMLResponse

from .artifact_delivery import ArtifactDeliveryError, VerifiedArtifactReader
from .citation_locator import (
    CitationClass,
    classify_citation,
    match_block,
    parse_context_blocks,
)
from .links import (
    LINKABLE_ARTIFACT_PREFIXES,
    LinkError,
    run_relative_ref,
    validate_ref,
)
from .redaction import redact_free_text
from .storage import RecordNotFound

if TYPE_CHECKING:
    from .config import Settings
    from .corpus_rag.chat import CorpusChatService
    from .engine import ResearchOrchestrator
    from .schemas import ArtifactRecord, ContextPacket
    from .ui_chat import ChatCitation

# A browser text pane is not a download surface: the preview is capped well
# below the signed-link ceiling so one large artifact cannot stall the page.
MAXIMUM_UI_DOCUMENT_BYTES = 2 * 1024 * 1024

_CITATION_BADGES: dict[CitationClass, str] = {
    'exact': '✓ exact',
    'fuzzy': '≈ fuzzy',
    'none': '✗ unverified',
}

_PAGE_STYLES = """
/* Dark token layer: near-black canvas, one indigo accent, semantic status
   colors. Text ramps stay at or above a 4.5:1 contrast ratio on pane
   surfaces; code and badges carry their own tinted surfaces. */
:root{
color-scheme:dark;
--bg:#08090a;
--surface:#0e0f11;
--well:#0a0b0c;
--raised:rgba(255,255,255,.06);
--line:rgba(255,255,255,.08);
--line-faint:rgba(255,255,255,.05);
--text:#f7f8f8;
--text-2:#d0d6e0;
--text-muted:#8a8f98;
--accent:#8b93ff;
--accent-hover:#a3aaff;
--accent-dim:rgba(139,147,255,.42);
--ok-bg:rgba(16,185,129,.14);--ok-fg:#57d9a3;--ok-line:rgba(16,185,129,.35);
--warn-bg:rgba(245,158,11,.14);--warn-fg:#f0b849;--warn-line:rgba(245,158,11,.35);
--bad-bg:rgba(244,63,94,.15);--bad-fg:#fb7185;--bad-line:rgba(244,63,94,.4);
--radius:10px;--radius-sm:7px;
--sans:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,
"Helvetica Neue",Arial,sans-serif;
--mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,
"Liberation Mono",monospace;
}
html{background-color:var(--bg);background-repeat:no-repeat;background-image:
radial-gradient(1100px 520px at 4rem -10rem,rgba(113,112,255,.13),
rgba(113,112,255,0) 68%),
radial-gradient(880px 460px at 100% -8rem,rgba(94,106,210,.07),
rgba(94,106,210,0) 62%)}
body{max-width:1400px;margin:0 auto;padding:2.5rem 1.25rem 4rem;
font-family:var(--sans);font-size:.9375rem;line-height:1.58;
color:var(--text-2);-webkit-font-smoothing:antialiased}
h1,h2,h3{text-wrap:balance}
p,li{text-wrap:pretty}
h1{display:flex;align-items:center;gap:.6rem;margin:0 0 1.6rem;
font-size:1.375rem;font-weight:600;line-height:1.25;letter-spacing:-.02em;
color:var(--text)}
h1::before{content:"";flex:none;inline-size:.5rem;block-size:.5rem;
border-radius:50%;background:var(--accent);
box-shadow:0 0 14px 2px rgba(113,112,255,.55)}
h2{margin:0 0 .95rem;padding-bottom:.6rem;border-bottom:1px solid var(--line);
font-size:.6875rem;font-weight:600;letter-spacing:.1em;text-transform:uppercase;
color:var(--text-muted)}
h3{margin:1.4rem 0 .55rem;font-size:.8125rem;font-weight:600;
letter-spacing:.005em;color:var(--text-2)}
h3:first-of-type{margin-top:.6rem}
.panes{display:grid;align-items:start;gap:1.25rem;
grid-template-columns:minmax(250px,1fr) minmax(0,2fr) minmax(0,1.5fr)}
.pane{min-width:0;padding:1.15rem 1.25rem 1.4rem;border:1px solid var(--line);
border-radius:var(--radius);background:
linear-gradient(180deg,rgba(255,255,255,.025),rgba(255,255,255,0) 6rem),
var(--surface);box-shadow:0 24px 48px -38px rgba(0,0,0,.95),
inset 0 1px 0 rgba(255,255,255,.03)}
p{margin:.7rem 0}
strong{color:var(--text);font-weight:600}
a{color:var(--accent);text-decoration:underline;
text-decoration-color:var(--accent-dim);text-decoration-thickness:1px;
text-underline-offset:2.5px;
transition:color .15s ease,text-decoration-color .15s ease}
a:hover{color:var(--accent-hover);text-decoration-color:currentColor}
a:focus-visible{outline:2px solid var(--accent);outline-offset:2px;
border-radius:3px}
ul{list-style:none;margin:0;padding:0}
li{padding:.45rem 0;border-bottom:1px solid var(--line-faint);
overflow-wrap:anywhere}
li:last-child{border-bottom:0}
.muted{color:var(--text-muted)}
.nowrap{white-space:nowrap}
pre{margin:.65rem 0 0;padding:.85rem 1rem;background:var(--well);
border:1px solid var(--line-faint);border-radius:var(--radius-sm);
box-shadow:inset 0 2px 10px rgba(0,0,0,.35);font-family:var(--mono);
font-size:.8125rem;line-height:1.62;color:var(--text-2);
white-space:pre-wrap;overflow-wrap:anywhere;tab-size:2}
code{font-family:var(--mono);font-size:.85em;color:#e6e8eb;
background:var(--raised);border:1px solid var(--line-faint);
border-radius:5px;padding:.06em .34em;overflow-wrap:anywhere}
pre code{background:none;border:0;padding:0;font-size:inherit;color:inherit}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
th,td{padding:.45rem .6rem;border-bottom:1px solid var(--line-faint);
font-size:.8125rem;text-align:left;vertical-align:top;overflow-wrap:anywhere}
th{padding-top:.1rem;padding-bottom:.45rem;border-bottom-color:var(--line);
font-size:.6875rem;font-weight:600;letter-spacing:.08em;
text-transform:uppercase;color:var(--text-muted);white-space:nowrap}
td:first-child,th:first-child{padding-left:0}
td:last-child,th:last-child{padding-right:0}
tr:last-child td{border-bottom:0}
.badge{display:inline-block;padding:.14rem .55rem;border:1px solid;
border-radius:999px;font-size:.75rem;font-weight:600;line-height:1.4;
letter-spacing:.01em;white-space:nowrap}
.badge-exact{background:var(--ok-bg);border-color:var(--ok-line);
color:var(--ok-fg)}
.badge-fuzzy{background:var(--warn-bg);border-color:var(--warn-line);
color:var(--warn-fg)}
.badge-none{background:var(--bad-bg);border-color:var(--bad-line);
color:var(--bad-fg)}
#ask{margin:0 0 1.25rem}
.ask-form{display:flex;flex-wrap:wrap;align-items:flex-end;gap:.6rem;
margin:0}
.ask-form label{flex:none;padding-bottom:.62rem;font-size:.6875rem;
font-weight:600;letter-spacing:.08em;text-transform:uppercase;
color:var(--text-muted)}
.ask-form input[type=text]{flex:1 1 18rem;min-width:0;
padding:.62rem .8rem;background:var(--well);border:1px solid var(--line);
border-radius:var(--radius-sm);color:var(--text);font:inherit;
font-size:.875rem}
.ask-form input[type=text]:focus-visible{outline:2px solid var(--accent);
outline-offset:2px;border-color:transparent}
.ask-form button{padding:.62rem 1.15rem;background:var(--accent);
border:1px solid transparent;border-radius:var(--radius-sm);color:#0b0c12;
font:inherit;font-size:.8125rem;font-weight:600;cursor:pointer;
transition:background-color .15s ease}
.ask-form button:hover{background:var(--accent-hover)}
.chat-turn{margin:1rem 0 0;padding:.9rem 1.05rem;background:var(--well);
border:1px solid var(--line-faint);border-radius:var(--radius-sm)}
.chat-citations{margin-top:.55rem}
.chat-citations p{margin:.35rem 0 0}
.viewer{margin:.6rem 0 0}
.viewer iframe{display:block;inline-size:100%;
block-size:min(78vh,860px);border:1px solid var(--line);
border-radius:var(--radius-sm);background:var(--well)}
::selection{background:rgba(113,112,255,.35);color:var(--text)}
@media (max-width:1100px){.panes{grid-template-columns:1fr}}
@media (max-width:640px){body{padding:1.6rem .9rem 2.8rem}
h1{font-size:1.2rem}.pane{padding:1rem 1rem 1.15rem}}
@media (prefers-reduced-motion:reduce){a,button{transition:none}}
""".strip()


@dataclass(frozen=True, slots=True)
class UiRequest:
    """One ``GET /ui/`` request: the optional selection plus its CSP nonce.

    ``question`` drives the chat answer; ``source_id``/``page`` select the
    cited source shown in the PDF viewer, with ``excerpt`` supplying the
    exact-span highlight text.
    """

    run_id: str | None = None
    ref: str | None = None
    packet_id: str | None = None
    excerpt: str | None = None
    question: str | None = None
    source_id: str | None = None
    page: int | None = None
    nonce: str = ''


def _escape(value: object) -> str:
    return html.escape(str(value), quote=True)


def _page_url(**parameters: str | None) -> str:
    present = {name: value for name, value in parameters.items() if value}
    if not present:
        return '/ui/'
    return f'/ui/?{urlencode(present)}'


def _link(url: str, label: str) -> str:
    return f'<a href="{_escape(url)}">{_escape(label)}</a>'


def _digest_prefix(value: object, width: int = 12) -> str:
    text = '' if value is None else str(value)
    return f'{text[:width]}…' if len(text) > width else text


def _format_score(value: object) -> str:
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, float)):
        return f'{value:.3f}'
    return '' if value is None else str(value)


def _render_sources_pane(
    engine: ResearchOrchestrator,
    selection: UiRequest,
) -> str:
    runs = engine.store.list_runs()
    run_items = '\n'.join(
        '<li>'
        f'{_link(_page_url(run=run.run_id), run.run_id)} '
        f'<span class="muted">{_escape(run.state)}</span> '
        f'{_escape(run.objective)}'
        '</li>'
        for run in runs
    ) or '<li class="muted">no runs recorded</li>'

    corpus = engine.store.list_knowledge_sources()
    source_rows = '\n'.join(
        '<tr>'
        f'<td>{_escape(source.title or source.source_id)}</td>'
        f'<td class="nowrap"><code>{_escape(source.source_type)}</code>'
        f'<br><span class="muted">{_escape(source.run_scope or "shared")}'
        '</span></td>'
        f'<td class="nowrap">'
        f'<code>{_escape(_digest_prefix(source.digest, 8))}</code></td>'
        '</tr>'
        for source in corpus
    ) or '<tr><td colspan="3" class="muted">no corpus sources</td></tr>'

    packets: list[ContextPacket] = []
    if selection.run_id:
        try:
            packets = engine.store.list_context_packets(selection.run_id)
        except RecordNotFound:
            packets = []
    packet_links = [
        _link(
            _page_url(run=selection.run_id, packet=packet.packet_id),
            packet.packet_id,
        )
        for packet in packets
    ]
    packet_items = '\n'.join(
        '<li>'
        f'{link} <span class="muted">{_escape(packet.agent)} '
        f'turn {packet.turn_number} ({_escape(packet.turn_kind)})</span> '
        f'{_escape(redact_free_text(packet.query))}</li>'
        for link, packet in zip(packet_links, packets, strict=True)
    ) or (
        '<li class="muted">select a run to list its context packets</li>'
        if not selection.run_id
        else '<li class="muted">no context packets for this run</li>'
    )

    return (
        '<h3>Runs</h3>'
        f'<ul>{run_items}</ul>'
        '<h3>Corpus sources</h3>'
        '<table><tr><th>title</th><th>type / scope</th><th>digest</th>'
        f'</tr>{source_rows}</table>'
        '<h3>Context packets</h3>'
        f'<ul>{packet_items}</ul>'
    )


def _linkable_artifacts(
    artifacts: list[ArtifactRecord],
) -> dict[str, ArtifactRecord]:
    # Latest record wins, mirroring signed-link redemption and delivery dedup;
    # only refs under LINKABLE_ARTIFACT_PREFIXES are ever previewable.
    latest: dict[str, ArtifactRecord] = {}
    for artifact in artifacts:
        ref = run_relative_ref(artifact.uri, artifact.run_id)
        if ref is None or not ref.startswith(LINKABLE_ARTIFACT_PREFIXES):
            continue
        latest[ref] = artifact
    return latest


def _render_document_body(
    settings: Settings,
    selection: UiRequest,
    linkable: dict[str, ArtifactRecord],
) -> str:
    ref = selection.ref or ''
    try:
        validate_ref(ref)
    except LinkError:
        return '<p class="muted">Document unavailable: invalid ref.</p>'
    if not ref.startswith(LINKABLE_ARTIFACT_PREFIXES):
        return (
            '<p class="muted">Document unavailable: ref is outside the '
            'linkable artifact directories.</p>'
        )
    artifact = linkable.get(ref)
    if artifact is None:
        return '<p class="muted">Document unavailable: no artifact record.</p>'
    try:
        content = VerifiedArtifactReader(settings.shared_mount_root).read(
            artifact,
            maximum_bytes=MAXIMUM_UI_DOCUMENT_BYTES,
        )
    except ArtifactDeliveryError:
        # The exception text can name the artifact URI or a store path, so the
        # page reports only that the preview is unavailable.
        return (
            '<p class="muted">Document unavailable: digest verification '
            'failed or the file exceeds the preview limit.</p>'
        )
    text = content.decode('utf-8', errors='replace')
    return (
        f'<p><strong>Ref:</strong> <code>{_escape(ref)}</code> · '
        f'<strong>SHA-256:</strong> '
        f'<code>{_escape(_digest_prefix(artifact.sha256))}</code></p>'
        f'<pre>{_escape(text)}</pre>'
    )


def _pdf_viewer_url(
    source_id: str,
    page: int | None,
    excerpt: str | None,
) -> str:
    parameters = {'source': source_id}
    if page is not None:
        parameters['page'] = str(page)
    if excerpt:
        parameters['excerpt'] = excerpt
    return '/ui/pdf/assets/web/highlight.html?' + urlencode(parameters)


def _render_cited_source(selection: UiRequest) -> str:
    if not selection.source_id:
        return ''
    excerpt = (
        redact_free_text(selection.excerpt) if selection.excerpt else None
    )
    url = _pdf_viewer_url(selection.source_id, selection.page, excerpt)
    return (
        '<h3>Cited source</h3>'
        '<div class="viewer">'
        f'<iframe src="{_escape(url)}" title="Cited source PDF" '
        'loading="lazy"></iframe>'
        '</div>'
    )


def _render_document_pane(
    engine: ResearchOrchestrator,
    settings: Settings,
    selection: UiRequest,
) -> str:
    viewer = _render_cited_source(selection)
    if not selection.run_id:
        return (
            viewer
            + '<p class="muted">Select a run to list its linkable artifacts.'
            '</p>'
        )
    linkable = _linkable_artifacts(engine.store.list_artifacts(selection.run_id))
    artifact_items = '\n'.join(
        '<li>'
        f'{_link(_page_url(run=selection.run_id, ref=ref), ref)} '
        f'<span class="muted">{_escape(artifact.type)} · '
        f'{_escape(_digest_prefix(artifact.sha256))}</span>'
        '</li>'
        for ref, artifact in linkable.items()
    ) or '<li class="muted">no linkable artifacts for this run</li>'
    listing = f'<h3>Linkable artifacts</h3><ul>{artifact_items}</ul>'
    if not selection.ref:
        return (
            viewer
            + '<p class="muted">Select an artifact to preview its text.</p>'
            + listing
        )
    return (
        viewer
        + _render_document_body(settings, selection, linkable)
        + listing
    )


def _render_citation(packet: ContextPacket, excerpt: str | None) -> str:
    if excerpt is None or not excerpt.strip():
        return (
            '<h3>Citation</h3>'
            '<p class="muted">Add <code>?excerpt=…</code> to classify a '
            'citation against this packet.</p>'
        )
    blocks = parse_context_blocks(packet.exact_text_supplied)
    match = match_block(blocks, excerpt, source_index=None)
    if match is None:
        classification: CitationClass = 'none'
        detail = (
            '<p class="muted">The excerpt did not locate a supplied context '
            'block.</p>'
        )
    else:
        classification = classify_citation(excerpt, match.block_text)
        source = (
            packet.ranked_sources[match.block_index]
            if match.block_index < len(packet.ranked_sources)
            else {}
        )
        detail = (
            f'<p><strong>Ordinal:</strong> {match.block_index + 1} · '
            f'<strong>source_id:</strong> '
            f'<code>{_escape(source.get("source_id", ""))}</code> · '
            f'<strong>digest:</strong> '
            f'<code>{_escape(_digest_prefix(source.get("digest")))}</code> · '
            f'<strong>score:</strong> '
            f'{_escape(_format_score(source.get("score")))}</p>'
            f'<pre>{_escape(redact_free_text(match.block_text))}</pre>'
        )
    return (
        '<h3>Citation</h3>'
        f'<p><span class="badge badge-{classification}">'
        f'{_escape(_CITATION_BADGES[classification])}</span></p>'
        f'<p><strong>Excerpt:</strong> '
        f'<code>{_escape(redact_free_text(excerpt))}</code></p>'
        f'{detail}'
    )


def _render_evidence_pane(
    engine: ResearchOrchestrator,
    selection: UiRequest,
) -> str:
    if not selection.packet_id:
        return (
            '<p class="muted">Select a context packet to inspect a citation.'
            '</p>'
        )
    try:
        packet = engine.store.get_context_packet(selection.packet_id)
    except RecordNotFound:
        return '<p class="muted">Packet unavailable.</p>'
    ranked_rows = '\n'.join(
        '<tr>'
        f'<td class="nowrap">{index}</td>'
        f'<td><code>{_escape(source.get("source_id", ""))}</code></td>'
        f'<td class="nowrap">'
        f'<code>{_escape(_digest_prefix(source.get("digest"), 8))}</code></td>'
        f'<td class="nowrap">'
        f'{_escape(_format_score(source.get("score")))}</td>'
        '</tr>'
        for index, source in enumerate(packet.ranked_sources, start=1)
    ) or '<tr><td colspan="4" class="muted">no ranked sources</td></tr>'
    return (
        f'<p><strong>Packet:</strong> '
        f'<code>{_escape(packet.packet_id)}</code> · '
        f'{_escape(packet.agent)} · turn {packet.turn_number} '
        f'({_escape(packet.turn_kind)})</p>'
        f'<p><strong>Query:</strong> {_escape(redact_free_text(packet.query))}'
        '</p>'
        '<h3>Ranked sources</h3>'
        '<table><tr><th>#</th><th>source_id</th><th>digest</th>'
        f'<th>score</th></tr>{ranked_rows}</table>'
        + _render_citation(packet, selection.excerpt)
    )


def _render_chat_citations(
    question: str,
    citations: list[ChatCitation],
) -> str:
    if not citations:
        return ''
    items = []
    for citation in citations:
        excerpt = redact_free_text(citation.excerpt)
        href = _page_url(
            q=question,
            source=citation.source_id,
            # citation.page is the 0-based chunk page_start; the viewer URL and
            # the boxes route both use the 1-based human page number.
            page=str(citation.page + 1) if citation.page is not None else None,
            excerpt=excerpt,
        )
        items.append(
            '<li>'
            f'{_link(href, citation.title)} '
            f'<span class="badge badge-{citation.verdict}">'
            f'{_escape(_CITATION_BADGES[citation.verdict])}</span>'
            f'<p><code>{_escape(excerpt)}</code></p>'
            '</li>'
        )
    return '<ul class="chat-citations">' + '\n'.join(items) + '</ul>'


def _render_ask_section(
    chat_service: CorpusChatService | None,
    selection: UiRequest,
) -> str:
    if chat_service is None:
        return ''
    question = selection.question or ''
    display_question = redact_free_text(question)
    form = (
        '<form method="get" action="/ui/" class="ask-form">'
        '<label for="ask-question">Question</label>'
        f'<input id="ask-question" type="text" name="q" '
        f'value="{_escape(display_question)}" '
        'placeholder="Ask about the corpus" autocomplete="off">'
        '<button type="submit">Ask</button>'
        '</form>'
    )
    if not question.strip():
        body = form
    else:
        answer = chat_service.answer(question)
        body = (
            form
            + '<div class="chat-turn">'
            f'<p><strong>Question:</strong> {_escape(display_question)}</p>'
            f'<p><strong>Answer:</strong> '
            f'{_escape(redact_free_text(answer.answer))}</p>'
            f'{_render_chat_citations(display_question, answer.citations)}'
            '</div>'
        )
    return (
        '<section class="pane" id="ask">'
        '<h2>Ask the corpus</h2>'
        f'{body}'
        '</section>'
    )


def render_ui_page(
    engine: ResearchOrchestrator,
    settings: Settings,
    request: UiRequest,
    chat_service: CorpusChatService | None = None,
) -> str:
    """Render the whole read-only page as one escaped HTML string."""
    return (
        '<!doctype html>'
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>Glasslab corpus</title>'
        f'<style nonce="{_escape(request.nonce)}">{_PAGE_STYLES}</style>'
        '</head><body>'
        '<h1>Glasslab corpus and reports</h1>'
        '<main>'
        f'{_render_ask_section(chat_service, request)}'
        '<div class="panes">'
        '<section class="pane" id="sources">'
        f'<h2>Sources</h2>{_render_sources_pane(engine, request)}</section>'
        '<section class="pane" id="document">'
        '<h2>Document</h2>'
        f'{_render_document_pane(engine, settings, request)}</section>'
        '<section class="pane" id="evidence">'
        '<h2>Evidence inspector</h2>'
        f'{_render_evidence_pane(engine, request)}</section>'
        '</div>'
        '</main></body></html>'
    )


def _ui_headers(nonce: str) -> dict[str, str]:
    # default-src 'none' plus a per-response style nonce: the only permitted
    # subresources are this page's own style block, its same-origin GET chat
    # form (form-action 'self'), and the same-origin cited-source PDF viewer
    # iframe (frame-src 'self'). No script, font, image, or remote origin can
    # load, so a corpus-authored string can never execute.
    return {
        'Content-Security-Policy': (
            "default-src 'none'; style-src 'nonce-" + nonce + "'; "
            "base-uri 'none'; form-action 'self'; frame-src 'self'; "
            "frame-ancestors 'none'"
        ),
        'X-Content-Type-Options': 'nosniff',
        'Referrer-Policy': 'no-referrer',
    }


def register_ui_routes(
    app: FastAPI,
    *,
    engine: ResearchOrchestrator,
    settings: Settings,
    require_operator: Callable[..., None],
    chat_service: CorpusChatService | None = None,
) -> None:
    """Register the single operator-gated ``GET /ui/`` page route.

    ``require_operator`` is the host application's header-auth dependency (a
    closure over its settings in ``main.create_app``), so it is injected
    rather than imported; the UI module never reads the operator token.
    ``chat_service`` is likewise injected by the host -- a
    :class:`~app.corpus_rag.chat.CorpusChatService`, or ``None`` when the chat
    is disabled -- and this module only calls its ``answer`` method.
    """

    @app.get('/ui/', response_class=HTMLResponse)
    def corpus_ui(
        run: str | None = Query(default=None),
        ref: str | None = Query(default=None),
        packet: str | None = Query(default=None),
        excerpt: str | None = Query(default=None),
        q: str | None = Query(default=None),
        source: str | None = Query(default=None),
        page: int | None = Query(default=None),
        _: None = Depends(require_operator),
    ) -> HTMLResponse:
        request = UiRequest(
            run_id=run,
            ref=ref,
            packet_id=packet,
            excerpt=excerpt,
            question=q,
            source_id=source,
            page=page,
            nonce=secrets.token_urlsafe(16),
        )
        return HTMLResponse(
            content=render_ui_page(engine, settings, request, chat_service),
            headers=_ui_headers(request.nonce),
        )
