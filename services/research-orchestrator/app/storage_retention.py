"""Storage usage reporting and cleanup for terminal runs (issue #99).

Scope, deliberately narrow: a run's workspace root
(``workspace_root/<run_id>/``) holds both durable, referenced material
(``protocol/``, ``reports/``, ``shared-artifacts/``, ``events/``) and pure
agent-process scratch space (``beaker-worktree/``, ``honeydew-worktree/``,
``runtime/``). Only the scratch-space subdirectories are ever eligible for
cleanup; nothing in the database references a path inside them (artifacts
only ever land in protocol/reports/shared-artifacts -- see
``workspaces.copy_agent_output`` and every ``ArtifactRecord`` construction
site in ``engine.py``). A run is only eligible once it is terminal
(FAILED/CANCELLED/TIMED_OUT/COMPLETE) and has stayed that way for at least
``Settings.terminal_run_retention_days``; active and paused runs are never
touched.

As a genuine runtime safety net -- not just a static code audit -- cleanup
still cross-checks every candidate subdirectory against that run's
``ArtifactRecord.metadata['path']`` entries before deleting anything, and
skips (rather than deletes) any subdirectory that turns out to contain a
referenced path. This is a report-then-act design throughout:
``plan_cleanup`` never touches the filesystem, and both dry-run and real
cleanup share the exact same plan so a dry run is a truthful preview.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
import os
from pathlib import Path
import shutil
from typing import Any

from .schemas import TERMINAL_STATES, utc_now
from .storage import RecordNotFound

# Only these subdirectories of a run's workspace root are ever eligible for
# cleanup. protocol/, reports/, shared-artifacts/, and events/ are never
# listed here and are never deleted by this module under any circumstance.
CLEANABLE_SUBDIRECTORIES = ('beaker-worktree', 'honeydew-worktree', 'runtime')


def _directory_bytes(path: Path) -> int:
    """Sum file sizes under ``path`` without following symlinks."""
    if not path.exists():
        return 0
    total = 0
    for root, _dirs, files in os.walk(path, followlinks=False):
        for name in files:
            file_path = Path(root) / name
            try:
                total += file_path.lstat().st_size
            except OSError:
                # A file removed concurrently (e.g. by the running agent
                # process) is not counted rather than failing the whole scan.
                continue
    return total


@dataclass(frozen=True)
class RunStorageUsage:
    run_id: str
    state: str
    total_bytes: int
    # Byte size of each immediate subdirectory under the run's workspace
    # root, keyed by name (protocol, beaker-worktree, runtime, etc.).
    subdirectory_bytes: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class StorageReport:
    generated_at: datetime
    total_bytes: int
    runs: tuple[RunStorageUsage, ...]


def report_storage_usage(
    *,
    store: Any,
    workspace_root: Path,
    now: datetime | None = None,
) -> StorageReport:
    """Observability for issue #99 item 5: total and per-run usage."""
    usages: list[RunStorageUsage] = []
    for run in store.list_runs():
        run_root = workspace_root / run.run_id
        if not run_root.is_dir():
            continue
        subdirectory_bytes = {
            entry.name: _directory_bytes(entry)
            for entry in sorted(run_root.iterdir())
            if entry.is_dir() and not entry.is_symlink()
        }
        usages.append(
            RunStorageUsage(
                run_id=run.run_id,
                state=run.state.value,
                total_bytes=sum(subdirectory_bytes.values()),
                subdirectory_bytes=subdirectory_bytes,
            )
        )
    return StorageReport(
        generated_at=now or utc_now(),
        total_bytes=sum(usage.total_bytes for usage in usages),
        runs=tuple(usages),
    )


def _referenced_paths(store: Any, run_id: str) -> list[Path]:
    """Resolved filesystem paths this run's artifacts actually point at.

    Only ``metadata['path']`` entries carry a real filesystem path (see the
    module docstring); artifacts without one (or with a non-local ``uri``
    scheme) contribute nothing here, which is safe because the presence
    check below only ever *protects* a subdirectory, never authorizes
    deleting one.
    """
    referenced: list[Path] = []
    for artifact in store.list_artifacts(run_id):
        raw_path = artifact.metadata.get('path')
        if isinstance(raw_path, str) and raw_path:
            referenced.append(Path(raw_path).resolve())
    return referenced


@dataclass(frozen=True)
class SubdirectoryCleanupPlan:
    name: str
    path: Path
    bytes_to_free: int
    # None means eligible; otherwise the reason cleanup is refusing to touch
    # this subdirectory (e.g. a referenced artifact path lives under it).
    skip_reason: str | None = None

    @property
    def eligible(self) -> bool:
        return self.skip_reason is None


@dataclass(frozen=True)
class RunCleanupPlan:
    run_id: str
    state: str
    terminal_since: datetime
    subdirectories: tuple[SubdirectoryCleanupPlan, ...]

    @property
    def eligible_bytes(self) -> int:
        return sum(
            item.bytes_to_free for item in self.subdirectories if item.eligible
        )


