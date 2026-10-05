"""Real-browser end-to-end QA for the ``/ui`` corpus chat and PDF viewer.

Drives headless Chromium through the loopback operator-token-injecting UI
proxies (``scripts/glasslab-orchestrator-ui-proxy.py``) against a live uvicorn
``app.main:app`` seeded with a synthetic corpus
(``scripts/qa/seed_ui_corpus.py``). Nothing here is mocked: the browser talks
HTTP to the proxies, each proxy injects the operator header, and the app
serves the chat page, the cited-source iframe, the vendored pdf.js assets,
the raw PDF bytes, and the live highlight boxes. Two listeners are live: the
page proxy (every path) and the #620 viewer proxy, scoped to ``/ui/pdf/``.

Assertions (issues #618/#619/#620):

1. the ask form renders and a POST ``/ui/chat`` turn renders an answer whose
   citation is an inline superscript marker with a CSS-only hover preview
   card (source title, verdict badge, "View source") and no footnote list;
2. clicking the citation opens the cited-source iframe at the exact
   ``/ui/pdf/assets/web/highlight.html?source=&page=&excerpt=`` URL, and the
   iframe is deliberately not sandboxed;
3. inside the iframe a canvas renders, the wrapper reports the requested page,
   and at least one highlight rectangle is drawn that overlaps the rendered
   text region (the cited page and the boxes page must be the same physical
   page);
4. zero CSP-violation console errors across the whole flow;
5. direct (unauthenticated) requests to the app are 401 for ``/ui/`` and
   ``/ui/pdf/document.pdf``;
6. the iframe ``src`` is the second viewer origin, a different port than the
   page origin, and a direct ``/runs`` request there is refused with 403 by
   the path-scoped proxy while the page proxy still reaches it;
7. a ``fetch`` from inside the viewer frame to ``<viewer-origin>/runs``
   returns the scoped proxy's 403 refusal and no operator run data.

Every step captures a screenshot and appends to the action log under
``<tempdir>/glasslab-ui-qa/artifacts/`` (see ``ui_qa.ARTIFACTS_DIR``).
"""

from __future__ import annotations

import importlib.util
import json
import re
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlsplit

import pytest

if TYPE_CHECKING:
    from playwright.sync_api import Frame, Page

# The browser stack is installed only in the Playwright venv. The module must
# still collect -- and its portability guard must still run -- in the default
# service test environment, so the browser tests skip by marker rather than
# through a module-level importorskip.
PLAYWRIGHT_INSTALLED = importlib.util.find_spec('playwright') is not None
PLAYWRIGHT_SKIP = pytest.mark.skipif(
    not PLAYWRIGHT_INSTALLED,
    reason='the playwright package is installed only in the Playwright venv',
)

# Importing the fixture registers it in this module's namespace; it lives in a
# plain module rather than a second conftest.py so its basename cannot shadow
# the shared tests/conftest.py that the rest of the suite imports.
from ui_qa import ui_qa  # noqa: F401

# Console text that marks a Content-Security-Policy violation in Chromium.
# Chromium prefixes every blocked-resource report with "Refused to ..."; the
# bare phrase "Content Security Policy" also appears in benign notices (for
# example, "frame-ancestors is ignored when delivered via a <meta> element"),
# which are not violations and must not be counted as such.
CSP_VIOLATION_MARKERS = (
    'refused to load',
    'refused to execute',
    'refused to connect',
    'refused to frame',
    'refused to apply',
    'violates the following content security policy',
)

IFRAME_TITLE = 'Cited source PDF'
HIGHLIGHT_PATH = '/ui/pdf/assets/web/highlight.html'


def _log(env, step: str, action: str, **detail: object) -> None:
    """Append one action-log entry (JSONL + Markdown) for a QA step."""
    entry = {'ts': time.time(), 'step': step, 'action': action, **detail}
    with (env.artifacts_dir / 'action-log.jsonl').open('a', encoding='utf-8') as fh:
        fh.write(json.dumps(entry, sort_keys=True) + '\n')
    rendered = ', '.join(f'{key}={value!r}' for key, value in detail.items())
    with (env.artifacts_dir / 'action-log.md').open('a', encoding='utf-8') as fh:
        fh.write(f'- **{step}** {action}' + (f' — {rendered}' if rendered else '') + '\n')


