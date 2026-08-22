#!/usr/bin/env python3
"""One-shot migration from the JSON file store into Postgres.

Idempotent: the INSERT uses ON CONFLICT DO UPDATE on the fixed store_key
``'default'``, so re-running safely replaces the single authoritative row
without creating duplicates or leaving stale state.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys


POSTGRES_DSN_ENV = 'GLASSLAB_WORKFLOW_API_STORE_POSTGRES_DSN'


def load_dsn(parser: argparse.ArgumentParser, dsn_fd: int | None) -> str:
    """Load the DSN from a non-argv channel and remove inherited state."""
    environment_dsn = os.environ.pop(POSTGRES_DSN_ENV, None)
    if environment_dsn is not None and dsn_fd is not None:
        parser.error(f'use either {POSTGRES_DSN_ENV} or --dsn-fd, not both')

    if dsn_fd is not None:
        try:
            with os.fdopen(dsn_fd, encoding='utf-8', closefd=True) as stream:
                dsn = stream.read().rstrip('\r\n')
        except OSError:
            parser.error('could not read the Postgres DSN from --dsn-fd')
    else:
        dsn = environment_dsn or ''

    if not dsn:
        parser.error(f'{POSTGRES_DSN_ENV} or --dsn-fd is required')
    return dsn


def main() -> int:
    parser = argparse.ArgumentParser(description='Import workflow-api JSON state into the Postgres workflow_state table.')
    parser.add_argument('--json-path', required=True, help='Path to run-store.json')
    parser.add_argument('--dsn-fd', type=int, help='Readable file descriptor containing the Postgres DSN')
    parser.add_argument('--dsn', nargs='?', const='', default=None, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.dsn is not None:
        parser.error(f'--dsn is not supported; use {POSTGRES_DSN_ENV} or --dsn-fd')
    dsn = load_dsn(parser, args.dsn_fd)

    json_path = Path(args.json_path)
    payload = json.loads(json_path.read_text(encoding='utf-8'))

    import psycopg

    try:
        with psycopg.connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    '''
                    CREATE TABLE IF NOT EXISTS workflow_state (
                        store_key TEXT PRIMARY KEY,
                        payload JSONB NOT NULL,
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                    '''
                )
                # The entire JSON file becomes a single row keyed by 'default'.
                # ON CONFLICT DO UPDATE makes the migration idempotent: re-running
                # replaces the row instead of duplicating it.
                cur.execute(
                    '''
                    INSERT INTO workflow_state (store_key, payload, updated_at)
                    VALUES (%s, %s::jsonb, NOW())
                    ON CONFLICT (store_key) DO UPDATE
                    SET payload = EXCLUDED.payload,
                        updated_at = EXCLUDED.updated_at
                    ''',
                    ('default', json.dumps(payload)),
                )
            conn.commit()
    except psycopg.Error:
        print('Postgres import failed.', file=sys.stderr)
        return 1

    print(f'Imported {json_path} into workflow_state(default).')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
