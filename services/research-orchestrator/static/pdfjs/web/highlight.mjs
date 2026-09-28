/**
 * Same-origin PDF highlight wrapper for the Glasslab corpus UI (#619).
 *
 * The page loads this module and the vendored pdf.js build from the same
 * origin; this module renders exactly one requested page to a canvas and
 * overlays translucent rectangles for the cited excerpt returned by the
 * same-origin boxes route. The URL query (source, page, excerpt) is the whole
 * state contract: no CDN, no external fetch, no persisted coordinates.
 */

import * as pdfjsLib from '../build/pdf.mjs';

pdfjsLib.GlobalWorkerOptions.workerSrc = '../build/pdf.worker.mjs';

const MIN_SCALE = 0.4;
const MAX_SCALE = 2;
const STAGE_GUTTER = 2; // keep the page edge clear of the stage border
const RESIZE_DEBOUNCE_MS = 120;

const parameters = new URLSearchParams(window.location.search);
const source = (parameters.get('source') ?? '').trim();
const excerpt = (parameters.get('excerpt') ?? '').trim();
const requestedPage = Number.parseInt(parameters.get('page') ?? '1', 10);
const pageNumber =
  Number.isFinite(requestedPage) && requestedPage > 0 ? requestedPage : 1;

const stage = document.getElementById('stage');
const metaLine = document.getElementById('meta');
const statusLine = document.getElementById('status');

const canvas = document.createElement('canvas');
canvas.setAttribute('role', 'img');
canvas.setAttribute('aria-label', `Page ${pageNumber} of the cited source`);

const overlay = document.createElement('div');
overlay.className = 'highlight-layer';
overlay.setAttribute('aria-hidden', 'true');

// The overlay is a child of `pageView.div`, directly above the canvas in the
// same pixel space: `pageView.div` is the positioning context for both.
const pageView = {
  div: document.getElementById('page'),
  canvas,
  overlay,
  viewport: null,
};
pageView.div.append(canvas, overlay);

const state = {
  page: null,
  boxes: [],
  section: null,
  renderTask: null,
  stageWidth: 0,
};

function setStatus(message, tone = 'info') {
  statusLine.textContent = message;
  statusLine.dataset.tone = tone;
}

/**
 * Accept the boxes route's JSON envelope ``{ boxes: [...], section: {...} }``
 * (box entries are four-number arrays or ``{ x0, y0, x1, y1 }`` objects) and
 * return the normalized rectangles beside the cited section. Corners are PDF
 * user-space coordinates (origin bottom-left), which is what the viewport
 * expects. A bare list or ``{ rects: [...] }`` envelope is still accepted and
 * has no section.
 */
function normalizeBoxes(payload) {
  const raw = Array.isArray(payload)
    ? payload
    : Array.isArray(payload?.boxes)
      ? payload.boxes
      : Array.isArray(payload?.rects)
        ? payload.rects
        : [];
  const boxes = raw.flatMap((entry) => {
    const corners =
      Array.isArray(entry) && entry.length >= 4
        ? [entry[0], entry[1], entry[2], entry[3]]
        : entry && typeof entry === 'object'
          ? [entry.x0, entry.y0, entry.x1, entry.y1]
          : null;
    if (!corners) {
      return [];
    }
    const [x0, y0, x1, y1] = corners.map(Number);
    if (![x0, y0, x1, y1].every(Number.isFinite)) {
      return [];
    }
    if (Math.abs(x1 - x0) < 0.5 || Math.abs(y1 - y0) < 0.5) {
      return [];
    }
    return [{ x0, y0, x1, y1 }];
  });
  const section =
    payload && !Array.isArray(payload) && payload.section
      ? payload.section
      : null;
  return { boxes, section };
}

async function fetchHighlightBoxes() {
  const query = new URLSearchParams({
    source,
    page: String(pageNumber),
  });
  if (excerpt) {
    query.set('excerpt', excerpt);
  }
  const response = await fetch(`/ui/pdf/boxes?${query}`, {
    headers: { Accept: 'application/json' },
    credentials: 'same-origin',
  });
  if (!response.ok) {
    throw new Error(`highlight lookup failed: ${response.status}`);
  }
  return normalizeBoxes(await response.json());
}

function renderMeta() {
  const title =
    state.section && typeof state.section.title === 'string'
      ? state.section.title.trim()
      : '';
  metaLine.textContent = title
    ? `source ${source} · page ${pageNumber} · ${title}`
    : `source ${source} · page ${pageNumber}`;
}

function fitViewport(stageWidth) {
  const unscaled = state.page.getViewport({ scale: 1 });
  const usable = Math.max(stageWidth - STAGE_GUTTER, 240);
  const scale = Math.min(MAX_SCALE, Math.max(MIN_SCALE, usable / unscaled.width));
  return state.page.getViewport({ scale });
}

