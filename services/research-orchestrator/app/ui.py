"""Read-only, server-rendered corpus and reports notebook for the operator UI.

One operator-gated route (``GET /ui/``) renders a no-JavaScript, three-column
notebook over durable orchestrator state:

* the **Sources** column is the navigational index: a CSS-only tab strip
  (visually hidden radio inputs, ``<label>`` tabs, and ``:checked`` sibling
  selectors) over the run list, the corpus source table, and the selected
  run's context packets, with the evidence inspector -- the packet's ranked
  sources plus the locator-classified citation -- folded into its Context
  packets tab;
* the **Ask the corpus** column holds the same-origin ``GET`` chat form and
  the answer, whose ``[n]`` citation positions render as inline superscript
  markers with a CSS-only hover/focus preview card (no end-of-answer
  reference list, no script);
* the **Viewer** column shows the selected content: the cited source's
  same-origin PDF viewer iframe, or -- when no PDF is servable -- the
  source's stored extracted text with the cited excerpt marked, or the
  selected run's artifact tree (folders are native
  ``<details>``/``<summary>``) with the digest-verified text preview of the
  selected file.

The page fills the viewport: the title is a static header and each column
scrolls internally, so the document body never grows tall with content. Below
1100px the grid stacks into one column (chat first) and every section stays
usable.

When a corpus-chat service is injected, the center column answers ``?q=…``
from a same-origin ``GET`` form (the loopback UI proxy forwards only
``GET``/``HEAD``); every valid ``[n]`` ordinal in the answer becomes an inline
``<sup>`` citation marker whose anchor links back with
``?q=&source=&page=&excerpt=``, and selecting one embeds the same-origin PDF
viewer iframe for the cited source. Hovering or keyboard-focusing a marker
reveals its preview card (title, verdict badge, "View source") through CSS
only. The page itself still emits no script and no external resource.

The page is escape-first: every interpolated value passes through
:func:`html.escape`, the document body is shown as escaped text inside
``<pre>`` (never rendered markdown or HTML), and ranked-source URIs and
filesystem paths are never emitted. Links are root-relative so the page works
unchanged through the loopback UI proxy. The Content-Security-Policy keeps
``default-src 'none'`` and a per-response style nonce, and widens only
``form-action`` to ``'self'`` (the chat form) and adds ``frame-src 'self'``
(the viewer iframe); no remote origin can load. There is deliberately no
``script-src`` at all, so the tabs, the file tree, and the citation hover
cards are pure HTML and CSS.
"""

from __future__ import annotations

# allow: SIZE_OK — this is one escaped HTML/CSS response builder; almost all
# of its lines are literal markup and CSS token tables, and splitting the
# columns across modules would scatter a single response contract (the nonce,
# the escaping, the root-relative URL scheme) without reducing what a
# reviewer must hold.

from collections.abc import Callable
from dataclasses import dataclass, field
import html
import re
import secrets
from typing import TYPE_CHECKING, Any
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
from .ui_pdf import document_is_resolvable

if TYPE_CHECKING:
    from .config import Settings
    from .corpus_rag.chat import CorpusChatService
    from .engine import ResearchOrchestrator
    from .schemas import ArtifactRecord, ContextPacket
    from .ui_chat import ChatAnswer, ChatCitation

# A browser text pane is not a download surface: the preview is capped well
# below the signed-link ceiling so one large artifact cannot stall the page.
MAXIMUM_UI_DOCUMENT_BYTES = 2 * 1024 * 1024

_CITATION_BADGES: dict[CitationClass, str] = {
    'exact': '✓ exact',
    'fuzzy': '≈ fuzzy',
    'none': '✗ unverified',
}

# An inline citation ordinal in the extractive answer: ``[n]`` selects
# ``citations[n-1]``. The digit run is bounded so a hostile corpus string can
# never make ``int()`` parse a pathologically long number, and a non-matching
# or out-of-range ``[n]`` stays escaped literal text.
_CITATION_ORDINAL_RE = re.compile(r'\[(\d{1,3})\]')