def plan_cleanup(
    *,
    store: Any,
    workspace_root: Path,
    retention_days: int,
    conversation_retention_days: int | None = None,
    now: datetime | None = None,
) -> list[RunCleanupPlan]:
    """Read-only: decide what cleanup would do, touching no filesystem state.

    A run is a candidate in two cases:

    1. Terminal runs (run.state in TERMINAL_STATES): eligible once they've
       stayed in a terminal state for at least ``retention_days`` (the terminal
       run retention window), using ``updated_at`` as the "terminal since"
       timestamp. Every state transition (including into a terminal state) sets
       it, and terminal states are not left once entered, so nothing legitimately
       bumps it afterward. If that assumption is ever wrong the effect is only a
       later cleanup, never an early one.

    2. Conversation runs (run.conversation=True): inert runs that never reach a
       terminal state. They become eligible after ``conversation_retention_days``
       based on their ``updated_at`` timestamp (last activity). This separate
       retention window ensures long-lived conversation runs don't cause unbounded
       storage growth.

    Both eligibility paths are checked independently; a run qualifies if it meets
    EITHER condition.
    """
    now = now or utc_now()
    terminal_threshold = timedelta(days=retention_days)
    conversation_threshold = (
        timedelta(days=conversation_retention_days)
        if conversation_retention_days is not None
        else None
    )
    plans: list[RunCleanupPlan] = []
    for run in store.list_runs():
        # Determine if run is eligible based on terminal or conversation criteria
        is_terminal = run.state in TERMINAL_STATES
        is_conversation = run.conversation

        if not is_terminal and not is_conversation:
            continue

        # Calculate eligibility timestamp and threshold
        if is_terminal:
            eligibility_timestamp = run.updated_at
            if now - eligibility_timestamp < terminal_threshold:
                continue
        else:
            # Conversation run: eligibility based on updated_at (last activity)
            if conversation_threshold is None:
                continue
            eligibility_timestamp = run.updated_at
            if now - eligibility_timestamp < conversation_threshold:
                continue
        run_root = workspace_root / run.run_id
        referenced = _referenced_paths(store, run.run_id)
        subdirectories: list[SubdirectoryCleanupPlan] = []
        for name in CLEANABLE_SUBDIRECTORIES:
            candidate = run_root / name
            if not candidate.is_dir():
                continue
            resolved_candidate = candidate.resolve()
            blocking = next(
                (
                    path
                    for path in referenced
                    if path.is_relative_to(resolved_candidate)
                ),
                None,
            )
            subdirectories.append(
                SubdirectoryCleanupPlan(
                    name=name,
                    path=candidate,
                    bytes_to_free=_directory_bytes(candidate),
                    skip_reason=(
                        f'referenced by an artifact record: {blocking}'
                        if blocking is not None
                        else None
                    ),
                )
            )
        if subdirectories:
            plans.append(
                RunCleanupPlan(
                    run_id=run.run_id,
                    state=run.state.value,
                    terminal_since=eligibility_timestamp,
                    subdirectories=tuple(subdirectories),
                )
            )
    return plans


@dataclass(frozen=True)
class CleanupReport:
    generated_at: datetime
    dry_run: bool
    plans: tuple[RunCleanupPlan, ...]
    usage_before: StorageReport
    # None in dry-run mode: nothing was deleted, so a post-cleanup usage
    # report would be identical to usage_before and is not worth recomputing
    # (this can be a slow full-tree walk on NFS).
    usage_after: StorageReport | None

    @property
    def bytes_freed(self) -> int:
        return sum(plan.eligible_bytes for plan in self.plans)


def run_cleanup(
    *,
    store: Any,
    workspace_root: Path,
    retention_days: int,
    conversation_retention_days: int | None = None,
    dry_run: bool,
    now: datetime | None = None,
) -> CleanupReport:
    """Report storage usage, plan cleanup, and (unless dry_run) apply it.

    dry_run and a real run share the identical plan_cleanup() output, so a
    dry run is a truthful preview of exactly what a real run would remove --
    it is never a separate, looser code path.
    """
    now = now or utc_now()
    usage_before = report_storage_usage(
        store=store,
        workspace_root=workspace_root,
        now=now,
    )
    plans = plan_cleanup(
        store=store,
        workspace_root=workspace_root,
        retention_days=retention_days,
        conversation_retention_days=conversation_retention_days,
        now=now,
    )
    if dry_run:
        return CleanupReport(
            generated_at=now,
            dry_run=True,
            plans=tuple(plans),
            usage_before=usage_before,
            usage_after=None,
        )

    for plan in plans:
        # Re-fetch the run immediately before deleting: if it somehow left
        # terminal state (or vanished) between planning and now, skip it
        # rather than delete storage out from under an active run. This is
        # cheap insurance against a plan going stale during a long scan.
        try:
            current = store.get_run(plan.run_id)
        except RecordNotFound:
            continue
        if current.state not in TERMINAL_STATES:
            continue
        for item in plan.subdirectories:
            if not item.eligible:
                continue
            if item.path.is_symlink() or not item.path.is_dir():
                continue
            try:
                current = store.get_run(plan.run_id)
            except RecordNotFound:
                continue
            is_terminal = current.state in TERMINAL_STATES
            is_conversation = current.conversation
            if not is_terminal and not is_conversation:
                continue
            if is_terminal:
                terminal_since = current.updated_at
                if now - terminal_since < timedelta(days=retention_days):
                    continue
            else:
                if conversation_retention_days is None:
                    continue
                if now - current.updated_at < timedelta(days=conversation_retention_days):
                    continue
            shutil.rmtree(item.path, ignore_errors=False)

    usage_after = report_storage_usage(
        store=store,
        workspace_root=workspace_root,
        now=utc_now(),
    )
    return CleanupReport(
        generated_at=now,
        dry_run=False,
        plans=tuple(plans),
        usage_before=usage_before,
        usage_after=usage_after,
    )
