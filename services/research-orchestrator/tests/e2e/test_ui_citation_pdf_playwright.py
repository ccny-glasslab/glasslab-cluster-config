"""Real-browser e2e lock for the ``/ui`` citation -> cited PDF page flow (#686).

Drives headless Chromium through the loopback operator-token-injecting UI
proxies against a live uvicorn ``app.main:app`` (the ``ui_qa`` harness). The
corpus is the synthetic two-page PDF seeded by ``scripts/qa/seed_ui_corpus.py``,
so no external source is needed. Nothing in the app is mocked; the only fake
is the loopback OpenAI-compatible synthesis endpoint ``ui_qa`` starts, which
returns scripted answers for questions carrying
``ui_qa.FUZZY_QUESTION_MARKER``/``ui_qa.UNVERIFIED_QUESTION_MARKER`` and an
HTTP 503 (extractive fallback) for every other question.

Assertions:

1. a corpus question POSTed through the composer renders an answer, and every
   citation marker in it names the seeded source, page, and excerpt; for each
   one the viewer origin serves ``GET /ui/pdf/document.pdf?source=`` as 200
   ``application/pdf`` with the seeded bytes and
   ``GET /ui/pdf/boxes?source=&page=&excerpt=`` as 200 with >=1 box;
2. following the citation renders the cited page in the viewer iframe with at
   least one highlight rectangle;
3. the verdict badge renders ``✓ exact`` (extractive answer), ``≈ fuzzy``, and
   ``✗ unverified`` (scripted claims the server classifies itself), and each
   citation still resolves and highlights;
4. the #595 streaming composer streams the answer in place and the ``done``
   frame's redirect renders the persisted, cited turn;
5. the page CSP is nonce-only for scripts (``script-src 'nonce-…'``) with
   ``connect-src 'self'``, the single inline script carries that nonce, and it
   appends streamed text with ``createTextNode`` (never ``innerHTML``).

Run with the Playwright venv, for example::

    GLASSLAB_QA_APP_PYTHON=$(command -v python3) \\
        /home/gr66ss/venvs/playwright/bin/python -m pytest \\
        tests/e2e/test_ui_citation_pdf_playwright.py -q

Screenshots and the action log are written to ``ui_qa.ARTIFACTS_DIR``.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlsplit

import pytest

if TYPE_CHECKING:
    from playwright.sync_api import Frame, Page

# The browser stack is installed only in the Playwright venv; skip by marker so
# the module still collects in the default service test environment.
PLAYWRIGHT_INSTALLED = importlib.util.find_spec('playwright') is not None
PLAYWRIGHT_SKIP = pytest.mark.skipif(
    not PLAYWRIGHT_INSTALLED,
    reason='the playwright package is installed only in the Playwright venv',
)

# Importing the fixture registers it in this module's namespace (see
# test_ui_corpus_playwright.py for why it is not a second conftest.py).
from ui_qa import (  # noqa: E402
    FUZZY_QUESTION_MARKER,
    UNVERIFIED_QUESTION_MARKER,
    assert_only_streaming_script,
    submit_native,
    ui_qa,  # noqa: F401
)

IFRAME_TITLE = 'Cited source PDF'
HIGHLIGHT_PATH = '/ui/pdf/assets/web/highlight.html'
EXACT_BADGE = '✓ exact'
FUZZY_BADGE = '≈ fuzzy'
UNVERIFIED_BADGE = '✗ unverified'


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


def _ask(page: Page, env, question: str):
    """POST ``question`` through the composer; return the latest turn."""
    from playwright.sync_api import expect

    response = page.goto(env.proxy_origin + '/ui/', wait_until='domcontentloaded')
    assert response is not None and response.status == 200
    form = page.locator('#ask form.ask-form')
    form.locator('input[name="q"]').fill(question)
    submit_native(page, form)
    assert 'c=' in page.url, f'conversation missing from URL: {page.url}'
    turn = page.locator('.chat-turn').last
    expect(turn).to_contain_text(question)
    return turn


def _assert_citation_resolves(context, env, marker) -> dict[str, str]:
    """A citation marker names the seeded source/page and the viewer serves it."""
    manifest = env.manifest
    href = marker.get_attribute('href')
    assert href, 'citation marker has no href'
    query = {key: values[0] for key, values in parse_qs(urlsplit(href).query).items()}
    assert query.get('source') == manifest['source_id'], query
    assert query.get('page') == str(manifest['page']), query
    assert query.get('excerpt') == manifest['excerpt'], query
    document = context.request.get(
        env.viewer_origin + '/ui/pdf/document.pdf',
        params={'source': query['source']},
    )
    assert document.status == 200, f'document.pdf status {document.status}'
    content_type = document.headers.get('content-type', '')
    assert content_type.startswith('application/pdf'), content_type
    body = document.body()
    assert body.startswith(b'%PDF'), body[:16]
    assert hashlib.sha256(body).hexdigest() == manifest['pdf_sha256'], (
        'the viewer served different bytes than the seeded source PDF'
    )
    boxes = context.request.get(
        env.viewer_origin + '/ui/pdf/boxes',
        params={
            'source': query['source'],
            'page': query['page'],
            'excerpt': query['excerpt'],
        },
    )
    assert boxes.status == 200, f'boxes status {boxes.status}'
    assert boxes.json()['boxes'], boxes.json()
    return {
        'source': query['source'],
        'page': query['page'],
        'excerpt': query['excerpt'],
        'boxes': len(boxes.json()['boxes']),
    }


def _assert_viewer_highlights(page: Page, env, marker) -> Frame:
    """Following the marker renders the cited page with a highlight rect."""
    from playwright.sync_api import expect

    expected_page = str(env.manifest['page'])
    with page.expect_navigation(wait_until='domcontentloaded'):
        marker.click()
    iframe = page.locator(f'iframe[title="{IFRAME_TITLE}"]')
    expect(iframe).to_be_visible()
    iframe.scroll_into_view_if_needed()
    frame = _wait_for_frame(page, env.viewer_origin + HIGHLIGHT_PATH)
    canvas = frame.locator('#page canvas')
    canvas.wait_for(state='visible', timeout=30_000)
    expect(canvas).to_have_attribute(
        'aria-label', f'Page {expected_page} of the cited source'
    )
    expect(frame.locator('#status')).to_have_attribute(
        'data-tone', 'ok', timeout=30_000
    )
    rects = frame.locator('#page .highlight-layer .highlight-rect')
    assert rects.count() >= 1, 'no highlight rectangle for the excerpt'
    return frame


def _verdict_cases():
    return [
        pytest.param(None, EXACT_BADGE, 'exact', id='exact'),
        pytest.param(FUZZY_QUESTION_MARKER, FUZZY_BADGE, 'fuzzy', id='fuzzy'),
        pytest.param(
            UNVERIFIED_QUESTION_MARKER, UNVERIFIED_BADGE, 'none', id='unverified'
        ),
    ]


@PLAYWRIGHT_SKIP
@pytest.mark.parametrize(('marker_word', 'badge', 'verdict'), _verdict_cases())
def test_chat_citation_opens_cited_pdf_page(
    ui_qa, marker_word, badge, verdict
) -> None:
    """Chat -> citation -> viewer, for each verdict badge.

    ``exact`` is the extractive answer; ``fuzzy``/``unverified`` are scripted
    claims from the fake synthesis endpoint that the server classifies, so a
    regression in the classifier or the badge rendering fails here.
    """
    from playwright.sync_api import expect, sync_playwright

    env = ui_qa
    question = env.manifest['question']
    if marker_word is not None:
        question = f'{question} {marker_word}'
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(viewport={'width': 1440, 'height': 1000})
        page = context.new_page()
        page_errors: list[str] = []
        page.on('pageerror', lambda error: page_errors.append(str(error)))

        turn = _ask(page, env, question)
        markers = turn.locator('a.cite')
        count = markers.count()
        assert count >= 1, 'expected at least one inline citation'
        if marker_word is not None:
            assert count == 1, (
                'the scripted answer must render exactly one citation; the '
                'LLM lane may have fallen back to the extractive answer'
            )
        resolved = [
            _assert_citation_resolves(context, env, markers.nth(index))
            for index in range(count)
        ]

        marker = markers.first
        badge_element = marker.locator('.cite-badge')
        expect(badge_element).to_have_text(badge)
        expect(badge_element).to_have_class(re.compile(rf'\bbadge-{verdict}\b'))
        marker.hover()
        expect(marker.locator('.cite-card')).to_be_visible()
        screenshot = f'citation-pdf-{verdict}.png'
        _shot(page, env, screenshot)

        frame = _assert_viewer_highlights(page, env, marker)
        _log(
            env,
            'citation-pdf',
            'resolved-and-highlighted',
            verdict=verdict,
            badge=badge,
            citations=resolved,
            frame_url=frame.url,
            screenshot=screenshot,
        )
        assert page_errors == [], f'uncaught page errors: {page_errors}'
        browser.close()


@PLAYWRIGHT_SKIP
def test_streaming_composer_persists_the_cited_turn(ui_qa) -> None:
    """The #595 composer streams the answer, then the turn renders cited."""
    from playwright.sync_api import expect, sync_playwright

    env = ui_qa
    manifest = env.manifest
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(viewport={'width': 1440, 'height': 1000})
        page = context.new_page()
        page_errors: list[str] = []
        page.on('pageerror', lambda error: page_errors.append(str(error)))
        page.goto(env.proxy_origin + '/ui/', wait_until='domcontentloaded')
        ask = page.locator('#ask')
        ask.locator('input[name="q"]').fill(manifest['question'])
        with page.expect_response(
            lambda response: '/ui/chat/stream' in response.url
        ) as stream_info:
            ask.locator('button[type="submit"]').click()
        stream = stream_info.value
        assert stream.status == 200, f'stream status {stream.status}'
        assert stream.headers.get('content-type', '').startswith(
            'text/event-stream'
        ), stream.headers
        # The script streamed in place: no navigation, live pane filled.
        expect(page.locator('#chat-live-status')).to_have_text(
            'Answer complete.', timeout=30_000
        )
        assert page.url == env.proxy_origin + '/ui/', page.url
        expect(page.locator('#chat-live-question')).to_have_text(
            manifest['question']
        )
        expect(page.locator('#chat-live-answer')).to_contain_text(
            manifest['excerpt']
        )
        _shot(page, env, 'citation-pdf-streamed.png')

        # The stream contract a client follows to the persisted turn: the
        # ``done`` frame names the conversation and its rendered page.
        frames = context.request.get(
            env.proxy_origin + '/ui/chat/stream',
            params={'q': manifest['question']},
            headers={'Accept': 'text/event-stream'},
        )
        assert frames.status == 200, frames.status
        done = None
        for block in frames.text().split('\n\n'):
            lines = block.splitlines()
            if lines and lines[0] == 'event: done':
                done = json.loads(lines[1].removeprefix('data:').strip())
        assert done and done['conversation_id'] and done['redirect'], done
        page.goto(env.proxy_origin + done['redirect'], wait_until='domcontentloaded')
        turn = page.locator('.chat-turn').last
        expect(turn).to_contain_text(manifest['question'])
        markers = turn.locator('a.cite')
        assert markers.count() >= 1, 'the persisted turn lost its citation'
        resolved = _assert_citation_resolves(context, env, markers.first)
        _log(
            env,
            'citation-pdf',
            'streamed-and-persisted',
            stream_url=stream.url,
            conversation_id=done['conversation_id'],
            citation=resolved,
            screenshot='citation-pdf-streamed.png',
        )
        assert page_errors == [], f'uncaught page errors: {page_errors}'
        browser.close()