# The Sources tab strip: (radio id, label text, panel id). The panel id is
# derived from the radio id suffix so the CSS sibling selectors, the labels,
# and the tests all agree on one naming scheme.
_SOURCES_TABS = (
    ('tab-runs', 'Runs', 'panel-runs'),
    ('tab-corpus', 'Corpus sources', 'panel-corpus'),
    ('tab-packets', 'Context packets', 'panel-packets'),
)

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
html{height:100%;background-color:var(--bg);background-repeat:no-repeat;
background-image:
radial-gradient(1100px 520px at 4rem -10rem,rgba(113,112,255,.13),
rgba(113,112,255,0) 68%),
radial-gradient(880px 460px at 100% -8rem,rgba(94,106,210,.07),
rgba(94,106,210,0) 62%)}
/* Fixed-viewport notebook: the masthead is static and each of the three
   columns scrolls its own body, so the document never grows with content. */
body{box-sizing:border-box;block-size:100vh;block-size:100dvh;margin:0;
padding:1.35rem 1.25rem 1.25rem;display:flex;flex-direction:column;overflow:hidden;
font-family:var(--sans);font-size:.9375rem;line-height:1.58;
color:var(--text-2);-webkit-font-smoothing:antialiased}
.masthead{flex:none}
h1,h2,h3{text-wrap:balance}
p,li{text-wrap:pretty}
h1{display:flex;align-items:center;gap:.6rem;margin:0 0 1.1rem;
font-size:1.375rem;font-weight:600;line-height:1.25;letter-spacing:-.02em;
color:var(--text)}
h1::before{content:"";flex:none;inline-size:.5rem;block-size:.5rem;
border-radius:50%;background:var(--accent);
box-shadow:0 0 14px 2px rgba(113,112,255,.55)}
main{flex:1 1 auto;min-block-size:0;display:grid;gap:1rem;
grid-template-columns:minmax(258px,.82fr) minmax(0,1.06fr) minmax(0,1.32fr)}
.pane{min-inline-size:0;min-block-size:0;display:flex;flex-direction:column;
overflow:hidden;padding:1.05rem 1.15rem 1.2rem;border:1px solid var(--line);
border-radius:var(--radius);background:
linear-gradient(180deg,rgba(255,255,255,.025),rgba(255,255,255,0) 6rem),
var(--surface);box-shadow:0 24px 48px -38px rgba(0,0,0,.95),
inset 0 1px 0 rgba(255,255,255,.03)}
.column-head{flex:none}
h2{margin:0 0 .8rem;padding-bottom:.55rem;border-bottom:1px solid var(--line);
font-size:.6875rem;font-weight:600;letter-spacing:.1em;text-transform:uppercase;
color:var(--text-muted)}
h3{margin:1.2rem 0 .5rem;font-size:.8125rem;font-weight:600;
letter-spacing:.005em;color:var(--text-2)}
h3:first-of-type{margin-top:.4rem}
.column-body{flex:1 1 auto;min-block-size:0;overflow:auto;
overscroll-behavior:contain;scrollbar-gutter:stable}
.column-body.is-fill{display:flex;flex-direction:column;overflow:hidden}
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
/* Zero-script tabs: the radios are visually hidden but stay focusable (Tab
   reaches the checked one, arrow keys move the selection), the labels are
   the visible tabs, and :checked sibling selectors swap panels and light
   the active label. No script and no inline style is required. */
