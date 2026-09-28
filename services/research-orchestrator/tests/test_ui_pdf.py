"""Operator-gated same-origin PDF serving and highlight-box endpoints.

The corpus UI embeds the vendored pdf.js viewer in a same-origin iframe. These
tests drive the ``/ui/pdf/**`` routes with ``TestClient`` against a real
``SqliteStore`` engine and a real PDF written under the configured raw root:
the document route serves the exact bytes with Range support and no
compression, the boxes route derives highlight rectangles live from the raw
PDF, and the asset route serves the vendored tree with an explicit MIME map
and a traversal guard. The removed ``/ui/pdf/viewer.html`` shell route is
asserted to 404: the product iframe loads ``highlight.html`` instead.
"""

from __future__ import annotations

import html
from hashlib import sha256
from pathlib import Path
import re
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient
import pymupdf

from app.corpus_rag.chat import CorpusChatService
from app.corpus_rag.contracts import (
    RagChunkRecord,
    RagDocumentRecord,
    RagSectionRecord,
)
from app.schemas import KnowledgeSource, SourceType
from app.ui import register_ui_routes
from app.ui_pdf import register_ui_pdf_routes

OPERATOR_TOKEN = 'test-operator-token'
AUTH_HEADERS = {'X-Glasslab-Operator-Token': OPERATOR_TOKEN}
_PDF_TEXT = 'Hello world from the corpus'


def _require_operator_token(
    supplied: str | None = Header(
        default=None,
        alias='X-Glasslab-Operator-Token',
    ),
) -> None:
    # Mirrors main.create_app's closure shape: the dependency is injected, not
    # imported, so the PDF module never knows the token.
    if supplied is None or supplied != OPERATOR_TOKEN:
        raise HTTPException(
            status_code=401,
            detail='valid operator token required',
        )


def _pdf_app(settings, engine) -> FastAPI:
    app = FastAPI()
    register_ui_pdf_routes(
        app,
        engine=engine,
        settings=settings,
        require_operator=_require_operator_token,
    )
    return app


def _client(settings, engine) -> TestClient:
    return TestClient(_pdf_app(settings, engine))


def _raw_settings(settings, raw_root: Path):
    return settings.model_copy(
        update={'corpus_rag_raw_root': str(raw_root)}
    )


def _make_pdf(path: Path, text: str = _PDF_TEXT) -> bytes:
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 72), text)
    data = document.tobytes()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return data


def _make_multipage_pdf(path: Path, page_texts: list[str]) -> bytes:
    """Write a PDF with one distinct text line per page (one-based order)."""
    document = pymupdf.open()
    for text in page_texts:
        page = document.new_page()
        page.insert_text((72, 72), text)
    data = document.tobytes()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return data


def _ui_and_pdf_client(settings, engine, chat_service) -> TestClient:
    """One app exposing both surfaces, so page bases can be cross-checked."""
    app = FastAPI()
    register_ui_routes(
        app,
        engine=engine,
        settings=settings,
        require_operator=_require_operator_token,
        chat_service=chat_service,
    )
    register_ui_pdf_routes(
        app,
        engine=engine,
        settings=settings,
        require_operator=_require_operator_token,
    )
    return TestClient(app)


def _register_source(engine, path: Path) -> KnowledgeSource:
    source = KnowledgeSource(
        source_type=SourceType.PAPER,
        canonical_uri=path.as_uri(),
        digest=sha256(path.read_bytes()).hexdigest(),
        title='Corpus PDF',
    )
    engine.store.save_knowledge_source(source)
    return source


def test_document_pdf_returns_200_and_bytes(orchestrator_bundle, tmp_path) -> None:
    settings, _, _, _, engine = orchestrator_bundle
    raw_root = tmp_path / 'rag-raw'
    settings = _raw_settings(settings, raw_root)
    pdf_path = raw_root / 'paper.pdf'
    data = _make_pdf(pdf_path)
    source = _register_source(engine, pdf_path)

    with _client(settings, engine) as client:
        response = client.get(
            '/ui/pdf/document.pdf',
            params={'source': source.source_id},
            headers=AUTH_HEADERS,
        )

    assert response.status_code == 200
    assert response.content == data
    assert sha256(response.content).hexdigest() == sha256(data).hexdigest()
    assert response.headers['content-type'] == 'application/pdf'
    assert response.headers['accept-ranges'] == 'bytes'
    assert 'content-encoding' not in response.headers


