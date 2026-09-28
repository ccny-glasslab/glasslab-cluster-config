"""Operator-gated, same-origin PDF serving and highlight-box endpoints.

The corpus UI embeds the vendored pdf.js viewer in a same-origin iframe, so
every byte the browser needs is served from this service: the raw corpus PDF
(``GET /ui/pdf/document.pdf``), the vendored viewer assets
(``GET /ui/pdf/assets/{path}``), and the live highlight rectangles
(``GET /ui/pdf/boxes``). The product iframe loads the first-party
``highlight.html`` wrapper from the asset route; the upstream ``viewer.html``
shell is deliberately not routable here because its relative refs cannot
resolve through this service's route shape.

Two boundaries are enforced here and nowhere else:

* **Raw-root containment.** A document request resolves the source's
  ``canonical_uri`` (which must be a ``file:`` URI), requires a ``.pdf``
  suffix, and requires the resolved path to stay inside
  ``settings.corpus_rag_raw_root``. Traversal, a symlink that escapes the
  root, a non-file scheme, and a missing file all collapse to a bare 404 so
  the response never reveals whether a path exists.
* **Vendored-asset containment.** Asset requests resolve inside
  ``<service_root>/static/pdfjs`` only, with an explicit MIME map because the
  browser refuses a module worker served with the wrong type.

The document route is a Starlette :class:`FileResponse`, which honors HTTP
Range requests; that is what lets the viewer seek without pulling the whole
file. No compression middleware is ever added to these routes: compression
strips ``Content-Length`` and re-chunks the body, which breaks byte ranges.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import unquote, urlsplit

from fastapi import Depends, FastAPI, HTTPException, Query
from starlette.responses import FileResponse

from .config import SERVICE_ROOT
from .storage import RecordNotFound

if TYPE_CHECKING:
    from .config import Settings
    from .corpus_rag.contracts import RagSectionRecord
    from .engine import ResearchOrchestrator

# The vendored pdf.js tree is baked into the service image; it is the only
# directory the asset route may read from.
_PDFJS_ROOT = SERVICE_ROOT / 'static' / 'pdfjs'

# The viewer needs same-origin module workers, blob URLs, and same-origin
# fetches; the policy below is the minimum that permits them and nothing else.
_VIEWER_CSP = (
    "default-src 'none'; script-src 'self' 'wasm-unsafe-eval'; "
    "worker-src 'self' blob:; style-src 'self'; "
    "img-src 'self' blob: data:; media-src blob:; "
    "font-src 'self' data:; connect-src 'self' blob: data:; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'self'"
)
# The raw PDF is a passive document: no script, no subresource, and it may
# only be framed by this same origin.
_DOCUMENT_CSP = "default-src 'none'; frame-ancestors 'self'"

# An excerpt longer than this is not a highlight request; it is a mistake or
# an abuse attempt, and PyMuPDF search cost grows with the needle length.
_MAX_EXCERPT_CHARS = 400

# Explicit MIME map for the vendored tree. ``.mjs`` must be JavaScript or the
# module worker is rejected; ``.wasm`` must be application/wasm or streaming
# compilation fails. Fonts and cmaps are opaque binary to the browser.
_ASSET_MEDIA_TYPES: dict[str, str] = {
    '.mjs': 'text/javascript',
    '.js': 'text/javascript',
    '.wasm': 'application/wasm',
    '.html': 'text/html',
    '.pdf': 'application/pdf',
    '.css': 'text/css',
    '.json': 'application/json',
    '.svg': 'image/svg+xml',
    '.gif': 'image/gif',
    '.ftl': 'text/plain',
    '.bcmap': 'application/octet-stream',
    '.pfb': 'application/octet-stream',
    '.ttf': 'application/octet-stream',
    '.otf': 'application/octet-stream',
    '.woff': 'application/octet-stream',
    '.woff2': 'application/octet-stream',
    '.icc': 'application/octet-stream',
}
_DEFAULT_ASSET_MEDIA_TYPE = 'application/octet-stream'


def _not_found() -> HTTPException:
    # A single opaque 404 for every rejection: the client learns nothing about
    # whether the source, the scheme, or the path was the problem.
    return HTTPException(status_code=404, detail='not found')


def _resolve_document(
    engine: ResearchOrchestrator,
    settings: Settings,
    source_id: str,
) -> Path:
    """Resolve a source id to a contained, regular ``.pdf`` file.

    Raises a bare 404 for an unknown source, a non-``file:`` URI, a non-PDF
    suffix, a symlink, a missing file, or a path outside the raw root.
    """
    try:
        source = engine.store.get_knowledge_source(source_id)
    except RecordNotFound:
        raise _not_found() from None
    parsed = urlsplit(source.canonical_uri)
    if parsed.scheme != 'file' or parsed.netloc not in ('', 'localhost'):
        raise _not_found()
    raw_path = Path(unquote(parsed.path))
    if raw_path.suffix.lower() != '.pdf':
        raise _not_found()
    # Reject a symlink outright rather than following it: resolve() would
    # follow it, and a link that stays inside the root would otherwise be
    # served even though the stored URI named a link, not a file.
    if raw_path.is_symlink():
        raise _not_found()
    try:
        resolved = raw_path.resolve(strict=True)
    except (OSError, RuntimeError):
        raise _not_found() from None
    root = Path(settings.corpus_rag_raw_root).resolve()
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise _not_found()
    return resolved


def _section_for_page(
    sections: list[RagSectionRecord],
    page: int,
) -> dict[str, str] | None:
    target = page - 1

    def span(section: RagSectionRecord) -> float:
        if section.page_start is None or section.page_end is None:
            return float('inf')
        return section.page_end - section.page_start

    containing = [
        section
        for section in sections
        if section.page_start is not None
        and section.page_end is not None
        and section.page_start <= target <= section.page_end
    ]
    if not containing:
        # No section contains the page: rank the sections that begin at or
        # before it by page_start and take the nearest preceding one, then let
        # the smallest-span/path tie-break below choose among equal starts.
        preceding = [
            (section.page_start, section)
            for section in sections
            if section.page_start is not None and section.page_start <= target
        ]
        if not preceding:
            return None
        nearest = max(start for start, _ in preceding)
        containing = [
            section for start, section in preceding if start == nearest
        ]
    # Nested sections overlap: the smallest page span is the most specific
    # (most deeply nested) node, and the path tie-break keeps the pick stable.
    smallest = min(span(section) for section in containing)
    selected = max(
        (section for section in containing if span(section) == smallest),
        key=lambda section: section.path,
    )
    title = (
        selected.title
        if selected.title is not None
        else f'Section {selected.path}'
    )
    return {'path': selected.path, 'title': title}


def document_is_resolvable(
    engine: ResearchOrchestrator,
    settings: Settings,
    source_id: str,
) -> bool:
    """Whether a source id resolves to a servable, contained PDF.

    A probe for the UI: it reuses the document route's exact containment logic
    and turns every rejection into ``False`` instead of a 404, so the page can
    omit the viewer iframe for a source it could not open.
    """
    try:
        _resolve_document(engine, settings, source_id)
    except HTTPException:
        return False
    return True


def _resolve_asset(path: str) -> Path:
    """Resolve an asset path inside the vendored pdf.js tree, or 404."""
    root = _PDFJS_ROOT.resolve()
    candidate = (_PDFJS_ROOT / path).resolve()
    if not candidate.is_relative_to(root) or not candidate.is_file():
        raise _not_found()
    return candidate


def register_ui_pdf_routes(
    app: FastAPI,
    *,
    engine: ResearchOrchestrator,
    settings: Settings,
    require_operator: Callable[..., None],
) -> None:
    """Register the operator-gated ``/ui/pdf/**`` route group.

    ``require_operator`` is the host application's header-auth dependency (a
    closure over its settings in ``main.create_app``), so it is injected
    rather than imported; this module never reads the operator token.
    """

    @app.api_route(
        '/ui/pdf/document.pdf',
        methods=['GET', 'HEAD'],
    )
    def document_pdf(
        source: str = Query(...),
        _: None = Depends(require_operator),
    ) -> FileResponse:
        path = _resolve_document(engine, settings, source)
        return FileResponse(
            path,
            media_type='application/pdf',
            headers={
                'Content-Disposition': 'inline; filename="document.pdf"',
                'X-Content-Type-Options': 'nosniff',
                'Accept-Ranges': 'bytes',
                'Content-Security-Policy': _DOCUMENT_CSP,
            },
        )

    @app.api_route(
        '/ui/pdf/boxes',
        methods=['GET', 'HEAD'],
    )
    def pdf_boxes(
        source: str = Query(...),
        page: int = Query(...),
        excerpt: str | None = Query(default=None),
        _: None = Depends(require_operator),
    ) -> dict[str, object]:
        # The HTTP boundary is 1-based (human page number); the viewer and the
        # chat citation link both pass that number. The PyMuPDF document is
        # 0-based, so index page - 1 and reject page < 1 or beyond the count.
        if page < 1:
            raise HTTPException(status_code=400, detail='page must be >= 1')
        if excerpt is not None and len(excerpt) > _MAX_EXCERPT_CHARS:
            raise HTTPException(
                status_code=400,
                detail=f'excerpt must be <= {_MAX_EXCERPT_CHARS} characters',
            )
        path = _resolve_document(engine, settings, source)
        import pymupdf

        needle = excerpt.strip() if excerpt is not None else None
        with pymupdf.open(str(path)) as document:
            if page > document.page_count:
                raise HTTPException(
                    status_code=400,
                    detail='page out of range',
                )
            pdf_page = document[page - 1]
            page_size = [pdf_page.rect.width, pdf_page.rect.height]
            boxes: list[list[float]] = []
            if needle:
                page_height = pdf_page.rect.height
                for quad in pdf_page.search_for(needle, quads=True):
                    rect = quad.rect
                    # search_for returns top-left page coordinates; the pdf.js
                    # viewport consumes PDF user space (origin bottom-left), so
                    # flip y or the highlight renders mirrored.
                    boxes.append(
                        [
                            rect.x0,
                            page_height - rect.y1,
                            rect.x1,
                            page_height - rect.y0,
                        ]
                    )
        try:
            rag_document = engine.store.get_rag_document(source)
        except RecordNotFound:
            section = None
        else:
            section = _section_for_page(
                engine.store.list_rag_sections(rag_document.doc_id),
                page,
            )
        return {
            'page': page,
            'page_size': page_size,
            'boxes': boxes,
            'section': section,
        }

    @app.api_route(
        '/ui/pdf/assets/{path:path}',
        methods=['GET', 'HEAD'],
    )
    def pdf_asset(
        path: str,
        _: None = Depends(require_operator),
    ) -> FileResponse:
        asset = _resolve_asset(path)
        media_type = _ASSET_MEDIA_TYPES.get(
            asset.suffix.lower(),
            _DEFAULT_ASSET_MEDIA_TYPE,
        )
        headers = {'X-Content-Type-Options': 'nosniff'}
        if media_type == 'text/html':
            headers['Content-Security-Policy'] = _VIEWER_CSP
        return FileResponse(asset, media_type=media_type, headers=headers)
