"""Shared-group write permissions for the split agent runtime (issue #597).

The orchestrator container (uid 10001) and the opencode sidecar container
(uid 10002, gid 10001) share the NFS-backed artifacts volume. The in-tree NFS
PersistentVolume does not honor ``fsGroup``, so the orchestrator must create
every per-run directory and file group-writable for gid 10001 or the sidecar
cannot write the worktree and runtime state. Setting the process umask to 0002
makes ``mkdir``, ``git worktree add``, and opencode output 0775/0664 instead of
0755/0644.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

SHARED_GROUP_WRITE_UMASK = 0o002


def enable_shared_group_write() -> None:
    """Make every file this process creates group-writable (umask 0002)."""
    os.umask(SHARED_GROUP_WRITE_UMASK)


def make_directory_group_writable(path: Path) -> None:
    """Add group rwX to one existing directory (pre-existing runtime dirs)."""
    _add_group_bits(path, 0o070)


def ensure_group_writable_tree(root: Path) -> None:
    """Add group rwX to an existing tree without removing any permission.

    Used on the resume path: a worktree created before the UID split is
    0755/0644 owned by uid 10001, so the uid-10002 sidecar can read but not
    write it. Only group bits are added; symlinks are skipped so a link cannot
    redirect the chmod outside the tree.
    """
    if not root.is_dir():
        return
    for directory, dirnames, filenames in os.walk(root):
        _add_group_bits(Path(directory), 0o070)
        for name in filenames:
            _add_group_bits(Path(directory) / name, 0o060)
        dirnames[:] = [
            name
            for name in dirnames
            if not (Path(directory) / name).is_symlink()
        ]


def _add_group_bits(path: Path, bits: int) -> None:
    try:
        current = path.lstat().st_mode
    except OSError:
        return
    if stat.S_ISLNK(current):
        return
    try:
        path.chmod(current | bits)
    except OSError:
        return