def test_document_pdf_range_returns_206(orchestrator_bundle, tmp_path) -> None:
    settings, _, _, _, engine = orchestrator_bundle
    raw_root = tmp_path / 'rag-raw'
    settings = _raw_settings(settings, raw_root)
    pdf_path = raw_root / 'paper.pdf'
    data = _make_pdf(pdf_path)
    source = _register_source(engine, pdf_path)

    with _client(settings, engine) as client:
        response = client.get(
            '/ui/pdf/document.pdf',
            params={'source': source.source_id},
            headers={**AUTH_HEADERS, 'Range': 'bytes=0-9'},
        )

    assert response.status_code == 206
    assert response.content == data[:10]
    assert response.headers['content-range'] == f'bytes 0-9/{len(data)}'


def test_document_pdf_unknown_source_404(orchestrator_bundle, tmp_path) -> None:
    settings, _, _, _, engine = orchestrator_bundle
    settings = _raw_settings(settings, tmp_path / 'rag-raw')

    with _client(settings, engine) as client:
        response = client.get(
            '/ui/pdf/document.pdf',
            params={'source': 'no-such-source'},
            headers=AUTH_HEADERS,
        )

    assert response.status_code == 404


def test_document_pdf_rejects_path_escape_and_non_pdf(
    orchestrator_bundle,
    tmp_path,
) -> None:
    settings, _, _, _, engine = orchestrator_bundle
    raw_root = tmp_path / 'rag-raw'
    settings = _raw_settings(settings, raw_root)
    # A real PDF that lives outside the configured raw root.
    outside = tmp_path / 'outside.pdf'
    _make_pdf(outside)
    escaped = _register_source(engine, outside)
    # A non-PDF file inside the raw root.
    non_pdf = raw_root / 'notes.txt'
    non_pdf.parent.mkdir(parents=True, exist_ok=True)
    non_pdf.write_text('not a pdf')
    wrong_type = _register_source(engine, non_pdf)

    with _client(settings, engine) as client:
        escape_response = client.get(
            '/ui/pdf/document.pdf',
            params={'source': escaped.source_id},
            headers=AUTH_HEADERS,
        )
        type_response = client.get(
            '/ui/pdf/document.pdf',
            params={'source': wrong_type.source_id},
            headers=AUTH_HEADERS,
        )

    assert escape_response.status_code == 404
    assert type_response.status_code == 404


def test_boxes_returns_rects_and_empty_on_absent(
    orchestrator_bundle,
    tmp_path,
) -> None:
    settings, _, _, _, engine = orchestrator_bundle
    raw_root = tmp_path / 'rag-raw'
    settings = _raw_settings(settings, raw_root)
    pdf_path = raw_root / 'paper.pdf'
    _make_pdf(pdf_path)
    source = _register_source(engine, pdf_path)

    with _client(settings, engine) as client:
        found = client.get(
            '/ui/pdf/boxes',
            params={
                'source': source.source_id,
                'page': 1,
                'excerpt': 'Hello',
            },
            headers=AUTH_HEADERS,
        )
        absent = client.get(
            '/ui/pdf/boxes',
            params={
                'source': source.source_id,
                'page': 1,
                'excerpt': 'zzzznotfound',
            },
            headers=AUTH_HEADERS,
        )

    assert found.status_code == 200
    body = found.json()
    assert body['page'] == 1
    assert body['page_size'] == [595.0, 842.0]
    assert len(body['boxes']) >= 1
    x0, y0, x1, y1 = body['boxes'][0]
    assert x1 > x0
    assert y1 > y0
    # The box is in PDF user space (origin bottom-left): text drawn near the
    # top of the page must report a y range near the page bottom, or the
    # pdf.js viewport draws the highlight mirrored.
    assert y0 > body['page_size'][1] / 2
    assert absent.status_code == 200
    assert absent.json()['boxes'] == []


