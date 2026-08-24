#!/usr/bin/env python3
"""Backfill committed runtime-replay observations to the current schema.

Rewrites observation rows in place to glasslab-runtime-replay-observation-v2,
preserving every measured value verbatim and adding v2 fields as null unless a
value is derivable from the row's own recorded notes. Dry-run by default;
--apply performs the write. Idempotent: v2 rows are left untouched.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

V2 = 'glasslab-runtime-replay-observation-v2'

DERIVED_REVISION_CYCLES = {
    # Ox replay completed its repair in one documented revision cycle.
    'opencode-go/ox-alpha-free': 1,
}

DERIVED_NOTES_SUFFIX = {
    'exo/mlx-community/Qwen3-Coder-Next-4bit': (
        '; revision_cycles null: single stalled turn, no discrete revision '
        'cycles were observable'
    ),
    'opencode-go/ox-alpha-free': '',
}


def backfill_row(row: dict) -> dict:
    if row.get('schema_version') == V2:
        return row
    updated = dict(row)
    updated['schema_version'] = V2
    updated.setdefault('tool_error_count', None)
    if 'invalid_tool_call_count' not in updated:
        updated['invalid_tool_call_count'] = None
    updated.setdefault('doom_loop_event_count', None)
    updated['doom_loop_threshold'] = None
    candidate = updated.get('candidate', '')
    updated['revision_cycles'] = DERIVED_REVISION_CYCLES.get(candidate)
    suffix = DERIVED_NOTES_SUFFIX.get(candidate, '')
    if suffix:
        updated['notes'] = updated.get('notes', '') + suffix
    updated.setdefault('session_db_layout', None)
    return updated


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        'paths',
        nargs='*',
        type=Path,
        default=[
            Path(__file__).resolve().parents[3]
            / 'docs'
            / 'glasslab-v2'
            / 'runtime-replay'
            / 'wine-classification-v1-run98-observations.jsonl'
        ],
        help='observation JSONL files (default: committed run98 observations)',
    )
    parser.add_argument(
        '--apply', action='store_true', help='write changes; default is dry-run'
    )
    args = parser.parse_args(argv)

    changed_any = False
    for path in args.paths:
        lines = path.read_text().splitlines()
        rows = [json.loads(line) for line in lines if line.strip()]
        backfilled = [backfill_row(row) for row in rows]
        if backfilled == rows:
            print(f'{path}: already v2, nothing to do')
            continue
        changed_any = True
        payload = '\n'.join(
            json.dumps(row, sort_keys=True) for row in backfilled
        ) + '\n'
        if args.apply:
            path.write_text(payload)
            print(f'{path}: wrote {len(backfilled)} v2 rows')
        else:
            print(f'{path}: dry-run, {len(backfilled)} rows would be rewritten')
    return 0 if changed_any or args.apply else 1


if __name__ == '__main__':
    sys.exit(main())
