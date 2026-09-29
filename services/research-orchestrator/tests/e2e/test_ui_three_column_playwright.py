"""Real-browser layout QA for the three-column ``/ui`` notebook.

Drives headless Chromium through the loopback operator-token-injecting UI
proxy (``scripts/glasslab-orchestrator-ui-proxy.py``) against a live uvicorn
``app.main:app`` seeded with a synthetic corpus and a completed run
(``scripts/qa/seed_ui_corpus.py``). Nothing here is mocked: the browser talks
HTTP to the proxy, the proxy injects the operator header, and the app serves
the notebook, the artifact tree, the cited-source iframe, and the PDF bytes.

Assertions:

1. the default page renders the Sources, Ask the corpus, and Viewer columns
   side by side, each with an internal scroller, while the document itself
   does not scroll past the viewport;
2. selecting the seeded run renders its artifact tree (native ``<details>``
   folders grouped by directory) in the Viewer, and clicking a linkable file
   renders its digest-verified text preview;
3. selecting the seeded textbook renders the PDF viewer iframe inside the
   Viewer column and lands on the Corpus sources tab;
4. the page carries no ``<script>`` element, no inline ``style`` attribute,
   and produces no CSP-violation console error and no page error;
5. at a narrow width (1024x768) a corpus table with the live corpus's order
   of magnitude of rows still scrolls inside the Sources column instead of
   stretching the document to tens of thousands of pixels.

Screenshots ``ui-3col-default.png``, ``ui-3col-runtree.png``,
``ui-3col-pdf.png``, ``ui-3col-mobile.png``, and ``ui-3col-narrow-corpus.png``
are written to the QA artifacts directory (``ui_qa.ARTIFACTS_DIR``).
"""

from __future__ import annotations

import importlib.util
import json
import time
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlencode

import pytest

if TYPE_CHECKING:
    from playwright.sync_api import Frame, Page

# The browser stack is installed only in the Playwright venv; the module must
# still collect in the default service test environment.
PLAYWRIGHT_INSTALLED = importlib.util.find_spec('playwright') is not None
PLAYWRIGHT_SKIP = pytest.mark.skipif(
    not PLAYWRIGHT_INSTALLED,
    reason='the playwright package is installed only in the Playwright venv',
)

# Importing the fixture registers it in this module's namespace; it lives in a
# plain module rather than a second conftest.py so its basename cannot shadow
# the shared tests/conftest.py that the rest of the suite imports.
from ui_qa import ui_qa  # noqa: F401

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
    entry = {'ts': time.time(), 'step': step, 'action': action, **detail}
    with (env.artifacts_dir / 'action-log.jsonl').open(
        'a', encoding='utf-8'
    ) as handle:
        handle.write(json.dumps(entry, sort_keys=True) + '\n')
    rendered = ', '.join(
        f'{key}={value!r}' for key, value in detail.items()
    )
    with (env.artifacts_dir / 'action-log.md').open(
        'a', encoding='utf-8'
    ) as handle:
        handle.write(
            f'- **{step}** {action}'
            + (f' — {rendered}' if rendered else '')
            + '\n'
        )


def _shot(page: Page, env, name: str) -> Path:
    path = env.artifacts_dir / name
    page.screenshot(path=str(path))
    return path


def _csp_violations(messages: list[dict[str, str]]) -> list[dict[str, str]]:
    return [
        message
        for message in messages
        if any(marker in message['text'].lower() for marker in CSP_VIOLATION_MARKERS)
    ]


def _wait_for_frame(page: Page, prefix: str, timeout_ms: int = 30_000) -> Frame:
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        for frame in page.frames:
            if frame.url.startswith(prefix):
                return frame
        page.wait_for_timeout(100)
    raise AssertionError(
        f'no frame with URL prefix {prefix!r}; '
        f'frames={[frame.url for frame in page.frames]}'
    )


def _overflow_y(page: Page, selector: str) -> str:
    return page.evaluate(
        'selector => getComputedStyle('
        'document.querySelector(selector)).overflowY',
        selector,
    )