def test_boxes_page_is_one_based(orchestrator_bundle, tmp_path) -> None:
    """The ``page`` request is 1-based: it indexes ``document[page - 1]``.

    Distinct text per page makes an off-by-one observable: searching page 1
    for page 1's text must find boxes, page 2 for page 2's text must find
    boxes, and page 2 for page 1's text must find nothing.
    """
    settings, _, _, _, engine = orchestrator_bundle
    raw_root = tmp_path / 'rag-raw'
    settings = _raw_settings(settings, raw_root)
    pdf_path = raw_root / 'two-pages.pdf'
    _make_multipage_pdf(pdf_path, ['alpha page one', 'beta page two'])
    source = _register_source(engine, pdf_path)

    with _client(settings, engine) as client:
        first = client.get(
            '/ui/pdf/boxes',
            params={
                'source': source.source_id,
                'page': 1,
                'excerpt': 'alpha',
            },
            headers=AUTH_HEADERS,
        )
        second = client.get(
            '/ui/pdf/boxes',
            params={
                'source': source.source_id,
                'page': 2,
                'excerpt': 'beta',
            },
            headers=AUTH_HEADERS,
        )
        off_by_one = client.get(
            '/ui/pdf/boxes',
            params={
                'source': source.source_id,
                'page': 2,
                'excerpt': 'alpha',
            },
            headers=AUTH_HEADERS,
        )
        out_of_range = client.get(
            '/ui/pdf/boxes',
            params={
                'source': source.source_id,
                'page': 3,
                'excerpt': 'alpha',
            },
            headers=AUTH_HEADERS,
        )

    assert first.status_code == 200
    assert first.json()['page'] == 1
    assert first.json()['boxes']
    assert second.status_code == 200
    assert second.json()['page'] == 2
    assert second.json()['boxes']
    assert off_by_one.status_code == 200
    assert off_by_one.json()['boxes'] == []
    assert out_of_range.status_code == 400


def test_rendered_citation_page_and_boxes_page_agree(
    orchestrator_bundle,
    tmp_path,
) -> None:
    """The page the chat link asks the viewer to render is the one boxes uses.

    A 0-based ``page_start`` must reach both the viewer URL and the boxes
    route as the same 1-based human page, so the highlight lands on the
    rendered page.
    """
    settings, _, _, _, engine = orchestrator_bundle
    raw_root = tmp_path / 'rag-raw'
    settings = _raw_settings(settings, raw_root)
    pdf_path = raw_root / 'cited.pdf'
    cited_text = 'Resampling improves stability of small samples.'
    _make_multipage_pdf(pdf_path, [cited_text, 'an unrelated second page'])
    source = _register_source(engine, pdf_path)
    engine.store.replace_rag_chunks(
        source.source_id,
        [
            RagChunkRecord(
                chunk_id=f'{source.source_id}::c0',
                source_id=source.source_id,
                kind='evidence_span',
                chunk_index=0,
                text=cited_text,
                digest=sha256(cited_text.encode()).hexdigest(),
                token_count=max(1, len(cited_text.split())),
                page_start=0,
                page_end=0,
            )
        ],
    )
    chat_service = CorpusChatService(engine.store)

    with _ui_and_pdf_client(settings, engine, chat_service) as client:
        page = client.get(
            '/ui/',
            params={'q': 'resampling stability small samples'},
            headers=AUTH_HEADERS,
        )
        href = re.search(r'href="(/ui/\?[^"]*source=[^"]*)"', page.text)
        assert href is not None
        followed = client.get(
            html.unescape(href.group(1)),
            headers=AUTH_HEADERS,
        )
        match = re.search(r'<iframe[^>]*\bsrc="([^"]+)"[^>]*>', followed.text)
        assert match is not None
        frame_src = html.unescape(match.group(1))
        rendered_page = int(
            parse_qs(urlsplit(frame_src).query)['page'][0]
        )
        boxes = client.get(
            '/ui/pdf/boxes',
            params={
                'source': source.source_id,
                'page': rendered_page,
                'excerpt': cited_text,
            },
            headers=AUTH_HEADERS,
        )

    assert rendered_page == 1  # page_start=0 is human page 1
    assert boxes.status_code == 200
    assert boxes.json()['page'] == rendered_page
    assert boxes.json()['boxes']