.tabs{position:relative;flex:1 1 auto;min-block-size:0;display:flex;
flex-direction:column}
.tab-input{position:absolute;inline-size:1px;block-size:1px;margin:0;
opacity:0;clip-path:inset(50%);pointer-events:none}
.tab-strip{flex:none;display:flex;gap:.2rem;margin:0 0 .55rem;
border-bottom:1px solid var(--line);overflow-x:auto}
.tab{flex:none;margin-bottom:-1px;padding:.4rem .58rem;
border:1px solid transparent;border-bottom:0;
border-radius:var(--radius-sm) var(--radius-sm) 0 0;font-size:.75rem;
font-weight:600;color:var(--text-muted);cursor:pointer;
white-space:nowrap;user-select:none}
.tab:hover{color:var(--text-2)}
.tab-panels{flex:1 1 auto;min-block-size:0;overflow:auto;
overscroll-behavior:contain}
.tab-panel{display:none}
#tab-runs:checked ~ .tab-panels > #panel-runs,
#tab-corpus:checked ~ .tab-panels > #panel-corpus,
#tab-packets:checked ~ .tab-panels > #panel-packets{display:block}
#tab-runs:checked ~ .tab-strip > label[for="tab-runs"],
#tab-corpus:checked ~ .tab-strip > label[for="tab-corpus"],
#tab-packets:checked ~ .tab-strip > label[for="tab-packets"]{color:var(--text);
background:var(--raised);border-color:var(--line);
border-bottom-color:transparent}
#tab-runs:focus-visible ~ .tab-strip > label[for="tab-runs"],
#tab-corpus:focus-visible ~ .tab-strip > label[for="tab-corpus"],
#tab-packets:focus-visible ~ .tab-strip > label[for="tab-packets"]{outline:2px solid var(--accent);outline-offset:2px}
li.is-current,tr.is-current td{background:rgba(139,147,255,.09)}
a[aria-current="page"]{color:var(--text);text-decoration-color:currentColor}
/* Recursive artifact tree: folders are native <details> disclosures and
   files are links (only under the linkable prefixes) or muted labels. */
