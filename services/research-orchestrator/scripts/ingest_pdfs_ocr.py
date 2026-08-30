#!/usr/bin/env python3
"""Extract text from a folder of PDFs, with OCR fallback for scans.

Born-digital PDFs extract exactly (pymupdf). Scanned or image-only PDFs fall
back to Tesseract OCR when ``--ocr`` is passed and the ``tesseract`` binary
is installed (~1-3 s per page on CPU; a 500-page book is tens of minutes).

Output: one UTF-8 ``.txt`` sidecar per PDF under ``--out``, plus a
``manifest.json`` recording per-file status so re-runs skip completed books.
Push the resulting folder into the live corpus with the existing uploader:

    python services/research-orchestrator/scripts/upload_knowledge_dir.py \
        --url http://127.0.0.1:18080 --dir <out-dir> \
        --source-type documentation

The server re-applies its fail-closed secret scanning to the extracted text.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

_SERVICE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_SERVICE_DIR))

from app.corpus_rag.ocr_backend import OCR_EXTRACTION_VERSION  # noqa: E402
from app.corpus_rag.pdf_backend import UnsupportedDocumentError  # noqa: E402
from app.corpus_rag.pdf_backend import PyMuPdfBackend  # noqa: E402


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dir', required=True, help='folder of source PDFs')
    parser.add_argument(
        '--out', required=True, help='folder to write extracted .txt files'
    )
    parser.add_argument(
        '--ocr',
        action='store_true',
        help='OCR scanned PDFs via tesseract (must be installed)',
    )
    parser.add_argument(
        '--dpi',
        type=int,
        default=200,
        help='rasterization DPI for OCR (default: %(default)s)',
    )
    parser.add_argument(
        '--force',
        action='store_true',
        help='re-process files already recorded in manifest.json',
    )
    args = parser.parse_args(argv)

    root = Path(args.dir).expanduser().resolve()
    out = Path(args.out).expanduser().resolve()
    if not root.is_dir():
        parser.error(f'not a directory: {root}')
    out.mkdir(parents=True, exist_ok=True)

    from app.corpus_rag.ocr_backend import TesseractOcrBackend

    if args.ocr and not TesseractOcrBackend.available():
        parser.error(
            '--ocr requested but tesseract is not installed '
            '(apt install tesseract-ocr)'
        )

    manifest_path = out / 'manifest.json'
    manifest: dict[str, dict[str, object]] = {}
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())

    pdfs = sorted(root.rglob('*.pdf'))
    if not pdfs:
        print(f'no PDFs under {root}')
        return 0

    failures = 0
    for pdf_path in pdfs:
        digest = _sha256_of(pdf_path)
        record = manifest.get(str(pdf_path))
        if (
            not args.force
            and record is not None
            and record.get('sha256') == digest
            and record.get('status') == 'ok'
        ):
            print(f'[skip] {pdf_path.name} (already extracted)')
            continue

        data = pdf_path.read_bytes()
        started = time.monotonic()
        try:
            document, extraction_version = _extract(data, args)
        except UnsupportedDocumentError as exc:
            failures += 1
            print(f'[fail] {pdf_path.name}: {exc}')
            manifest[str(pdf_path)] = {
                'sha256': digest,
                'status': 'failed',
                'error': str(exc),
            }
            continue
        except Exception as exc:  # noqa: BLE001 - per-file isolation
            failures += 1
            print(f'[fail] {pdf_path.name}: {type(exc).__name__}: {exc}')
            manifest[str(pdf_path)] = {
                'sha256': digest,
                'status': 'failed',
                'error': f'{type(exc).__name__}: {exc}',
            }
            continue

        out_path = out / (pdf_path.stem + '.txt')
        out_path.write_text(document.text, encoding='utf-8')
        elapsed = time.monotonic() - started
        print(
            f'[ok]   {pdf_path.name}: {len(document.pages)} page(s), '
            f'{len(document.text)} chars via {extraction_version} '
            f'({elapsed:.1f}s)'
        )
        manifest[str(pdf_path)] = {
            'sha256': digest,
            'status': 'ok',
            'extraction_version': extraction_version,
            'pages': len(document.pages),
            'chars': len(document.text),
            'out': out_path.name,
        }

    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True))
    print(
        f'done: {len(pdfs)} file(s), {failures} failed — '
        f'text sidecars in {out}; push them with upload_knowledge_dir.py'
    )
    return 1 if failures else 0


def _extract(data: bytes, args: argparse.Namespace):
    if getattr(args, 'ocr', False):
        from app.corpus_rag.ocr_backend import extract_pdf_with_fallback

        return extract_pdf_with_fallback(data, allow_ocr=True, dpi=args.dpi)
    return PyMuPdfBackend().extract(data), 'pymupdf-v1'


if __name__ == '__main__':
    raise SystemExit(main())