def _shot(page: Page, env, name: str) -> Path:
    path = env.artifacts_dir / name
    page.screenshot(path=str(path), full_page=True)
    return path


def _csp_violations(messages: list[dict[str, str]]) -> list[dict[str, str]]:
    violations = []
    for message in messages:
        lowered = message['text'].lower()
        if any(marker in lowered for marker in CSP_VIOLATION_MARKERS):
            violations.append(message)
    return violations


def _wait_for_frame(page: Page, prefix: str, timeout_ms: int = 30_000) -> Frame:
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        for frame in page.frames:
            if frame.url.startswith(prefix):
                return frame
        page.wait_for_timeout(100)
    raise AssertionError(
        f'no frame with URL prefix {prefix!r}; frames={[f.url for f in page.frames]}'
    )


def test_ui_qa_defaults_are_portable() -> None:
    """The QA defaults must live under the temp dir, never a user home path."""
    from ui_qa import DEFAULT_ARTIFACTS_DIR, DEFAULT_SCRATCH_ROOT

    temp_root = Path(tempfile.gettempdir()).resolve()
    for name, default in (
        ('DEFAULT_ARTIFACTS_DIR', DEFAULT_ARTIFACTS_DIR),
        ('DEFAULT_SCRATCH_ROOT', DEFAULT_SCRATCH_ROOT),
    ):
        resolved = default.resolve()
        assert '/home/' not in str(resolved), f'{name} is user-specific: {resolved}'
        assert resolved.is_relative_to(temp_root), (
            f'{name}={resolved} is not under the temp dir {temp_root}'
        )


