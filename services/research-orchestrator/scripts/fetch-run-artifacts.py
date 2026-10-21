#!/usr/bin/env python3
"""Fetch a run's human-facing results through the read API (issue #585).

Downloads, digest-verifies, and writes a completed run's report.md, report
bundle (PDF/DOCX), every authoritative comparison JSON, and protocol/program.md;
``--include-jobs`` adds per-job evaluation.json, metrics.json, and reports/.
Every file is checked against the SHA-256 in the run's ArtifactRecord before
it is written, and the command fails closed (exit 1) on any mismatch, any
selected artifact the manifest reports unavailable, or a missing report or
protocol. Nothing is written for a file that fails verification.

Workstation usage, with the orchestrator port-forwarded as documented in
docs/research-orchestrator.md:

    export GLASSLAB_OPERATOR_TOKEN=...   # or --token-file PATH
    python services/research-orchestrator/scripts/fetch-run-artifacts.py \\
        <run_id> --output ./run-<run_id> [--include-jobs]

Standard library only, so it runs without the service's dependencies.
"""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import sys
from typing import Any, Callable
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_BASE_URL = 'http://127.0.0.1:18080'
TOKEN_HEADER = 'X-Glasslab-Operator-Token'
TOKEN_ENV = 'GLASSLAB_OPERATOR_TOKEN'

# Run-level artifact types that make up the reviewable result. Of each
# single-valued type only the latest record is fetched; every distinct
# comparison is fetched because a run may adjudicate several.
REQUIRED_RUN_TYPES = ('report', 'protocol')
LATEST_RUN_TYPES = ('report', 'report.pdf', 'report.docx', 'protocol')
ALL_RUN_TYPES = ('comparison',)
JOB_EVIDENCE_FILES = {'evaluation.json', 'metrics.json'}

Fetcher = Callable[[str], bytes]


class FetchError(Exception):
    """A fail-closed condition: nothing more should be trusted or written."""


def _job_evidence(entry: dict[str, Any]) -> bool:
    for value in (entry.get('type') or '', entry.get('relative_path') or ''):
        path = PurePosixPath(value)
        if path.name in JOB_EVIDENCE_FILES or 'reports' in path.parts:
            return True
    return False


def select_artifacts(
    manifest: dict[str, Any], *, include_jobs: bool
) -> list[dict[str, Any]]:
    """Pick the entries to fetch; raise if a required result is absent."""
    entries = list(manifest.get('artifacts') or [])
    latest: dict[str, dict[str, Any]] = {}
    selected: list[dict[str, Any]] = []
    seen_uris: set[str] = set()
    for entry in sorted(entries, key=lambda item: item.get('created_at') or ''):
        if entry.get('job_id'):
            continue
        if entry.get('type') in LATEST_RUN_TYPES:
            latest[entry['type']] = entry
    for entry in entries:
        uri = entry.get('uri')
        if entry.get('job_id'):
            wanted = include_jobs and _job_evidence(entry)
        else:
            wanted = entry.get('type') in ALL_RUN_TYPES
        if wanted and uri not in seen_uris:
            seen_uris.add(uri)
            selected.append(entry)
    missing = [kind for kind in REQUIRED_RUN_TYPES if kind not in latest]
    if missing:
        raise FetchError(
            'run has no recorded ' + ', '.join(missing) + ' artifact'
        )
    return [*latest.values(), *selected]


def _safe_relative(entry: dict[str, Any]) -> PurePosixPath:
    value = entry.get('relative_path')
    if not isinstance(value, str) or not value:
        raise FetchError(f'no relative path for {entry.get("uri")}')
    path = PurePosixPath(value)
    if path.is_absolute() or '..' in path.parts:
        raise FetchError(f'unsafe artifact path from server: {value!r}')
    return path


def fetch_run(
    run_id: str,
    *,
    output: Path,
    include_jobs: bool,
    fetch: Fetcher,
) -> list[Path]:
    quoted_run = urllib.parse.quote(run_id, safe='')
    manifest = json.loads(fetch(f'/runs/{quoted_run}/artifacts/manifest'))
    selected = select_artifacts(manifest, include_jobs=include_jobs)
    unavailable = [entry for entry in selected if not entry.get('available')]
    if unavailable:
        raise FetchError(
            'unavailable artifacts: '
            + '; '.join(
                f'{entry.get("uri")} ({entry.get("reason")})'
                for entry in unavailable
            )
        )
    # Download and verify everything before writing anything, so a failure
    # never leaves a partial result set that looks complete.
    verified: list[tuple[PurePosixPath, bytes]] = []
    for entry in selected:
        relative = _safe_relative(entry)
        quoted_id = urllib.parse.quote(entry['artifact_id'], safe='')
        content = fetch(
            f'/runs/{quoted_run}/artifacts/{quoted_id}/content'
        )
        if sha256(content).hexdigest() != entry['sha256']:
            raise FetchError(f'digest mismatch for {entry["uri"]}')
        verified.append((relative, content))
    written: list[Path] = []
    for relative, content in verified:
        destination = output / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_name(destination.name + '.partial')
        partial.write_bytes(content)
        partial.replace(destination)
        written.append(destination)
    (output / 'artifact-manifest.json').write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + '\n'
    )
    return written


def http_fetcher(base_url: str, token: str, *, timeout: float) -> Fetcher:
    def fetch(path: str) -> bytes:
        request = urllib.request.Request(
            base_url.rstrip('/') + path, headers={TOKEN_HEADER: token}
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode('utf-8', 'replace')[:500]
            raise FetchError(f'GET {path} -> HTTP {exc.code}: {detail}') from exc
        except urllib.error.URLError as exc:
            raise FetchError(f'GET {path} failed: {exc.reason}') from exc

    return fetch


def _token(args: argparse.Namespace) -> str:
    if args.token_file:
        token = Path(args.token_file).read_text().strip()
    else:
        token = os.environ.get(TOKEN_ENV, '').strip()
    if not token:
        raise FetchError(f'set {TOKEN_ENV} or pass --token-file')
    return token


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('run_id')
    parser.add_argument('--output', type=Path, default=None,
                        help='destination directory (default ./run-<run_id>)')
    parser.add_argument('--include-jobs', action='store_true',
                        help='also fetch per-job evaluation/metrics/reports')
    parser.add_argument('--base-url', default=DEFAULT_BASE_URL)
    parser.add_argument('--token-file', default=None)
    parser.add_argument('--timeout', type=float, default=120.0)
    args = parser.parse_args(argv)
    output = args.output or Path(f'run-{args.run_id}')
    try:
        fetch = http_fetcher(args.base_url, _token(args), timeout=args.timeout)
        written = fetch_run(
            args.run_id,
            output=output,
            include_jobs=args.include_jobs,
            fetch=fetch,
        )
    except FetchError as exc:
        print(f'fetch-run-artifacts: {exc}', file=sys.stderr)
        return 1
    for path in written:
        print(path)
    print(f'{len(written)} digest-verified artifact(s) written to {output}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