@PLAYWRIGHT_SKIP
def test_ui_three_column_notebook_and_viewer(ui_qa) -> None:
    from playwright.sync_api import expect, sync_playwright

    env = ui_qa
    manifest = env.manifest
    run = manifest['run']
    run_id = run['run_id']

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

        # --- 1. the default notebook: three columns, no page scrolling ----
        response = page.goto(
            env.proxy_origin + '/ui/', wait_until='domcontentloaded'
        )
        assert response is not None, 'no navigation response for /ui/'
        assert response.status == 200, f'/ui/ status {response.status}'
        for column in ('#sources', '#ask', '#viewer'):
            expect(page.locator(column)).to_be_visible()
        boxes = {
            column: page.locator(column).bounding_box()
            for column in ('#sources', '#ask', '#viewer')
        }
        for column, box in boxes.items():
            assert box is not None and box['width'] > 0, (column, box)
            assert box['x'] + box['width'] <= 1440, (column, box)
        assert boxes['#sources']['x'] + boxes['#sources']['width'] <= (
            boxes['#ask']['x'] + 1
        ), boxes
        assert boxes['#ask']['x'] + boxes['#ask']['width'] <= (
            boxes['#viewer']['x'] + 1
        ), boxes

        viewport_height = page.evaluate('window.innerHeight')
        document_height = page.evaluate('document.documentElement.scrollHeight')
        assert document_height <= viewport_height + 1, (
            f'document scrolls ({document_height} > {viewport_height}); '
            'the columns must scroll internally'
        )
        # Each column body owns the scrolling, not the document.
        for scroller in ('#sources .tab-panels', '#ask .column-body'):
            assert _overflow_y(page, scroller) == 'auto', scroller
        # Zero script and zero inline style: the tabs are pure CSS.
        assert page.locator('script').count() == 0
        assert page.locator('[style]').count() == 0
        expect(page.locator('#panel-runs')).to_be_visible()
        expect(page.locator('#panel-corpus')).to_be_hidden()
        _shot(page, env, 'ui-3col-default.png')
        _log(
            env,
            'layout',
            'default-three-column',
            url=page.url,
            columns={key: value for key, value in boxes.items()},
            document_height=document_height,
            viewport_height=viewport_height,
            screenshot='ui-3col-default.png',
        )

        # --- 2. the run selection renders the artifact tree in the viewer -
        with page.expect_navigation(wait_until='domcontentloaded'):
            page.locator(f'#panel-runs a[href*="{run_id}"]').first.click()
        viewer = page.locator('#viewer')
        expect(viewer.locator('details.tree-folder').first).to_be_visible()
        viewer_text = viewer.inner_text()
        for folder in ('reports/', 'protocol/', 'beacon/', 'shared-artifacts/'):
            assert folder in viewer_text, viewer_text
        assert 'plan.md' in viewer_text, viewer_text
        _shot(page, env, 'ui-3col-runtree.png')
        _log(
            env,
            'run',
            'file-tree-rendered',
            url=page.url,
            folders=[
                'reports/',
                'protocol/',
                'beacon/',
                'shared-artifacts/',
            ],
            screenshot='ui-3col-runtree.png',
        )

        with page.expect_navigation(wait_until='domcontentloaded'):
            page.locator('a[href*="ref=reports%2Freport.md"]').first.click()
        expect(viewer.locator('pre')).to_contain_text('QA report')
        _log(env, 'run', 'preview-rendered', url=page.url)

        # --- 3. the source selection renders the PDF iframe in the viewer --
        source_query = urlencode(
            {
                'source': manifest['source_id'],
                'page': manifest['page'],
                'excerpt': manifest['excerpt'],
            }
        )
        response = page.goto(
            env.proxy_origin + f'/ui/?{source_query}',
            wait_until='domcontentloaded',
        )
        assert response is not None and response.status == 200
        iframe = page.locator(f'iframe[title="{IFRAME_TITLE}"]')
        expect(iframe).to_be_visible()
        assert page.locator('#tab-corpus').is_checked(), (
            'selecting a source must land on the Corpus sources tab'
        )
        expect(page.locator('#panel-corpus')).to_be_visible()
        iframe_box = iframe.bounding_box()
        viewer_box = page.locator('#viewer').bounding_box()
        assert iframe_box is not None and viewer_box is not None
        assert iframe_box['height'] >= 400, iframe_box
        assert iframe_box['width'] <= viewer_box['width'], (
            iframe_box,
            viewer_box,
        )
        frame = _wait_for_frame(page, env.proxy_origin + HIGHLIGHT_PATH)
        canvas = frame.locator('#page canvas')
        canvas.wait_for(state='visible', timeout=30_000)
        status = frame.locator('#status')
        expect(status).to_have_attribute('data-tone', 'ok', timeout=30_000)
        assert page.locator('script').count() == 0
        assert page.locator('[style]').count() == 0
        _shot(page, env, 'ui-3col-pdf.png')
        _log(
            env,
            'source',
            'pdf-in-viewer',
            url=page.url,
            iframe_src=iframe.get_attribute('src'),
            iframe_box=iframe_box,
            viewer_box=viewer_box,
            status=status.inner_text(),
            screenshot='ui-3col-pdf.png',
        )

        # --- 4. zero CSP-violation console errors, zero page errors ---------
        (env.artifacts_dir / 'console-log.jsonl').write_text(
            '\n'.join(
                json.dumps(message, sort_keys=True)
                for message in console_messages
            )
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
def test_ui_notebook_stacks_below_1100px(ui_qa) -> None:
    """Below 1100px the notebook is one column (chat first), each usable."""
    from playwright.sync_api import expect, sync_playwright

    env = ui_qa
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(viewport={'width': 820, 'height': 1000})
        page = context.new_page()
        response = page.goto(
            env.proxy_origin + '/ui/', wait_until='domcontentloaded'
        )
        assert response is not None and response.status == 200

        boxes = {
            column: page.locator(column).bounding_box()
            for column in ('#ask', '#sources', '#viewer')
        }
        for column, box in boxes.items():
            assert box is not None and box['width'] > 0, (column, box)
        # Chat first, then sources, then viewer, all in one stacked band.
        assert boxes['#ask']['y'] < boxes['#sources']['y']
        assert boxes['#sources']['y'] < boxes['#viewer']['y']
        assert abs(boxes['#ask']['x'] - boxes['#sources']['x']) < 1
        assert abs(boxes['#sources']['x'] - boxes['#viewer']['x']) < 1
        # The stacked page scrolls as one document again (no fixed viewport
        # height) except for the content-sized tab panels, which keep their
        # internal scroller so a large corpus table cannot grow the document.
        assert page.evaluate('getComputedStyle(document.body).overflowY') == (
            'visible'
        )
        assert _overflow_y(page, '#ask .column-body') == 'visible'
        assert _overflow_y(page, '#sources .tab-panels') == 'auto'
        assert _overflow_y(page, '#viewer .column-body') == 'visible'
        expect(page.locator('#panel-runs')).to_be_visible()
        expect(page.locator('.ask-form input[name="q"]')).to_be_visible()
        assert page.locator('script').count() == 0
        _shot(page, env, 'ui-3col-mobile.png')
        _log(
            env,
            'layout',
            'stacked-below-1100px',
            url=page.url,
            columns={key: value for key, value in boxes.items()},
            screenshot='ui-3col-mobile.png',
        )
        browser.close()


@PLAYWRIGHT_SKIP
def test_ui_narrow_corpus_tab_stays_bounded(ui_qa) -> None:
    """At 1024x768 the corpus tab scrolls inside the panel, not the page.

    Regression: the narrow media query returned the tab panels to document
    flow, so a ~1400-row corpus table grew
    ``document.documentElement.scrollHeight`` to ~82k px. The panel must keep
    its internal scroller and the document must stay viewport-relative.
    """
    from playwright.sync_api import expect, sync_playwright

    env = ui_qa
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(viewport={'width': 1024, 'height': 768})
        page = context.new_page()
        response = page.goto(
            env.proxy_origin + '/ui/', wait_until='domcontentloaded'
        )
        assert response is not None and response.status == 200

        # Pure CSS tab switch: clicking the label checks the radio.
        page.locator('label[for="tab-corpus"]').click()
        assert page.locator('#tab-corpus').is_checked()
        expect(page.locator('#panel-corpus')).to_be_visible()

        # The QA seed carries one source, so clone its row up to the live
        # corpus's order of magnitude to make the panel content overflow the
        # 65vh cap. This is test instrumentation in the browser, not app
        # markup: the page itself stays script-free.
        row_count = page.evaluate(
            """count => {
                const body = document.querySelector(
                    '#panel-corpus table'
                ).tBodies[0];
                const sample = body.rows[0];
                for (let i = 0; i < count; i += 1) {
                    body.appendChild(sample.cloneNode(true));
                }
                return body.rows.length;
            }""",
            1400,
        )
        assert row_count >= 1400, row_count

        viewport_height = page.evaluate('window.innerHeight')
        document_height = page.evaluate(
            'document.documentElement.scrollHeight'
        )
        assert document_height <= viewport_height * 3, (
            f'document grew to {document_height}px at a {viewport_height}px '
            'viewport; the corpus table must scroll inside .tab-panels'
        )
        assert _overflow_y(page, '#sources .tab-panels') == 'auto'
        panel = page.evaluate(
            """() => {
                const element = document.querySelector(
                    '#sources .tab-panels'
                );
                return {
                    clientHeight: element.clientHeight,
                    scrollHeight: element.scrollHeight,
                };
            }"""
        )
        assert panel['scrollHeight'] > panel['clientHeight'], panel
        assert panel['clientHeight'] <= 0.65 * viewport_height + 2, panel
        assert page.locator('script').count() == 0
        assert page.locator('[style]').count() == 0
        _shot(page, env, 'ui-3col-narrow-corpus.png')
        _log(
            env,
            'layout',
            'narrow-corpus-bounded',
            url=page.url,
            rows=row_count,
            document_height=document_height,
            viewport_height=viewport_height,
            panel=panel,
            screenshot='ui-3col-narrow-corpus.png',
        )
        browser.close()