@PLAYWRIGHT_SKIP
def test_ui_corpus_chat_and_pdf_viewer_through_proxy(ui_qa) -> None:
    from playwright.sync_api import expect, sync_playwright

    env = ui_qa
    manifest = env.manifest
    expected_page = str(manifest['page'])

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(viewport={'width': 1440, 'height': 1000})
        page = context.new_page()
        console_messages: list[dict[str, str]] = []
        page_errors: list[str] = []
        page.on(
            'console',
            lambda message: console_messages.append(
                {'type': message.type, 'text': message.text}
            ),
        )
        page.on('pageerror', lambda error: page_errors.append(str(error)))

        # --- 1. the ask form renders through the proxy ---------------------
        response = page.goto(
            env.proxy_origin + '/ui/', wait_until='domcontentloaded'
        )
        assert response is not None, 'no navigation response for /ui/'
        assert response.status == 200, f'/ui/ status {response.status}'
        ask = page.locator('#ask')
        expect(ask).to_be_visible()
        expect(ask.locator('h2')).to_have_text('Ask the corpus')
        form = ask.locator('form.ask-form')
        assert form.get_attribute('method').lower() == 'post'
        assert form.get_attribute('action') == '/ui/chat'
        question_input = ask.locator('input[name="q"]')
        expect(question_input).to_be_visible()
        expect(ask.locator('button[type="submit"]')).to_have_text('Ask')
        _shot(page, env, '01-ask-form.png')
        _log(
            env,
            'ask-form',
            'rendered',
            url=page.url,
            status=response.status,
            screenshot='01-ask-form.png',
        )

        # --- 2. the posted turn renders the answer and its inline citation --
        question_input.fill(manifest['question'])
        with page.expect_navigation(wait_until='domcontentloaded'):
            ask.locator('button[type="submit"]').click()
        turn = page.locator('.chat-turn')
        expect(turn).to_be_visible()
        assert 'c=' in page.url, f'conversation missing from URL: {page.url}'
        expect(turn).to_contain_text('Question:')
        expect(turn).to_contain_text(manifest['question'])
        expect(turn).to_contain_text('Answer:')
        # NotebookLM-style inline citation: a superscript marker in the answer
        # text, no end-of-answer reference list, and a CSS-only hover card.
        markers = turn.locator('a.cite')
        assert markers.count() >= 1, 'expected at least one inline citation'
        assert turn.locator('ul.chat-citations').count() == 0, (
            'the end-of-answer footnote list must be gone'
        )
        assert page.locator('script').count() == 0, 'the page must be script-free'
        marker = markers.first
        expect(marker.locator('sup')).to_have_text('1')
        card = marker.locator('.cite-card')
        expect(card).to_be_hidden()
        marker.hover()
        expect(card).to_be_visible()
        expect(card.locator('.cite-title')).to_have_text(manifest['title'])
        expect(card.locator('.cite-badge')).to_have_text('✓ exact')
        expect(card.locator('.cite-cta')).to_contain_text('View source')
        _shot(page, env, '02-answer-citation-hover.png')
        _log(
            env,
            'answer',
            'rendered',
            url=page.url,
            title=manifest['title'],
            excerpt=manifest['excerpt'],
            badge='✓ exact',
            superscript='1',
            footnote_list=False,
            screenshot='02-answer-citation-hover.png',
        )

        # --- 3. the citation opens the exact unsandboxed viewer iframe -----
        with page.expect_navigation(wait_until='domcontentloaded'):
            marker.click()
        iframe = page.locator(f'iframe[title="{IFRAME_TITLE}"]')
        expect(iframe).to_be_visible()
        assert iframe.get_attribute('sandbox') is None, (
            'the cited-source iframe must not be sandboxed'
        )
        src = iframe.get_attribute('src')
        assert src is not None, 'iframe has no src'
        parsed = urlsplit(src)
        assert parsed.netloc, f'iframe src must be absolute, got {src!r}'
        assert parsed.path == HIGHLIGHT_PATH, f'iframe path {parsed.path!r}'
        params = parse_qs(parsed.query)
        assert params == {
            'source': [manifest['source_id']],
            'page': [expected_page],
            'excerpt': [manifest['excerpt']],
        }, f'unexpected iframe query: {params}'
        iframe_origin = f'{parsed.scheme}://{parsed.netloc}'
        assert iframe_origin == env.viewer_origin, (
            f'iframe src origin {iframe_origin!r} is not the viewer origin '
            f'{env.viewer_origin!r}'
        )
        page_port = urlsplit(env.proxy_origin).port
        viewer_port = urlsplit(env.viewer_origin).port
        assert viewer_port != page_port, (
            f'viewer origin {env.viewer_origin!r} must not share the page '
            f'origin port {page_port}'
        )
        iframe.scroll_into_view_if_needed()
        frame = _wait_for_frame(page, env.viewer_origin + HIGHLIGHT_PATH)
        frame_parsed = urlsplit(frame.url)
        assert (
            f'{frame_parsed.scheme}://{frame_parsed.netloc}'
            == env.viewer_origin
        ), frame.url
        assert frame_parsed.path == HIGHLIGHT_PATH, frame.url
        assert parse_qs(frame_parsed.query) == params, frame.url
        _shot(page, env, '03-cited-source-iframe.png')
        _log(
            env,
            'citation',
            'opened-iframe',
            url=page.url,
            iframe_src=src,
            iframe_origin=iframe_origin,
            viewer_origin=env.viewer_origin,
            page_origin=env.proxy_origin,
            sandboxed=False,
            screenshot='03-cited-source-iframe.png',
        )

        # --- 4. canvas + requested page + highlight rects inside the iframe -
        canvas = frame.locator('#page canvas')
        canvas.wait_for(state='visible', timeout=30_000)
        expect(canvas).to_have_attribute(
            'aria-label', f'Page {expected_page} of the cited source'
        )
        status = frame.locator('#status')
        status.wait_for(state='visible', timeout=30_000)
        expect(status).to_have_attribute('data-tone', 'ok', timeout=30_000)
        meta_text = frame.locator('#meta').inner_text()
        assert f'page {expected_page}' in meta_text, meta_text
        assert manifest['section_title'] in meta_text, meta_text
        status_text = status.inner_text()
        assert re.search(
            rf'Page {expected_page}: \d+ highlights? for the cited excerpt\.',
            status_text,
        ), status_text

        canvas_box = canvas.bounding_box()
        assert canvas_box is not None, 'canvas has no layout box'
        assert canvas_box['width'] > 0 and canvas_box['height'] > 0, canvas_box

        rects = frame.locator('#page .highlight-layer .highlight-rect')
        assert rects.count() >= 1, 'no highlight rectangle for the excerpt'
        rect_box = rects.first.bounding_box()
        assert rect_box is not None, 'highlight rect has no layout box'
        assert rect_box['width'] > 0 and rect_box['height'] > 0, rect_box

        # Both boxes are measured inside the iframe document so the viewer's
        # own scroll position cannot skew the comparison.
        geometry = frame.evaluate(
            """() => {
                const canvas = document.querySelector('#page canvas');
                const rect = document.querySelector(
                    '#page .highlight-layer .highlight-rect'
                );
                if (!canvas || !rect) {
                    return null;
                }
                const canvasRect = canvas.getBoundingClientRect();
                const highlightRect = rect.getBoundingClientRect();
                const context = canvas.getContext('2d');
                const data = context.getImageData(
                    0, 0, canvas.width, canvas.height
                ).data;
                const { width, height } = canvas;
                let dark = 0;
                let minX = width, minY = height, maxX = -1, maxY = -1;
                for (let i = 0; i < data.length; i += 4) {
                    if (data[i] < 128 && data[i + 1] < 128 && data[i + 2] < 128) {
                        dark += 1;
                        const pixel = i / 4;
                        const x = pixel % width;
                        const y = Math.floor(pixel / width);
                        if (x < minX) minX = x;
                        if (y < minY) minY = y;
                        if (x > maxX) maxX = x;
                        if (y > maxY) maxY = y;
                    }
                }
                if (maxX < 0) {
                    return { dark: 0, text: null, highlight: null, overlap: false };
                }
                const scaleX = canvas.clientWidth / width;
                const scaleY = canvas.clientHeight / height;
                const text = {
                    x: minX * scaleX,
                    y: minY * scaleY,
                    width: (maxX - minX + 1) * scaleX,
                    height: (maxY - minY + 1) * scaleY,
                };
                const highlight = {
                    x: highlightRect.left - canvasRect.left,
                    y: highlightRect.top - canvasRect.top,
                    width: highlightRect.width,
                    height: highlightRect.height,
                };
                const overlap =
                    highlight.x < text.x + text.width &&
                    highlight.x + highlight.width > text.x &&
                    highlight.y < text.y + text.height &&
                    highlight.y + highlight.height > text.y;
                return { dark, text, highlight, overlap };
            }"""
        )
        assert geometry is not None, 'no rendered page or highlight rectangle'
        dark_pixels = geometry['dark']
        text_box = geometry['text']
        assert dark_pixels > 0, 'canvas rendered no dark (text) pixels'
        assert text_box is not None, 'dark pixels had no bounding box'
        # A rect merely existing is not enough: the highlight must land on the
        # rendered text. This is the assertion that fails if the page the
        # viewer renders and the page the boxes route searches diverge.
        assert geometry['overlap'], (
            f"highlight {geometry['highlight']} does not overlap rendered "
            f"text {text_box}"
        )
        # Record the raw boxes payload the wrapper consumed: the coordinates
        # are the evidence for the highlight geometry (see the QA report).
        boxes_response = context.request.get(
            env.viewer_origin + '/ui/pdf/boxes',
            params={
                'source': manifest['source_id'],
                'page': expected_page,
                'excerpt': manifest['excerpt'],
            },
        )
        assert boxes_response.status == 200, boxes_response.status
        boxes_payload = boxes_response.json()
        assert boxes_payload['boxes'], boxes_payload
        frame.locator('#page').screenshot(
            path=str(env.artifacts_dir / '04-iframe-page-highlight.png')
        )
        _shot(page, env, '04-iframe-highlight.png')
        _log(
            env,
            'iframe',
            'rendered',
            frame_url=frame.url,
            meta=meta_text,
            status=status_text,
            canvas={'width': canvas_box['width'], 'height': canvas_box['height']},
            dark_pixels=dark_pixels,
            highlight_rects=rects.count(),
            highlight_rect_box={
                'x': rect_box['x'],
                'y': rect_box['y'],
                'width': rect_box['width'],
                'height': rect_box['height'],
            },
            text_box=text_box,
            boxes=boxes_payload['boxes'],
            screenshot='04-iframe-highlight.png',
        )

        # --- 4b. the viewer origin cannot reach the operator read API ------
        runs_url = env.viewer_origin + '/runs'
        viewer_runs = frame.evaluate(
            """async (url) => {
                try {
                    const response = await fetch(url, {
                        headers: {'Accept': 'application/json'},
                    });
                    return {
                        status: response.status,
                        ok: response.ok,
                        body: (await response.text()).slice(0, 400),
                    };
                } catch (error) {
                    return {status: null, ok: false, body: String(error)};
                }
            }""",
            runs_url,
        )
        run_id = manifest['run']['run_id']
        assert viewer_runs['status'] == 403, viewer_runs
        assert not viewer_runs['ok'], viewer_runs
        assert run_id not in viewer_runs['body'], viewer_runs
        _log(
            env,
            'viewer-origin',
            'runs-fetch-refused-in-frame',
            url=runs_url,
            status=viewer_runs['status'],
            body=viewer_runs['body'],
            operator_run_id=run_id,
            operator_data_absent=run_id not in viewer_runs['body'],
        )

        # --- 5. zero CSP-violation console errors --------------------------
        (env.artifacts_dir / 'console-log.jsonl').write_text(
            '\n'.join(json.dumps(message, sort_keys=True) for message in console_messages)
            + '\n',
            encoding='utf-8',
        )
        violations = _csp_violations(console_messages)
        _log(
            env,
            'console',
            'checked',
            console_messages=len(console_messages),
            csp_violations=len(violations),
            page_errors=len(page_errors),
        )
        assert page_errors == [], f'uncaught page errors: {page_errors}'
        assert violations == [], f'CSP violations: {violations}'

        browser.close()