ul.tree{margin:0;padding:0}
ul.tree li{padding:.16rem 0;border-bottom:0}
ul.tree ul.tree{margin-inline-start:.7rem;padding-inline-start:.6rem;
border-inline-start:1px solid var(--line-faint)}
details.tree-folder>summary{padding:.12rem 0;font-size:.8125rem;
font-weight:600;color:var(--text-2);cursor:pointer}
details.tree-folder>summary:hover{color:var(--text)}
.tree-meta{font-size:.75rem}
.ask-form{display:flex;flex-wrap:wrap;align-items:flex-end;gap:.6rem;
margin:0}
.ask-form label{flex:none;padding-bottom:.62rem;font-size:.6875rem;
font-weight:600;letter-spacing:.08em;text-transform:uppercase;
color:var(--text-muted)}
.ask-form input[type=text]{flex:1 1 12rem;min-width:0;
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
/* Inline citations: a superscript ordinal marker whose preview card is a
   CSS-only hover/focus popover (no script, no inline style). The card opens
   downward, inside the chat column's scroll area, so it is not clipped by
   the column overflow at the common top-of-answer hover position. */
.cite{position:relative;display:inline-block;margin:0 .16rem;
font-weight:600;color:var(--accent);text-decoration:none;cursor:pointer}
.cite sup{font-size:.7em;line-height:0}
.cite:hover{color:var(--accent-hover)}
.cite:focus-visible{outline:2px solid var(--accent);outline-offset:2px;
border-radius:3px}
.cite-card{position:absolute;top:calc(100% + .5rem);left:0;z-index:4;
display:block;inline-size:max-content;max-inline-size:min(17rem,80vw);
padding:.6rem .72rem;background:var(--surface);border:1px solid var(--line);
border-radius:var(--radius-sm);box-shadow:0 18px 36px -20px rgba(0,0,0,.95);
font-size:.8125rem;font-weight:400;line-height:1.45;text-align:left;
white-space:normal;visibility:hidden;opacity:0;pointer-events:none;
transition:opacity .12s ease}
.cite:hover .cite-card,.cite:focus-within .cite-card{visibility:visible;
opacity:1}
.cite-title{display:block;color:var(--accent);font-weight:600;
text-decoration:underline;text-decoration-color:var(--accent-dim);
text-underline-offset:2.5px}
.cite-badge{display:inline-block;margin-top:.4rem;padding:.1rem .5rem;
border:1px solid;border-radius:999px;font-size:.6875rem;font-weight:600;
line-height:1.4;white-space:nowrap}
.cite-cta{display:block;margin-top:.4rem;color:var(--text-muted);
font-size:.75rem}
.cite-cta::after{content:" →"}
.pdf-viewer{flex:1 1 auto;min-block-size:0;display:flex;
flex-direction:column;margin:.5rem 0 0}
.pdf-viewer iframe{display:block;flex:1 1 auto;min-block-size:0;
inline-size:100%;border:1px solid var(--line);border-radius:var(--radius-sm);
background:var(--well)}
/* Extracted-text reader: the fallback when a cited source has no servable
   PDF. It mirrors the PDF viewer geometry -- the wrapper fills the viewer
   column's body and scrolls internally -- and its <mark> spans come from a
   server-side excerpt match, never from script or inline style. */
.source-text{flex:1 1 auto;min-block-size:0;overflow:auto;
overscroll-behavior:contain;scrollbar-gutter:stable;margin:.5rem 0 0}
.source-title{margin:.55rem 0 0;color:var(--text);font-weight:600}
.excerpt-callout{margin:.75rem 0;padding:.7rem .85rem;background:var(--well);
border:1px solid var(--accent-dim);border-radius:var(--radius-sm)}
.excerpt-callout strong{display:block;font-size:.6875rem;font-weight:600;
letter-spacing:.08em;text-transform:uppercase;color:var(--accent)}
.excerpt-text{margin:.4rem 0 0;font-size:.8125rem;line-height:1.62;
white-space:pre-wrap;overflow-wrap:anywhere}
.source-chunk{margin:.85rem 0;padding:.75rem .9rem;background:var(--well);
border:1px solid var(--line-faint);border-radius:var(--radius-sm);
box-shadow:inset 0 2px 10px rgba(0,0,0,.25)}
.chunk-text{margin:0;font-size:.8125rem;line-height:1.62;
white-space:pre-wrap;overflow-wrap:anywhere;color:var(--text-2)}
.chunk-meta{margin:.5rem 0 0;font-size:.75rem}
mark{background:rgba(139,147,255,.3);color:var(--text);border-radius:3px;
padding:0 .08em}
::selection{background:rgba(113,112,255,.35);color:var(--text)}
/* Below 1100px the notebook stacks into one column (chat first) and every
   section returns to document flow instead of internal scrolling. */
@media (max-width:1100px){
body{block-size:auto;min-block-size:100vh;overflow:visible;
padding:1.6rem .9rem 2.8rem}
main{display:flex;flex-direction:column}
#ask{order:1}
#sources{order:2}
#viewer{order:3}
.pane{overflow:visible}
.column-body,.tab-panels{overflow:visible}
.column-body.is-fill{display:block;overflow:visible}
.pdf-viewer iframe{block-size:70vh;flex:none}
}
@media (max-width:640px){body{padding:1.2rem .8rem 2.2rem}
h1{font-size:1.2rem}.pane{padding:.95rem .95rem 1.05rem}}
@media (prefers-reduced-motion:reduce){a,button,.cite-card{transition:none}}
""".strip()


@dataclass(frozen=True, slots=True)
class UiRequest:
    """One ``GET /ui/`` request: the optional selection plus its CSP nonce.

    ``question`` drives the chat answer; ``source_id``/``page`` select the
    cited source shown in the PDF viewer, with ``excerpt`` supplying the
    exact-span highlight text. ``run_id``/``ref`` select the run whose file
    tree is shown and the file previewed in the viewer; ``packet_id``
    selects the packet inspected in the Sources column.
    """

    run_id: str | None = None
    ref: str | None = None
    packet_id: str | None = None
    excerpt: str | None = None
    question: str | None = None
    source_id: str | None = None
    page: int | None = None
    nonce: str = ''


@dataclass(slots=True)
class _TreeFolder:
    """One directory level of a run's artifact tree while it is rendered."""

    folders: dict[str, _TreeFolder] = field(default_factory=dict)
    files: list[str] = field(default_factory=list)


def _escape(value: object) -> str:
    return html.escape(str(value), quote=True)


def _page_url(**parameters: str | None) -> str:
    present = {name: value for name, value in parameters.items() if value}
    if not present:
        return '/ui/'
    return f'/ui/?{urlencode(present)}'


def _link(url: str, label: str, *, current: bool = False) -> str:
    marker = ' aria-current="page"' if current else ''
    return f'<a href="{_escape(url)}"{marker}>{_escape(label)}</a>'


def _digest_prefix(value: object, width: int = 12) -> str:
    text = '' if value is None else str(value)
    return f'{text[:width]}…' if len(text) > width else text


def _format_score(value: object) -> str:
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, float)):
        return f'{value:.3f}'
    return '' if value is None else str(value)


