#!/usr/bin/env python3
"""Mechanical GATE subset for emitted advisories (eval/corpus_rag/rubric.md).

Checks, purely mechanically:

(a) kind == method_advisory => every candidate has >= 1 citation;
(b) every citation chunk_id resolves via ``store.list_rag_chunks()``;
(c) evidence_uri starts with ``knowledge://<source_id>`` and source_id nonempty;
(d) contradiction_pairs entries carry keys a, b, topic;
(e) uncertainty_statement is nonempty.

kind == insufficient_corpus is valid when ``reason`` is nonempty.

Exit 0 prints ``{"valid": true, ...counts}``; exit 1 prints
``{"valid": false, "violations": [...]}``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from app.storage import SqliteStore

_KNOWLEDGE_PREFIX = 'knowledge://'


def _check_citation(
    citation: dict[str, Any], where: str, stored_ids: set[str], violations: list[str]
) -> None:
    chunk_id = citation.get('chunk_id')
    source_id = citation.get('source_id') or ''
    uri = str(citation.get('evidence_uri') or '')
    if chunk_id not in stored_ids:
        violations.append(f'{where}: chunk_id {chunk_id!r} does not resolve in store')
    if not source_id or not uri.startswith(f'{_KNOWLEDGE_PREFIX}{source_id}'):
        violations.append(
            f'{where}: evidence_uri {uri!r} malformed for source_id {source_id!r}'
        )


def _violations(doc: dict[str, Any], stored_ids: set[str]) -> list[str]:
    kind = doc.get('kind')
    if kind == 'insufficient_corpus':
        problems = []
        if not str(doc.get('reason') or '').strip():
            problems.append('insufficient_corpus: reason is empty')
        return problems
    if kind != 'method_advisory':
        return [f'unknown advisory kind {kind!r}']

    violations: list[str] = []
    for i, candidate in enumerate(doc.get('candidates') or []):
        name = candidate.get('method_name')
        if not candidate.get('citations'):
            violations.append(f'candidate[{i}] ({name!r}): has no citations')
        for j, citation in enumerate(candidate.get('citations') or []):
            _check_citation(citation, f'candidate[{i}].citations[{j}]', stored_ids, violations)
    for j, citation in enumerate(doc.get('citations_all') or []):
        _check_citation(citation, f'citations_all[{j}]', stored_ids, violations)
    for j, pair in enumerate(doc.get('contradiction_pairs') or []):
        missing = {'a', 'b', 'topic'} - set(pair)
        if missing:
            violations.append(f'contradiction_pairs[{j}]: missing keys {sorted(missing)}')
    if not str(doc.get('uncertainty_statement') or '').strip():
        violations.append('uncertainty_statement is empty')
    return violations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog='check-advisory',
        description='Mechanical GATE checks for a corpus-RAG advisory JSON document.',
    )
    parser.add_argument('--advisory-json', required=True, help='advisory JSON path')
    parser.add_argument('--store', required=True, help='SQLite store path')
    args = parser.parse_args(argv)

    doc = json.loads(Path(args.advisory_json).read_text())
    store = SqliteStore(args.store)
    stored_ids = {row['chunk_id'] for row in store.list_rag_chunks()}

    violations = _violations(doc, stored_ids)
    if violations:
        print(json.dumps({'valid': False, 'violations': violations}, indent=2))
        return 1

    counts: dict[str, Any] = {'valid': True, 'kind': doc.get('kind')}
    if doc.get('kind') == 'method_advisory':
        counts.update(
            candidates=len(doc.get('candidates') or []),
            citations=sum(len(c.get('citations') or []) for c in doc.get('candidates') or []),
            contradiction_pairs=len(doc.get('contradiction_pairs') or []),
        )
    print(json.dumps(counts, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
