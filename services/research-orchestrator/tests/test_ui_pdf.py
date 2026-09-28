"""Operator-gated same-origin PDF serving and highlight-box endpoints.

The corpus UI embeds the vendored pdf.js viewer in a same-origin iframe. These
tests drive the four ``/ui/pdf/**`` routes with ``TestClient`` against a real
``SqliteStore`` engine and a real PDF written under the configured raw root:
the document route serves the exact bytes with Range support and no
compression, the boxes route derives highlight rectangles live from the raw
PDF, the asset route serves the vendored tree with an explicit MIME map and a
traversal guard, and the viewer route carries the viewer CSP.
"""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient
import pymupdf

from app.schemas import KnowledgeSource, SourceType
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
                'page': 0,
                'excerpt': 'Hello',
            },
            headers=AUTH_HEADERS,
        )
        absent = client.get(
            '/ui/pdf/boxes',
            params={
                'source': source.source_id,
                'page': 0,
                'excerpt': 'zzzznotfound',
            },
            headers=AUTH_HEADERS,
        )

    assert found.status_code == 200
    body = found.json()
    assert body['page'] == 0
    assert body['page_size'] == [595.0, 842.0]
    assert len(body['boxes']) >= 1
    x0, y0, x1, y1 = body['boxes'][0]
    assert x1 > x0
    assert y1 > y0
    assert absent.status_code == 200
    assert absent.json()['boxes'] == []


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
        bad_page = client.get(
            '/ui/pdf/boxes',
            params={'source': source.source_id, 'page': -1, 'excerpt': 'Hello'},
            headers=AUTH_HEADERS,
        )
        long_excerpt = client.get(
            '/ui/pdf/boxes',
            params={
                'source': source.source_id,
                'page': 0,
                'excerpt': 'x' * 401,
            },
            headers=AUTH_HEADERS,
        )

    assert bad_page.status_code == 400
    assert long_excerpt.status_code == 400


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


def test_viewer_html_csp_frame_ancestors_self(orchestrator_bundle) -> None:
    settings, _, _, _, engine = orchestrator_bundle

    with _client(settings, engine) as client:
        response = client.get('/ui/pdf/viewer.html', headers=AUTH_HEADERS)

    assert response.status_code == 200
    assert response.headers['content-type'].startswith('text/html')
    csp = response.headers['content-security-policy']
    assert "default-src 'none'" in csp
    assert "frame-ancestors 'self'" in csp
    assert "script-src 'self' 'wasm-unsafe-eval'" in csp