def test_boxes_includes_section_for_matched_page(
    orchestrator_bundle,
    tmp_path,
) -> None:
    """The boxes payload names the section under the 1-based page.

    Section pages are 0-based while the boxes route is 1-based, so the route
    compares against ``page - 1``. Nested sections overlap: the most specific
    (smallest page span) wins, and a missing/None title falls back to
    ``Section <path>``.
    """
    settings, _, _, _, engine = orchestrator_bundle
    raw_root = tmp_path / 'rag-raw'
    settings = _raw_settings(settings, raw_root)
    pdf_path = raw_root / 'sectioned.pdf'
    _make_multipage_pdf(
        pdf_path,
        ['alpha one', 'beta two', 'gamma three', 'delta four'],
    )
    source = _register_source(engine, pdf_path)
    document = RagDocumentRecord(
        source_id=source.source_id,
        doc_type='paper',
        title='Sectioned',
        extraction_version='v1',
    )
    engine.store.upsert_rag_document(document)
    engine.store.replace_rag_sections(
        document.doc_id,
        [
            RagSectionRecord(
                doc_id=document.doc_id, path='1', level=1, title='Intro',
                page_start=0, page_end=0,
            ),
            RagSectionRecord(
                doc_id=document.doc_id, path='1.1', level=2, title='Deep',
                page_start=1, page_end=1,
            ),
            RagSectionRecord(
                doc_id=document.doc_id, path='2', level=1, title=None,
                page_start=2, page_end=2,
            ),
            RagSectionRecord(
                doc_id=document.doc_id, path='0', level=1, title='NoPages',
                page_start=None, page_end=None,
            ),
        ],
    )

    # A second source with no rag document: the route must not fail the boxes
    # lookup just because no section tree exists.
    orphan_path = raw_root / 'orphan.pdf'
    _make_pdf(orphan_path, 'orphan text')
    orphan = _register_source(engine, orphan_path)

    with _client(settings, engine) as client:
        contained = client.get(
            '/ui/pdf/boxes',
            params={'source': source.source_id, 'page': 2, 'excerpt': 'beta'},
            headers=AUTH_HEADERS,
        )
        fallback = client.get(
            '/ui/pdf/boxes',
            params={'source': source.source_id, 'page': 4, 'excerpt': 'delta'},
            headers=AUTH_HEADERS,
        )
        untitled = client.get(
            '/ui/pdf/boxes',
            params={'source': source.source_id, 'page': 3, 'excerpt': 'gamma'},
            headers=AUTH_HEADERS,
        )
        first_page = client.get(
            '/ui/pdf/boxes',
            params={'source': source.source_id, 'page': 1, 'excerpt': 'alpha'},
            headers=AUTH_HEADERS,
        )
        orphaned = client.get(
            '/ui/pdf/boxes',
            params={'source': orphan.source_id, 'page': 1, 'excerpt': 'orphan'},
            headers=AUTH_HEADERS,
        )

    assert contained.status_code == 200
    # page 2 is 0-based 1: both '1' (0-0) and '1.1' (1-1) are candidates; the
    # smallest span wins.
    assert contained.json()['section'] == {'path': '1.1', 'title': 'Deep'}
    # page 4 is 0-based 3: no section contains it, so the nearest preceding one
    # ('2' at 2-2) is chosen.
    assert fallback.json()['section'] == {'path': '2', 'title': 'Section 2'}
    # A None title falls back to the path-derived label.
    assert untitled.json()['section'] == {'path': '2', 'title': 'Section 2'}
    assert first_page.json()['section'] == {'path': '1', 'title': 'Intro'}
    assert orphaned.status_code == 200
    assert orphaned.json()['section'] is None