@PLAYWRIGHT_SKIP
def test_ui_requires_operator_token_direct(ui_qa) -> None:
    from playwright.sync_api import sync_playwright

    env = ui_qa
    with sync_playwright() as playwright:
        request = playwright.request.new_context()
        try:
            ui = request.get(env.app_origin + '/ui/')
            assert ui.status == 401, f'direct /ui/ status {ui.status}'
            document = request.get(
                env.app_origin + '/ui/pdf/document.pdf',
                params={'source': env.manifest['source_id']},
            )
            assert document.status == 401, (
                f'direct /ui/pdf/document.pdf status {document.status}'
            )
            # Contrast: the same paths through the token-injecting proxy are
            # authorized, so the 401s above are the auth boundary, not a
            # missing route.
            proxied = request.get(env.proxy_origin + '/ui/')
            assert proxied.status == 200, f'proxied /ui/ status {proxied.status}'
            _log(
                env,
                'auth',
                'checked',
                direct_ui=ui.status,
                direct_ui_body=ui.text()[:200],
                direct_pdf=document.status,
                direct_pdf_body=document.text()[:200],
                proxied_ui=proxied.status,
            )
        finally:
            request.dispose()


@PLAYWRIGHT_SKIP
def test_viewer_origin_is_scoped_to_pdf_paths(ui_qa) -> None:
    """The #620 viewer listener refuses operator API paths before upstream.

    A script that lands on the viewer origin must not be able to read
    ``/runs``: the path-scoped proxy returns 403 before injecting the operator
    token. The contrast through the unscoped page proxy proves the refusal is
    the path scope, not a missing route or an auth failure; the viewer asset
    request proves the same listener does forward the viewer paths.
    """
    from playwright.sync_api import sync_playwright

    env = ui_qa
    with sync_playwright() as playwright:
        request = playwright.request.new_context()
        try:
            refused = request.get(env.viewer_origin + '/runs')
            assert refused.status == 403, (
                f'viewer-origin /runs status {refused.status}'
            )
            refused_body = refused.text()
            assert 'allowlist' in refused_body, refused_body
            page_runs = request.get(env.proxy_origin + '/runs')
            assert page_runs.status == 200, (
                f'page-proxy /runs status {page_runs.status}'
            )
            viewer_asset = request.get(
                env.viewer_origin + '/ui/pdf/assets/web/highlight.html'
            )
            assert viewer_asset.status == 200, (
                f'viewer-origin viewer asset status {viewer_asset.status}'
            )
            _log(
                env,
                'viewer-origin',
                'scope-checked',
                viewer_runs=refused.status,
                viewer_runs_body=refused_body[:200],
                page_proxy_runs=page_runs.status,
                viewer_pdf_asset=viewer_asset.status,
            )
        finally:
            request.dispose()
