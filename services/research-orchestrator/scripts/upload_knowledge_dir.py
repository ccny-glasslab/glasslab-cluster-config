#!/usr/bin/env python3
"""Upload a local folder of PDFs/markdown/text into the orchestrator corpus.

Point it at a directory on your laptop; every *.pdf, *.md, and *.txt file is
POSTed to the operator-only ``/knowledge/sources/upload`` endpoint, then the
dense index is rebuilt so Honeydew/Beaker can retrieve it:

    python services/research-orchestrator/scripts/upload_knowledge_dir.py \
        --url http://127.0.0.1:18080 \
        --dir ~/Documents/methods-pdfs \
        --source-type documentation

Auth: pass --token or export GLASSLAB_OPERATOR_TOKEN when the service
enables require_operator_auth. Re-uploading an unchanged file deduplicates
on the server (same content digest); changed files become new sources.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

UPLOAD_EXTENSIONS = {'.pdf', '.md', '.txt'}

try:
    import httpx
except ImportError:  # pragma: no cover - guidance for workstation use
    print(
        'upload_knowledge_dir requires httpx: pip install httpx',
        file=sys.stderr,
    )
    raise SystemExit(2)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--url',
        default=os.environ.get('GLASSLAB_ORCHESTRATOR_URL', 'http://127.0.0.1:18080'),
        help='orchestrator base URL (default: %(default)s)',
    )
    parser.add_argument('--dir', required=True, help='folder of documents')
    parser.add_argument(
        '--source-type',
        default='documentation',
        help='SourceType label applied to every file (default: %(default)s)',
    )
    parser.add_argument(
        '--token',
        default=os.environ.get('GLASSLAB_OPERATOR_TOKEN', ''),
        help='operator API token (or GLASSLAB_OPERATOR_TOKEN env)',
    )
    parser.add_argument(
        '--skip-rebuild',
        action='store_true',
        help='skip the dense-index rebuild after uploading',
    )
    args = parser.parse_args(argv)

    root = Path(args.dir).expanduser().resolve()
    if not root.is_dir():
        parser.error(f'not a directory: {root}')

    files = sorted(
        path
        for path in root.rglob('*')
        if path.is_file() and path.suffix.lower() in UPLOAD_EXTENSIONS
    )
    if not files:
        print(f'no {sorted(UPLOAD_EXTENSIONS)} files under {root}')
        return 0

    headers = {}
    if args.token:
        headers['X-Glasslab-Operator-Token'] = args.token

    uploaded = failed = 0
    with httpx.Client(base_url=args.url, headers=headers, timeout=300) as client:
        for path in files:
            payload = path.read_bytes()
            response = client.post(
                '/knowledge/sources/upload',
                files={'file': (path.name, payload)},
                data={
                    'source_type': args.source_type,
                    'title': path.stem,
                },
            )
            if response.status_code == 201:
                uploaded += 1
                body = response.json()
                print(f'[ok]   {path.name} -> {body["source_id"]}')
                continue
            failed += 1
            detail = response.text[:200]
            print(f'[fail] {path.name} ({response.status_code}): {detail}')

        if failed == 0 and not args.skip_rebuild:
            rebuild = client.post('/knowledge/index/rebuild')
            if rebuild.status_code == 200:
                print(
                    '[ok]   dense index rebuilt: '
                    f"{rebuild.json().get('reindexed_sources')} source(s)"
                )
            else:
                failed += 1
                print(
                    f'[fail] index rebuild ({rebuild.status_code}): '
                    f'{rebuild.text[:200]}'
                )

    print(f'done: {uploaded} uploaded, {failed} failed')
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