def test_boxes_whitespace_only_excerpt_returns_empty(
    orchestrator_bundle,
    tmp_path,
) -> None:
    """A whitespace-only excerpt is not a highlight request.

    PyMuPDF matches the spaces between words, so an unstripped ``'   '`` would
    return spurious rectangles; the route must strip and treat it as absent.
    """
    settings, _, _, _, engine = orchestrator_bundle
    raw_root = tmp_path / 'rag-raw'
    settings = _raw_settings(settings, raw_root)
    pdf_path = raw_root / 'paper.pdf'
    _make_pdf(pdf_path)
    source = _register_source(engine, pdf_path)

    with _client(settings, engine) as client:
        response = client.get(
            '/ui/pdf/boxes',
            params={
                'source': source.source_id,
                'page': 1,
                'excerpt': '   ',
            },
            headers=AUTH_HEADERS,
        )

    assert response.status_code == 200
    assert response.json()['boxes'] == []


def test_boxes_without_excerpt_returns_section_and_no_boxes(
    orchestrator_bundle,
    tmp_path,
) -> None:
    """A page-only request returns the section with an empty box list.

    A citation may carry a page with no excerpt; the wrapper still fetches
    boxes so the cited section renders. The route accepts the absent excerpt
    and returns ``section`` beside ``boxes == []``.
    """
    settings, _, _, _, engine = orchestrator_bundle
    raw_root = tmp_path / 'rag-raw'
    settings = _raw_settings(settings, raw_root)
    pdf_path = raw_root / 'sectioned.pdf'
    _make_multipage_pdf(pdf_path, ['alpha one', 'beta two'])
    source = _register_source(engine, pdf_path)
    document = RagDocumentRecord(
        source_id=source.source_id,
        doc_type='paper',
        title='Sectioned',
        extraction_version='v1',
    )
    engine.store.upsert_rag_document(document)
    engine.store.replace_rag_sections(
        document.doc_id,
        [
            RagSectionRecord(
                doc_id=document.doc_id, path='1', level=1, title='Intro',
                page_start=0, page_end=0,
            ),
        ],
    )

    with _client(settings, engine) as client:
        response = client.get(
            '/ui/pdf/boxes',
            params={'source': source.source_id, 'page': 1},
            headers=AUTH_HEADERS,
        )

    assert response.status_code == 200
    body = response.json()
    assert body['boxes'] == []
    assert body['section'] == {'path': '1', 'title': 'Intro'}


def test_boxes_rejects_bad_page_and_long_excerpt(
    orchestrator_bundle,
    tmp_path,
) -> None:
    settings, _, _, _, engine = orchestrator_bundle
    raw_root = tmp_path / 'rag-raw'
    settings = _raw_settings(settings, raw_root)
    pdf_path = raw_root / 'paper.pdf'
    _make_pdf(pdf_path)
    source = _register_source(engine, pdf_path)

    with _client(settings, engine) as client:
        negative_page = client.get(
            '/ui/pdf/boxes',
            params={'source': source.source_id, 'page': -1, 'excerpt': 'Hello'},
            headers=AUTH_HEADERS,
        )
        zero_page = client.get(
            '/ui/pdf/boxes',
            params={'source': source.source_id, 'page': 0, 'excerpt': 'Hello'},
            headers=AUTH_HEADERS,
        )
        out_of_range = client.get(
            '/ui/pdf/boxes',
            params={'source': source.source_id, 'page': 2, 'excerpt': 'Hello'},
            headers=AUTH_HEADERS,
        )
        long_excerpt = client.get(
            '/ui/pdf/boxes',
            params={
                'source': source.source_id,
                'page': 1,
                'excerpt': 'x' * 401,
            },
            headers=AUTH_HEADERS,
        )

    assert negative_page.status_code == 400
    assert zero_page.status_code == 400
    assert out_of_range.status_code == 400
    assert long_excerpt.status_code == 400