function drawHighlights() {
  overlay.replaceChildren();
  const viewport = pageView.viewport;
  if (!viewport || state.boxes.length === 0) {
    return;
  }
  for (const box of state.boxes) {
    const [left0, top0] = viewport.convertToViewportPoint(box.x0, box.y0);
    const [left1, top1] = viewport.convertToViewportPoint(box.x1, box.y1);
    const left = Math.min(left0, left1);
    const top = Math.min(top0, top1);
    const width = Math.abs(left1 - left0);
    const height = Math.abs(top1 - top0);
    if (width < 0.5 || height < 0.5) {
      continue;
    }
    const rect = document.createElement('div');
    rect.className = 'highlight-rect';
    rect.style.left = `${left}px`;
    rect.style.top = `${top}px`;
    rect.style.width = `${width}px`;
    rect.style.height = `${height}px`;
    overlay.append(rect);
  }
  overlay.querySelector('.highlight-rect')?.scrollIntoView({ block: 'center' });
}

async function renderPage() {
  const page = state.page;
  if (!page) {
    return;
  }
  const viewport = fitViewport(stage.clientWidth);
  pageView.viewport = viewport;

  const outputScale = window.devicePixelRatio || 1;
  canvas.width = Math.floor(viewport.width * outputScale);
  canvas.height = Math.floor(viewport.height * outputScale);
  canvas.style.width = `${Math.floor(viewport.width)}px`;
  canvas.style.height = `${Math.floor(viewport.height)}px`;
  pageView.div.style.width = `${Math.floor(viewport.width)}px`;
  pageView.div.style.height = `${Math.floor(viewport.height)}px`;

  const transform =
    outputScale === 1 ? null : [outputScale, 0, 0, outputScale, 0, 0];

  state.renderTask?.cancel();
  const renderTask = page.render({
    canvas,
    canvasContext: canvas.getContext('2d', { alpha: false }),
    viewport,
    transform,
  });
  state.renderTask = renderTask;
  try {
    await renderTask.promise;
  } catch (error) {
    if (error?.name === 'RenderingCancelledException') {
      return;
    }
    throw error;
  } finally {
    if (state.renderTask === renderTask) {
      state.renderTask = null;
    }
  }
  drawHighlights();
}

function reportRenderFailure(error) {
  console.error(error);
  setStatus('The page could not be rendered.', 'error');
}

let resizeTimer = 0;
const resizeObserver = new ResizeObserver(() => {
  const width = Math.round(stage.clientWidth);
  if (width === state.stageWidth) {
    return;
  }
  state.stageWidth = width;
  window.clearTimeout(resizeTimer);
  resizeTimer = window.setTimeout(() => {
    renderPage().catch(reportRenderFailure);
  }, RESIZE_DEBOUNCE_MS);
});

async function start() {
  if (!source) {
    setStatus('Missing source in the viewer URL.', 'error');
    return;
  }
  renderMeta();
  setStatus('Loading the document…');
  let pdfDocument = null;
  try {
    pdfDocument = await pdfjsLib.getDocument({
      url: `/ui/pdf/document.pdf?${new URLSearchParams({ source })}`,
      cMapUrl: new URL('cmaps/', import.meta.url).href,
      cMapPacked: true,
      standardFontDataUrl: new URL('standard_fonts/', import.meta.url).href,
      wasmUrl: new URL('wasm/', import.meta.url).href,
      iccUrl: new URL('iccs/', import.meta.url).href,
      isEvalSupported: false,
    }).promise;
  } catch (error) {
    console.error(error);
    setStatus('The document could not be loaded.', 'error');
    return;
  }

  try {
    state.page = await pdfDocument.getPage(pageNumber);
  } catch (error) {
    console.error(error);
    setStatus(`Page ${pageNumber} is not in this document.`, 'error');
    return;
  }

  let highlightFailure = false;
  try {
    const highlight = await fetchHighlightBoxes();
    state.boxes = highlight.boxes;
    state.section = highlight.section;
  } catch (error) {
    console.error(error);
    state.boxes = [];
    state.section = null;
    highlightFailure = true;
  }

  renderMeta();
  state.stageWidth = Math.round(stage.clientWidth);
  resizeObserver.observe(stage);
  await renderPage();

  if (highlightFailure) {
    setStatus(
      `Page ${pageNumber} rendered; the highlight lookup failed.`,
      'warn',
    );
  } else if (state.boxes.length > 0) {
    const label = state.boxes.length === 1 ? 'highlight' : 'highlights';
    setStatus(
      `Page ${pageNumber}: ${state.boxes.length} ${label} for the cited excerpt.`,
      'ok',
    );
  } else if (excerpt) {
    setStatus(`Page ${pageNumber}: the cited excerpt did not match here.`);
  } else {
    setStatus(`Page ${pageNumber}.`);
  }
}

start().catch(reportRenderFailure);
