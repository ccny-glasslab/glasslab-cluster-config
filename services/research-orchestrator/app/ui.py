"""Read-only, server-rendered corpus and reports page for the operator UI.

One operator-gated route (``GET /ui/``) renders a no-JavaScript, three-pane
view over durable orchestrator state: the sources index (runs, corpus
sources, and the selected run's context packets), a digest-verified text
preview of a linkable run artifact, and a server-side citation inspector that
locates a supplied excerpt inside a ``ContextPacket``'s exact supplied text
using :mod:`app.citation_locator` (never the tautological stored
``ranked_sources[].verified`` flag).

The page is escape-first: every interpolated value passes through
:func:`html.escape`, the document body is shown as escaped text inside
``<pre>`` (never rendered markdown or HTML), and ranked-source URIs and
filesystem paths are never emitted. Links are root-relative so the page works
unchanged through the loopback UI proxy. The response carries a strict
Content-Security-Policy whose per-response nonce authorizes only the page's
own ``<style>`` block; no script tag or external resource is ever emitted, so
the page renders with JavaScript disabled.
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
from .storage import RecordNotFound

if TYPE_CHECKING:
    from .config import Settings
    from .engine import ResearchOrchestrator
    from .schemas import ArtifactRecord, ContextPacket

# A browser text pane is not a download surface: the preview is capped well
# below the signed-link ceiling so one large artifact cannot stall the page.
MAXIMUM_UI_DOCUMENT_BYTES = 2 * 1024 * 1024

_CITATION_BADGES: dict[CitationClass, str] = {
    'exact': '✓ exact',
    'fuzzy': '≈ fuzzy',
    'none': '✗ unverified',
}

_PAGE_STYLES = """
:root{color-scheme:light}
body{font-family:system-ui,sans-serif;max-width:1400px;margin:2rem auto;
padding:0 1rem;line-height:1.5}
h1{font-size:1.2rem}
h2{font-size:1rem;border-bottom:1px solid #d0d7de;padding-bottom:.3rem}
h3{font-size:.9rem;margin:.9rem 0 .3rem}
.panes{display:grid;align-items:start;gap:1.5rem;
grid-template-columns:minmax(240px,1fr) minmax(0,2fr) minmax(0,1.5fr)}
@media (max-width:1100px){.panes{grid-template-columns:1fr}}
.pane{min-width:0}
ul{list-style:none;margin:0;padding:0}
li{padding:.25rem 0;border-bottom:1px solid #eaeef2;overflow-wrap:anywhere}
pre{white-space:pre-wrap;background:#f6f8fa;padding:1rem;border-radius:6px;
font-size:.85rem;overflow-wrap:anywhere}
code{font-size:.85em}
table{border-collapse:collapse;width:100%}
td,th{border:1px solid #d0d7de;padding:.3rem .5rem;font-size:.8rem;
text-align:left;overflow-wrap:anywhere}
th{white-space:nowrap}
.nowrap{white-space:nowrap}
.muted{color:#57606a}
.badge{display:inline-block;padding:.1rem .45rem;border-radius:6px;
font-size:.8rem;font-weight:600}
.badge-exact{background:#dafbe1;color:#116329}
.badge-fuzzy{background:#fff8c5;color:#7d4e00}
.badge-none{background:#ffebe9;color:#82071e}
a{color:#0969da}
""".strip()


@dataclass(frozen=True, slots=True)
class UiRequest:
    """One ``GET /ui/`` request: the optional selection plus its CSP nonce."""

    run_id: str | None = None
    ref: str | None = None
    packet_id: str | None = None
    excerpt: str | None = None
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
        f'{_escape(packet.query)}</li>'
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


def _render_document_pane(
    engine: ResearchOrchestrator,
    settings: Settings,
    selection: UiRequest,
) -> str:
    if not selection.run_id:
        return '<p class="muted">Select a run to list its linkable artifacts.</p>'
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
            '<p class="muted">Select an artifact to preview its text.</p>'
            + listing
        )
    return _render_document_body(settings, selection, linkable) + listing


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
            f'<pre>{_escape(match.block_text)}</pre>'
        )
    return (
        '<h3>Citation</h3>'
        f'<p><span class="badge badge-{classification}">'
        f'{_escape(_CITATION_BADGES[classification])}</span></p>'
        f'<p><strong>Excerpt:</strong> <code>{_escape(excerpt)}</code></p>'
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
        f'<p><strong>Query:</strong> {_escape(packet.query)}</p>'
        '<h3>Ranked sources</h3>'
        '<table><tr><th>#</th><th>source_id</th><th>digest</th>'
        f'<th>score</th></tr>{ranked_rows}</table>'
        + _render_citation(packet, selection.excerpt)
    )


def render_ui_page(
    engine: ResearchOrchestrator,
    settings: Settings,
    request: UiRequest,
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
        '<main class="panes">'
        '<section class="pane" id="sources">'
        f'<h2>Sources</h2>{_render_sources_pane(engine, request)}</section>'
        '<section class="pane" id="document">'
        f'<h2>Document</h2>'
        f'{_render_document_pane(engine, settings, request)}</section>'
        '<section class="pane" id="evidence">'
        f'<h2>Evidence inspector</h2>'
        f'{_render_evidence_pane(engine, request)}</section>'
        '</main></body></html>'
    )


def _ui_headers(nonce: str) -> dict[str, str]:
    # default-src 'none' plus a per-response style nonce: the only permitted
    # resource is this page's own style block. No script, font, image, or
    # network origin can load, so a corpus-authored string can never execute.
    return {
        'Content-Security-Policy': (
            "default-src 'none'; style-src 'nonce-" + nonce + "'; "
            "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
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
) -> None:
    """Register the single operator-gated ``GET /ui/`` page route.

    ``require_operator`` is the host application's header-auth dependency (a
    closure over its settings in ``main.create_app``), so it is injected
    rather than imported; the UI module never reads the operator token.
    """

    @app.get('/ui/', response_class=HTMLResponse)
    def corpus_ui(
        run: str | None = Query(default=None),
        ref: str | None = Query(default=None),
        packet: str | None = Query(default=None),
        excerpt: str | None = Query(default=None),
        _: None = Depends(require_operator),
    ) -> HTMLResponse:
        request = UiRequest(
            run_id=run,
            ref=ref,
            packet_id=packet,
            excerpt=excerpt,
            nonce=secrets.token_urlsafe(16),
        )
        return HTMLResponse(
            content=render_ui_page(engine, settings, request),
            headers=_ui_headers(request.nonce),
        )