def _active_sources_tab(selection: UiRequest) -> str:
    """Return the radio id the server marks checked on this render.

    The radio group is the whole tab mechanism, so the initial selection must
    come from the request: an inspected packet lands on the packets tab, a
    cited/selected source on the corpus tab, and everything else (including a
    selected run and a previewed file) on the runs tab.
    """
    if selection.packet_id:
        return 'tab-packets'
    if selection.source_id:
        return 'tab-corpus'
    return 'tab-runs'


def _render_tab_inputs(selection: UiRequest) -> str:
    active = _active_sources_tab(selection)
    inputs = []
    for tab_id, _, _ in _SOURCES_TABS:
        checked = ' checked' if tab_id == active else ''
        inputs.append(
            '<input class="tab-input" type="radio" name="sources-tab" '
            f'id="{tab_id}"{checked}>'
        )
    return '\n'.join(inputs)


def _render_sources_panel(
    engine: ResearchOrchestrator,
    selection: UiRequest,
) -> str:
    runs = engine.store.list_runs()
    run_items = '\n'.join(
        '<li' + (' class="is-current"' if run.run_id == selection.run_id else '')
        + '>'
        + _link(
            _page_url(run=run.run_id),
            run.run_id,
            current=run.run_id == selection.run_id,
        )
        + f' <span class="muted">{_escape(run.state)}</span> '
        + _escape(run.objective)
        + '</li>'
        for run in runs
    ) or '<li class="muted">no runs recorded</li>'

    corpus = engine.store.list_knowledge_sources()
    source_rows = '\n'.join(
        '<tr'
        + (
            ' class="is-current"'
            if source.source_id == selection.source_id
            else ''
        )
        + '>'
        + '<td>'
        + _link(
            _page_url(source=source.source_id),
            source.title or source.source_id,
            current=source.source_id == selection.source_id,
        )
        + '</td>'
        + f'<td class="nowrap"><code>{_escape(source.source_type)}</code>'
        + f'<br><span class="muted">{_escape(source.run_scope or "shared")}'
        + '</span></td>'
        + '<td class="nowrap">'
        + f'<code>{_escape(_digest_prefix(source.digest, 8))}</code></td>'
        + '</tr>'
        for source in corpus
    ) or '<tr><td colspan="3" class="muted">no corpus sources</td></tr>'

    packets: list[ContextPacket] = []
    if selection.run_id:
        try:
            packets = engine.store.list_context_packets(selection.run_id)
        except RecordNotFound:
            packets = []
    packet_items = '\n'.join(
        '<li'
        + (
            ' class="is-current"'
            if packet.packet_id == selection.packet_id
            else ''
        )
        + '>'
        + _link(
            _page_url(run=selection.run_id, packet=packet.packet_id),
            packet.packet_id,
            current=packet.packet_id == selection.packet_id,
        )
        + f' <span class="muted">{_escape(packet.agent)} '
        + f'turn {packet.turn_number} ({_escape(packet.turn_kind)})</span> '
        + _escape(redact_free_text(packet.query))
        + '</li>'
        for packet in packets
    ) or (
        '<li class="muted">select a run to list its context packets</li>'
        if not selection.run_id
        else '<li class="muted">no context packets for this run</li>'
    )

    panels = (
        f'<section class="tab-panel" id="panel-runs">{run_items}</section>',
        '<section class="tab-panel" id="panel-corpus">'
        '<table><tr><th>title</th><th>type / scope</th><th>digest</th>'
        f'</tr>{source_rows}</table></section>',
        '<section class="tab-panel" id="panel-packets">'
        f'{packet_items}{_render_evidence_inspector(engine, selection)}'
        '</section>',
    )
    tab_strip = '\n'.join(
        f'<label class="tab" for="{tab_id}">{_escape(label)}</label>'
        for tab_id, label, _ in _SOURCES_TABS
    )
    return (
        '<aside class="pane" id="sources" aria-label="Sources">'
        '<div class="column-head"><h2>Sources</h2></div>'
        '<div class="tabs">'
        f'{_render_tab_inputs(selection)}'
        f'<div class="tab-strip">{tab_strip}</div>'
        f'<div class="tab-panels">{"".join(panels)}</div>'
        '</div>'
        '</aside>'
    )


