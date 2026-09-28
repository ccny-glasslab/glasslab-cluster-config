"""Guard the vendored pdf.js viewer assets shipped in the orchestrator image.

The corpus UI serves a prebuilt generic pdf.js distribution from the same
origin as the orchestrator, so no browser ever needs a CDN at runtime. These
tests pin the three properties that make that promise true:

* the required runtime files (viewer, worker, sandbox, locale, cmaps,
  standard fonts, wasm, iccs) exist in ``static/pdfjs``;
* ``static/pdfjs/VENDOR.json`` records a sha256 for every vendored byte and
  its values still match the tree;
* ``web/viewer.html`` loads no subresource from an external origin.
"""

from __future__ import annotations

import hashlib
import json
from html.parser import HTMLParser
from pathlib import Path

PDFJS_ROOT = Path(__file__).resolve().parents[1] / "static" / "pdfjs"
VENDOR_MANIFEST_PATH = PDFJS_ROOT / "VENDOR.json"

PDFJS_VERSION = "6.3.289"
PDFJS_SOURCE_URL = (
    "https://github.com/mozilla/pdf.js/releases/download/"
    "v6.3.289/pdfjs-6.3.289-dist.zip"
)

REQUIRED_FILES = (
    "build/pdf.mjs",
    "build/pdf.worker.mjs",
    "build/pdf.sandbox.mjs",
    "web/viewer.html",
    "web/viewer.mjs",
    "web/viewer.css",
    "web/locale/locale.json",
    "LICENSE",
)

REQUIRED_DIRECTORIES = (
    "web/locale/en-US",
    "web/images",
    "web/cmaps",
    "web/standard_fonts",
    "web/wasm",
    "web/iccs",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _vendored_files() -> list[Path]:
    return sorted(
        path
        for path in PDFJS_ROOT.rglob("*")
        if path.is_file() and path != VENDOR_MANIFEST_PATH
    )


class _ExternalAssetParser(HTMLParser):
    """Collect subresource URLs fetched from an external origin.

    Only attributes the browser loads as a subresource are inspected:
    ``src``/``poster``/``data``/``srcset`` on any element, ``<link href>``,
    ``<base href>``, and CSS ``url(...)`` in ``<style>`` blocks. Navigation
    ``<a href>`` links and the SVG ``xmlns`` namespace URI are not asset
    loads and are intentionally ignored.
    """

    _FETCH_ATTRIBUTES = ("src", "poster", "data", "srcset", "imagesrcset")

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.external_urls: list[str] = []
        self._in_style = False

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        attributes = {name: value or "" for name, value in attrs}
        href = attributes.get("href", "")
        if tag in {"link", "base"} and href.startswith(("http://", "https://")):
            self.external_urls.append(f"<{tag} href={href!r}>")
        for name in self._FETCH_ATTRIBUTES:
            value = attributes.get(name, "")
            if "http://" in value or "https://" in value:
                self.external_urls.append(f"<{tag} {name}={value!r}>")
        if tag == "style":
            self._in_style = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "style":
            self._in_style = False

    def handle_data(self, data: str) -> None:
        if self._in_style and ("http://" in data or "https://" in data):
            self.external_urls.append(f"<style> {data.strip()[:120]!r}")


def test_required_viewer_files_present() -> None:
    missing_files = [
        name for name in REQUIRED_FILES if not (PDFJS_ROOT / name).is_file()
    ]
    assert missing_files == [], f"missing pdf.js files: {missing_files}"
    empty_files = [
        name
        for name in REQUIRED_FILES
        if (PDFJS_ROOT / name).stat().st_size == 0
    ]
    assert empty_files == [], f"empty pdf.js files: {empty_files}"

    missing_directories = [
        name for name in REQUIRED_DIRECTORIES if not (PDFJS_ROOT / name).is_dir()
    ]
    assert missing_directories == [], (
        f"missing pdf.js directories: {missing_directories}"
    )
    empty_directories = [
        name
        for name in REQUIRED_DIRECTORIES
        if not any((PDFJS_ROOT / name).iterdir())
    ]
    assert empty_directories == [], (
        f"empty pdf.js directories: {empty_directories}"
    )


def test_vendor_manifest_hashes_match_tree() -> None:
    manifest = json.loads(VENDOR_MANIFEST_PATH.read_text(encoding="utf-8"))

    assert manifest["version"] == PDFJS_VERSION
    assert manifest["source_url"] == PDFJS_SOURCE_URL

    recorded: dict[str, str] = manifest["sha256"]
    on_disk = {
        path.relative_to(PDFJS_ROOT).as_posix(): path
        for path in _vendored_files()
    }
    assert set(recorded) == set(on_disk), (
        "VENDOR.json does not cover the vendored tree: "
        f"unrecorded={sorted(set(on_disk) - set(recorded))} "
        f"phantom={sorted(set(recorded) - set(on_disk))}"
    )

    mismatched = {
        name: _sha256(path)
        for name, path in on_disk.items()
        if _sha256(path) != recorded[name]
    }
    assert mismatched == {}, f"sha256 mismatch: {mismatched}"
    assert manifest["total_bytes"] == sum(
        path.stat().st_size for path in on_disk.values()
    )


def test_no_external_origin_in_viewer_html() -> None:
    parser = _ExternalAssetParser()
    parser.feed((PDFJS_ROOT / "web" / "viewer.html").read_text(encoding="utf-8"))
    parser.close()

    assert parser.external_urls == [], (
        "viewer.html loads resources from an external origin: "
        f"{parser.external_urls}"
    )
