#!/usr/bin/env python3
"""Report storage usage and clean up terminal-run scratch space (issue #99).

Only ever deletes beaker-worktree/, honeydew-worktree/, and runtime/ under a
run's workspace root, and only for runs that are terminal
(FAILED/CANCELLED/TIMED_OUT/COMPLETE) and have stayed that way for at least
the configured retention window. protocol/, reports/, shared-artifacts/, and
events/ are never touched. See app/storage_retention.py for the full safety
design, including the per-subdirectory artifact-reference check applied
immediately before every deletion.

Safe by default: with no flags (or with --dry-run) this only reports what
WOULD be removed. Pass --apply to actually delete, mirroring
import-sqlite-store-to-postgres.py's convention for destructive maintenance
scripts.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

# When invoked as ``python scripts/...``, Python puts ``scripts`` rather than
# the service root on sys.path. Resolve the adjacent app package explicitly so
# the image-bundled operational tool works from any current directory.
SERVICE_ROOT = Path(__file__).resolve().parents[1]
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from app.config import Settings, get_settings
from app.postgres_store import PostgresStore
from app.storage import SqliteStore
from app.storage_retention import CleanupReport, StorageReport, run_cleanup


def _store(settings: Settings):
    return (
        PostgresStore(settings.store_postgres_dsn)
        if settings.store_backend == 'postgres'
        else SqliteStore(settings.database_path)
    )


def _human_bytes(count: int) -> str:
    value = float(count)
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
        if value < 1024 or unit == 'TiB':
            return f'{value:.1f} {unit}'
        value /= 1024
    return f'{value:.1f} TiB'


def _print_usage(report: StorageReport, *, label: str) -> None:
    print(f'{label}: {_human_bytes(report.total_bytes)} across {len(report.runs)} run(s)')
    for usage in sorted(report.runs, key=lambda item: item.total_bytes, reverse=True):
        breakdown = ', '.join(
            f'{name}={_human_bytes(size)}'
            for name, size in sorted(usage.subdirectory_bytes.items())
            if size
        )
        print(
            f'  {usage.run_id} [{usage.state}]: '
            f'{_human_bytes(usage.total_bytes)}'
            + (f' ({breakdown})' if breakdown else '')
        )


def _print_report(report: CleanupReport) -> None:
    mode = 'DRY RUN (nothing was deleted)' if report.dry_run else 'APPLIED'
    print(f'=== Cleanup report: {mode} ===')
    _print_usage(report.usage_before, label='Storage before')
    print()

    if not report.plans:
        print('No terminal runs are past the retention window.')
    for plan in report.plans:
        print(
            f'{plan.run_id} [{plan.state}], terminal since '
            f'{plan.terminal_since.isoformat()}:'
        )
        for item in plan.subdirectories:
            verb = 'would free' if report.dry_run else 'freed'
            if item.eligible:
                print(f'  - {item.name}: {verb} {_human_bytes(item.bytes_to_free)}')
            else:
                print(f'  - {item.name}: SKIPPED ({item.skip_reason})')

    print()
    verb = 'Would free' if report.dry_run else 'Freed'
    print(f'{verb} {_human_bytes(report.bytes_freed)} total.')
    if report.usage_after is not None:
        print()
        _print_usage(report.usage_after, label='Storage after')


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--retention-days',
        type=int,
        default=None,
        help=(
            'Override Settings.terminal_run_retention_days '
            '(GLASSLAB_ORCHESTRATOR_TERMINAL_RUN_RETENTION_DAYS).'
        ),
    )
    parser.add_argument(
        '--conversation-retention-days',
        type=int,
        default=None,
        help=(
            'Override Settings.conversation_run_retention_days '
            '(GLASSLAB_ORCHESTRATOR_CONVERSATION_RUN_RETENTION_DAYS).'
        ),
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Report what would be deleted. This is also the default.',
    )
    parser.add_argument(
        '--apply',
        action='store_true',
        help='Actually delete eligible storage. Overrides the safe default.',
    )
    args = parser.parse_args()

    if args.dry_run and args.apply:
        parser.error('--dry-run and --apply are mutually exclusive')

    settings = get_settings()
    retention_days = (
        args.retention_days
        if args.retention_days is not None
        else settings.terminal_run_retention_days
    )
    conversation_retention_days = (
        args.conversation_retention_days
        if args.conversation_retention_days is not None
        else settings.conversation_run_retention_days
    )

    report = run_cleanup(
        store=_store(settings),
        workspace_root=Path(settings.workspace_root),
        retention_days=retention_days,
        conversation_retention_days=conversation_retention_days,
        dry_run=not args.apply,
    )
    _print_report(report)
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f'cleanup failed: {exc}', file=sys.stderr)
        raise SystemExit(1)