def _render_evidence_inspector(
    engine: ResearchOrchestrator,
    selection: UiRequest,
) -> str:
    heading = '<h3>Evidence inspector</h3>'
    if not selection.packet_id:
        return (
            heading
            + '<p class="muted">Select a context packet to inspect its '
            'ranked sources and citation.</p>'
        )
    try:
        packet = engine.store.get_context_packet(selection.packet_id)
    except RecordNotFound:
        return heading + '<p class="muted">Packet unavailable.</p>'
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
        heading
        + f'<p><strong>Packet:</strong> '
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


def _artifact_refs(
    artifacts: list[ArtifactRecord],
) -> dict[str, ArtifactRecord]:
    # Latest record wins, mirroring signed-link redemption and delivery dedup.
    # The tree shows every run-relative artifact ref; only refs under
    # LINKABLE_ARTIFACT_PREFIXES become preview links.
    latest: dict[str, ArtifactRecord] = {}
    for artifact in artifacts:
        ref = run_relative_ref(artifact.uri, artifact.run_id)
        if ref is None:
            continue
        try:
            validate_ref(ref)
        except LinkError:
            continue
        latest[ref] = artifact
    return latest


def _render_tree_children(
    folder: _TreeFolder,
    refs: dict[str, ArtifactRecord],
    selection: UiRequest,
) -> str:
    items = []
    for name in sorted(folder.folders):
        items.append(
            '<li>'
            '<details class="tree-folder" open>'
            f'<summary>{_escape(name)}/</summary>'
            '<ul class="tree">'
            f'{_render_tree_children(folder.folders[name], refs, selection)}'
            '</ul>'
            '</details>'
            '</li>'
        )
    for ref in sorted(folder.files):
        artifact = refs[ref]
        name = ref.rsplit('/', 1)[-1]
        current = ref == selection.ref
        if ref.startswith(LINKABLE_ARTIFACT_PREFIXES):
            target = _link(
                _page_url(run=selection.run_id, ref=ref),
                name,
                current=current,
            )
            meta = f'{artifact.type} · {_digest_prefix(artifact.sha256, 8)}'
        else:
            target = f'<span class="muted">{_escape(name)}</span>'
            meta = 'text preview unavailable'
        item_class = ' class="is-current"' if current else ''
        items.append(
            f'<li{item_class}>{target} '
            f'<span class="tree-meta muted">{_escape(meta)}</span></li>'
        )
    return '\n'.join(items)


def _render_artifact_tree(
    refs: dict[str, ArtifactRecord],
    selection: UiRequest,
) -> str:
    if not refs:
        return '<p class="muted">no artifacts recorded for this run</p>'
    root = _TreeFolder()
    for ref in sorted(refs):
        node = root
        for part in ref.split('/')[:-1]:
            node = node.folders.setdefault(part, _TreeFolder())
        node.files.append(ref)
    return f'<ul class="tree">{_render_tree_children(root, refs, selection)}</ul>'


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


def _highlight_escaped(text: str, needle: str | None) -> str:
    """Escape ``text`` and wrap case-insensitive ``needle`` matches in mark.

    The match runs over the raw text and each matched span is escaped
    independently, so ``html.escape`` (a character-wise transform) still
    escapes every byte; a hostile excerpt can never smuggle markup through
    the highlight wrapper, and only matched plain-text spans gain a ``<mark>``.
    """
    text = str(text)
    if not needle:
        return _escape(text)
    rendered: list[str] = []
    cursor = 0
    for match in re.finditer(re.escape(needle), text, re.IGNORECASE):
        rendered.append(_escape(text[cursor : match.start()]))
        rendered.append(f'<mark>{_escape(match.group(0))}</mark>')
        cursor = match.end()
    rendered.append(_escape(text[cursor:]))
    return ''.join(rendered)


def _render_chunk_provenance(chunk: dict[str, Any]) -> str:
    """The muted page/section line under one rendered chunk, when stored."""
    parts: list[str] = []
    page_start = chunk.get('page_start')
    if page_start is not None:
        parts.append(f'page {_escape(page_start)}')
    section_path = chunk.get('section_path')
    if section_path:
        parts.append(f'section {_escape(section_path)}')
    if not parts:
        return ''
    return f'<p class="chunk-meta muted">{" · ".join(parts)}</p>'


