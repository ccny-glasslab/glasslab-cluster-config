"""Contract tests for the same-origin PDF highlight wrapper (issue #619).

The wrapper at ``static/pdfjs/web/highlight.html`` is a thin first-party page
around the vendored pdf.js build. These tests pin the three properties that
keep it thin, dark, and same-origin:

* the HTML loads its own module by URL only -- no inline script and no
  resource from an external origin;
* the module computes highlight geometry with the pdf.js v6 viewport API
  (``convertToViewportPoint``; ``convertToViewportRectangle`` was removed in
  v6) and bootstraps the vendored module worker and same-origin endpoints;
* ``theme.css`` defines the dark ``/ui`` token layer with no external font
  import and no remote URL.
"""

from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path

WEB_ROOT = Path(__file__).resolve().parents[1] / "static" / "pdfjs" / "web"

HIGHLIGHT_HTML = WEB_ROOT / "highlight.html"
HIGHLIGHT_MODULE = WEB_ROOT / "highlight.mjs"
THEME_CSS = WEB_ROOT / "theme.css"


def _read(path: Path) -> str:
    assert path.is_file(), f"missing highlight asset: {path}"
    return path.read_text(encoding="utf-8")


class _ScriptCollector(HTMLParser):
    """Collect every script tag, its body text, and its src/type attributes."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.scripts: list[dict[str, str]] = []
        self.script_bodies: list[str] = []
        self.stylesheet_hrefs: list[str] = []
        self._open: dict[str, str] | None = None
        self._body: list[str] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        attributes = {name: value or "" for name, value in attrs}
        if tag == "script":
            self._open = attributes
            self._body = []
        elif tag == "link":
            rel = attributes.get("rel", "")
            if "stylesheet" in rel.split():
                self.stylesheet_hrefs.append(attributes.get("href", ""))

    def handle_endtag(self, tag: str) -> None:
        if tag != "script" or self._open is None:
            return
        self.scripts.append(self._open)
        self.script_bodies.append("".join(self._body).strip())
        self._open = None
        self._body = []

    def handle_data(self, data: str) -> None:
        if self._open is not None:
            self._body.append(data)


def test_highlight_html_has_no_inline_script_no_external_origin() -> None:
    text = _read(HIGHLIGHT_HTML)
    parser = _ScriptCollector()
    parser.feed(text)
    parser.close()

    assert "http://" not in text and "https://" not in text, (
        "highlight.html must not reference an external origin"
    )
    assert "".join(parser.script_bodies) == "", (
        f"highlight.html must not carry inline script: {parser.script_bodies}"
    )
    assert parser.scripts, "highlight.html must load its module scripts"
    assert all(script.get("src") for script in parser.scripts), (
        f"every script needs a src URL: {parser.scripts}"
    )
    loaded = {(script.get("src", ""), script.get("type", ""))
              for script in parser.scripts}
    assert ("highlight.mjs", "module") in loaded, (
        f"highlight.mjs must load as a module: {sorted(loaded)}"
    )
    assert ("../build/pdf.mjs", "module") in loaded, (
        f"the vendored pdf.js module must load first: {sorted(loaded)}"
    )
    assert "theme.css" in parser.stylesheet_hrefs, (
        f"theme.css must be linked: {parser.stylesheet_hrefs}"
    )


def test_highlight_uses_convert_to_viewport_point() -> None:
    source = _read(HIGHLIGHT_MODULE)
    assert "convertToViewportPoint" in source, (
        "pdf.js v6 highlight geometry uses viewport.convertToViewportPoint"
    )
    assert "convertToViewportRectangle" not in source, (
        "convertToViewportRectangle was removed in pdf.js v6; do not use it"
    )


def test_highlight_bootstraps_vendored_pdfjs_same_origin() -> None:
    source = _read(HIGHLIGHT_MODULE)
    assert "http://" not in source and "https://" not in source, (
        "highlight.mjs must not reference an external origin"
    )
    assert "GlobalWorkerOptions.workerSrc" in source, (
        "the vendored pdf.js module worker must be configured"
    )
    assert "'../build/pdf.worker.mjs'" in source or (
        '"../build/pdf.worker.mjs"' in source
    ), "the worker must load from the vendored ../build tree"
    assert "'../build/pdf.mjs'" in source or '"../build/pdf.mjs"' in source, (
        "the vendored pdf.js build must be imported by relative path"
    )
    assert "/ui/pdf/document.pdf" in source, (
        "the PDF must be fetched from the same-origin document endpoint"
    )
    assert "/ui/pdf/boxes" in source, (
        "highlight boxes must come from the same-origin boxes endpoint"
    )


def test_highlight_theme_is_dark_tokens() -> None:
    css = _read(THEME_CSS)
    compact = "".join(css.split())

    assert "color-scheme:dark" in compact, "theme.css must opt into dark"
    assert "--bg:#08090a" in compact, "theme.css must keep the /ui canvas token"
    assert "--surface:#0e0f11" in compact, (
        "theme.css must keep the /ui surface token"
    )
    assert "--accent:#8b93ff" in compact, (
        "theme.css must keep the /ui indigo accent token"
    )
    assert "--hl-fill:" in compact, (
        "theme.css must define a highlight fill token"
    )
    assert "http://" not in css and "https://" not in css, (
        "theme.css must not reference an external origin"
    )
    assert "@import" not in css, "theme.css must not import external CSS"
    assert "@font-face" not in css.lower(), (
        "theme.css must use the system font stack, not an external font"
    )
    assert "url(" not in css.lower(), (
        "theme.css must not load any external URL"
    )