@PLAYWRIGHT_SKIP
def test_page_csp_and_streaming_script_are_nonce_gated(ui_qa) -> None:
    """Cheap guard: nonce-only scripts, same-origin connect, text-node writes."""
    from playwright.sync_api import sync_playwright

    env = ui_qa
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_context().new_page()
        response = page.goto(env.proxy_origin + '/ui/', wait_until='domcontentloaded')
        assert response is not None and response.status == 200
        csp = response.headers.get('content-security-policy', '')
        directives = {
            part.split()[0]: part.split()[1:]
            for part in (piece.strip() for piece in csp.split(';'))
            if part
        }
        script_src = directives.get('script-src', [])
        assert len(script_src) == 1, f'script-src must be nonce-only: {csp}'
        nonce_match = re.fullmatch(r"'nonce-([A-Za-z0-9_-]+)'", script_src[0])
        assert nonce_match, f'script-src is not a nonce source: {csp}'
        assert directives.get('connect-src') == ["'self'"], csp
        for forbidden in ("'unsafe-inline'", "'unsafe-eval'"):
            assert forbidden not in csp, csp

        # The DOM hides ``nonce`` from attribute reads, so check the served
        # markup for the one script tag and its nonce.
        html = response.text()
        tags = re.findall(r'<script\b[^>]*>', html)
        assert tags == [f'<script nonce="{nonce_match.group(1)}">'], tags
        assert_only_streaming_script(page)
        source = page.locator('script').first.inner_text()
        assert 'createTextNode' in source, 'streamed text must use createTextNode'
        for sink in ('innerHTML', 'outerHTML', 'insertAdjacentHTML'):
            assert sink not in source, f'the streaming script uses {sink}'
        _log(
            env,
            'citation-pdf',
            'csp-checked',
            script_src=script_src,
            connect_src=directives.get('connect-src'),
        )
        browser.close()