def _render_cited_source_text(
    engine: ResearchOrchestrator,
    selection: UiRequest,
) -> str:
    """Render the stored extracted text when no PDF can be served.

    Most corpus sources are not file-backed, and selecting one used to show
    only a dead end. The store already holds their extracted rag chunks, so
    this reader renders them as escaped, newline-preserving paragraphs with
    their page/section provenance. The store orders chunks by
    ``(source_id, chunk_index)``. The citation excerpt is redacted and shown
    as a callout, and its case-insensitive matches inside the body text are
    wrapped in ``<mark>`` -- escape-first everywhere, so neither the corpus
    nor the excerpt can inject markup.
    """
    source_id = selection.source_id or ''
    chunks = engine.store.list_rag_chunks(source_ids=[source_id])
    try:
        title = engine.store.get_knowledge_source(source_id).title
    except RecordNotFound:
        title = None
    if not chunks:
        return (
            '<h3>Cited source</h3>'
            '<p class="muted">The cited source document is not available. '
            'No extracted text is stored for this source.</p>'
        )
    excerpt = (
        redact_free_text(selection.excerpt)
        if selection.excerpt and selection.excerpt.strip()
        else None
    )
    rendered = [
        '<h3>Cited source</h3>',
        '<div class="source-text">',
        '<p class="muted">The cited source PDF file is unavailable; '
        'showing its stored extracted text instead.</p>',
    ]
    if title:
        rendered.append(f'<p class="source-title">{_escape(title)}</p>')
    if excerpt:
        rendered.append(
            '<div class="excerpt-callout">'
            '<strong>Cited excerpt</strong>'
            f'<p class="excerpt-text">{_escape(excerpt)}</p>'
            '</div>'
        )
    for chunk in chunks:
        rendered.append(
            '<article class="source-chunk">'
            f'<p class="chunk-text">'
            f'{_highlight_escaped(chunk.get("text", ""), excerpt)}</p>'
            f'{_render_chunk_provenance(chunk)}'
            '</article>'
        )
    rendered.append('</div>')
    return ''.join(rendered)


def _render_cited_source(
    engine: ResearchOrchestrator,
    settings: Settings,
    selection: UiRequest,
) -> str:
    if not selection.source_id:
        return ''
    if not document_is_resolvable(engine, settings, selection.source_id):
        return _render_cited_source_text(engine, selection)
    excerpt = (
        redact_free_text(selection.excerpt) if selection.excerpt else None
    )
    url = _pdf_viewer_url(selection.source_id, selection.page, excerpt)
    return (
        '<h3>Cited source</h3>'
        '<div class="pdf-viewer">'
        f'<iframe src="{_escape(url)}" title="Cited source PDF" '
        'loading="lazy"></iframe>'
        '</div>'
    )


def _render_run_view(
    engine: ResearchOrchestrator,
    settings: Settings,
    selection: UiRequest,
) -> str:
    refs = _artifact_refs(engine.store.list_artifacts(selection.run_id or ''))
    if selection.ref:
        preview = (
            '<h3>Preview</h3>'
            + _render_document_body(settings, selection, refs)
        )
    else:
        preview = (
            '<p class="muted">Select a file in the tree to preview its '
            'digest-verified text.</p>'
        )
    return (
        preview
        + '<h3>Run files</h3>'
        + _render_artifact_tree(refs, selection)
    )