def test_document_pdf_head_200(orchestrator_bundle, tmp_path) -> None:
    """HEAD on the read routes answers 200, like the GET-only routes they wrap.

    The loopback UI proxy forwards GET and HEAD; a 405 on HEAD would break a
    proxy health probe even though the page itself is GET-only.
    """
    settings, _, _, _, engine = orchestrator_bundle
    raw_root = tmp_path / 'rag-raw'
    settings = _raw_settings(settings, raw_root)
    pdf_path = raw_root / 'paper.pdf'
    _make_pdf(pdf_path)
    source = _register_source(engine, pdf_path)

    with _client(settings, engine) as client:
        document = client.head(
            '/ui/pdf/document.pdf',
            params={'source': source.source_id},
            headers=AUTH_HEADERS,
        )
        asset = client.head(
            '/ui/pdf/assets/web/viewer.css',
            headers=AUTH_HEADERS,
        )
        boxes = client.head(
            '/ui/pdf/boxes',
            params={'source': source.source_id, 'page': 1},
            headers=AUTH_HEADERS,
        )

    assert document.status_code == 200
    assert document.headers['content-type'] == 'application/pdf'
    assert asset.status_code == 200
    assert boxes.status_code == 200


def test_asset_mjs_mime_and_no_traversal(orchestrator_bundle) -> None:
    settings, _, _, _, engine = orchestrator_bundle

    with _client(settings, engine) as client:
        asset = client.get(
            '/ui/pdf/assets/build/pdf.worker.mjs',
            headers=AUTH_HEADERS,
        )
        traversal = client.get(
            '/ui/pdf/assets/..%2F..%2Fapp%2Fmain.py',
            headers=AUTH_HEADERS,
        )

    assert asset.status_code == 200
    assert asset.headers['content-type'].startswith('text/javascript')
    assert traversal.status_code == 404


def test_viewer_html_route_is_removed(orchestrator_bundle) -> None:
    """The upstream viewer shell is not routable; the product uses highlight.html.

    ``/ui/pdf/viewer.html`` named a shell whose relative refs cannot resolve
    through this service's route shape, so it is removed rather than redirected.
    """
    settings, _, _, _, engine = orchestrator_bundle

    with _client(settings, engine) as client:
        response = client.get('/ui/pdf/viewer.html', headers=AUTH_HEADERS)

    assert response.status_code == 404


def test_asset_subresources_resolve(orchestrator_bundle) -> None:
    """Every relative ref the highlight wrapper emits resolves to a real asset.

    ``highlight.html`` links ``viewer.css`` and imports ``pdf.mjs`` and the
    locale catalog relative to itself; if any 404s the viewer renders blank.
    """
    settings, _, _, _, engine = orchestrator_bundle

    with _client(settings, engine) as client:
        css = client.get('/ui/pdf/assets/web/viewer.css', headers=AUTH_HEADERS)
        module = client.get('/ui/pdf/assets/build/pdf.mjs', headers=AUTH_HEADERS)
        locale = client.get(
            '/ui/pdf/assets/web/locale/locale.json',
            headers=AUTH_HEADERS,
        )

    assert css.status_code == 200
    assert css.headers['content-type'].startswith('text/css')
    assert module.status_code == 200
    assert module.headers['content-type'].startswith('text/javascript')
    assert locale.status_code == 200
    assert locale.headers['content-type'].startswith('application/json')


def test_asset_pdf_served_as_application_pdf(orchestrator_bundle) -> None:
    """A ``.pdf`` under the vendored tree must be ``application/pdf``.

    The MIME map is explicit because the browser refuses a viewer subresource
    served with the wrong type; a pdf must not fall back to octet-stream.
    """
    settings, _, _, _, engine = orchestrator_bundle

    with _client(settings, engine) as client:
        response = client.get(
            '/ui/pdf/assets/web/compressed.tracemonkey-pldi-09.pdf',
            headers=AUTH_HEADERS,
        )

    assert response.status_code == 200
    assert response.headers['content-type'] == 'application/pdf'