def _render_viewer_panel(
    engine: ResearchOrchestrator,
    settings: Settings,
    selection: UiRequest,
) -> str:
    if selection.source_id:
        body_class = 'column-body is-fill'
        body = _render_cited_source(engine, settings, selection)
    elif selection.run_id:
        body_class = 'column-body'
        body = _render_run_view(engine, settings, selection)
    else:
        body_class = 'column-body'
        body = '<p class="muted">Select a source or run.</p>'
    return (
        '<section class="pane" id="viewer">'
        '<div class="column-head"><h2>Viewer</h2></div>'
        f'<div class="{body_class}">{body}</div>'
        '</section>'
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


def _render_citation_marker(
    question: str,
    ordinal: int,
    citation: ChatCitation,
) -> str:
    """One inline superscript marker and its CSS-only hover preview card."""
    excerpt = redact_free_text(citation.excerpt)
    href = _page_url(
        q=question,
        source=citation.source_id,
        # citation.page is the 0-based chunk page_start; the viewer URL and
        # the boxes route both use the 1-based human page number.
        page=str(citation.page + 1) if citation.page is not None else None,
        excerpt=excerpt,
    )
    return (
        f'<a class="cite" href="{_escape(href)}">'
        f'<sup>{ordinal}</sup>'
        '<span class="cite-card">'
        f'<span class="cite-title">{_escape(citation.title)}</span>'
        f'<span class="cite-badge badge-{citation.verdict}">'
        f'{_escape(_CITATION_BADGES[citation.verdict])}</span>'
        '<span class="cite-cta">View source</span>'
        '</span>'
        '</a>'
    )


def _render_chat_answer(question: str, answer: ChatAnswer) -> str:
    """Render the answer with inline superscript citation markers.

    Escape-first: every literal segment is escaped and only a valid ``[n]``
    ordinal (``1 <= n <= len(answer.citations)``) becomes the controlled marker
    element. There is no end-of-answer reference block; the quoted excerpts
    are already inline in the answer text.
    """
    text = redact_free_text(answer.answer)
    citations = answer.citations
    rendered: list[str] = []
    cursor = 0
    for match in _CITATION_ORDINAL_RE.finditer(text):
        ordinal = int(match.group(1))
        if not 1 <= ordinal <= len(citations):
            continue
        rendered.append(_escape(text[cursor : match.start()]))
        rendered.append(
            _render_citation_marker(question, ordinal, citations[ordinal - 1])
        )
        cursor = match.end()
    rendered.append(_escape(text[cursor:]))
    return ''.join(rendered)


def _render_chat_panel(
    chat_service: CorpusChatService | None,
    selection: UiRequest,
) -> str:
    if chat_service is None:
        body = (
            '<p class="muted">Corpus chat is not enabled on this '
            'deployment.</p>'
        )
    else:
        question = selection.question or ''
        display_question = redact_free_text(question)
        body = (
            '<form method="get" action="/ui/" class="ask-form">'
            '<label for="ask-question">Question</label>'
            f'<input id="ask-question" type="text" name="q" '
            f'value="{_escape(display_question)}" '
            'placeholder="Ask about the corpus" autocomplete="off">'
            '<button type="submit">Ask</button>'
            '</form>'
        )
        if question.strip():
            answer = chat_service.answer(question)
            body += (
                '<div class="chat-turn">'
                f'<p><strong>Question:</strong> '
                f'{_escape(display_question)}</p>'
                f'<p><strong>Answer:</strong> '
                f'{_render_chat_answer(display_question, answer)}</p>'
                '</div>'
            )
    return (
        '<section class="pane" id="ask">'
        '<div class="column-head"><h2>Ask the corpus</h2></div>'
        f'<div class="column-body">{body}</div>'
        '</section>'
    )


def render_ui_page(
    engine: ResearchOrchestrator,
    settings: Settings,
    request: UiRequest,
    chat_service: CorpusChatService | None = None,
) -> str:
    """Render the whole read-only notebook as one escaped HTML string."""
    return (
        '<!doctype html>'
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>Glasslab corpus</title>'
        f'<style nonce="{_escape(request.nonce)}">{_PAGE_STYLES}</style>'
        '</head><body>'
        '<header class="masthead">'
        '<h1>Glasslab corpus and reports</h1>'
        '</header>'
        '<main class="notebook">'
        f'{_render_sources_panel(engine, request)}'
        f'{_render_chat_panel(chat_service, request)}'
        f'{_render_viewer_panel(engine, settings, request)}'
        '</main>'
        '</body></html>'
    )


def _ui_headers(nonce: str) -> dict[str, str]:
    # CSP sign-off (#618): default-src 'none' plus a per-response style nonce,
    # and exactly two deliberate deltas. form-action 'self' is required for the
    # zero-JS GET chat form ('none' blocks the submission, so the chat cannot
    # work), and frame-src 'self' is required for the same-origin cited-source
    # PDF iframe. Everything else stays default-deny, including script: there
    # is no script-src at all, so a corpus-authored string can never execute.
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

    @app.api_route(
        '/ui/',
        methods=['GET', 'HEAD'],
        response_class=HTMLResponse,
    )
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
