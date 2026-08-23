"""Per-run, per-agent isolated workspaces over git worktrees.

Each run gets its own root with separate Beaker and Honeydew worktrees (shared
repository object database, independent working trees and indices) plus durable
protocol, shared-artifact, report, and event directories. Everything an agent
can write is confined to its own workspace; authoritative copies and digests
are produced by the orchestrator.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from hashlib import sha256
from pathlib import Path
import shutil
import subprocess
import zipfile

from .schemas import AgentName


class WorkspaceError(RuntimeError):
    pass


@dataclass(frozen=True)
class RunWorkspaces:
    root: Path
    protocol: Path
    beaker: Path
    honeydew: Path
    shared_artifacts: Path
    reports: Path
    events: Path


class WorkspaceManager:
    def __init__(
        self,
        *,
        workspace_root: str,
        approved_repo_path: str,
        approved_repo_ref: str,
    ) -> None:
        self.workspace_root = Path(workspace_root)
        self.approved_repo_path = Path(approved_repo_path).resolve()
        self.approved_repo_ref = approved_repo_ref

    def paths(self, run_id: str) -> RunWorkspaces:
        root = self.workspace_root / run_id
        return RunWorkspaces(
            root=root,
            protocol=root / 'protocol',
            beaker=root / 'beaker-worktree',
            honeydew=root / 'honeydew-worktree',
            shared_artifacts=root / 'shared-artifacts',
            reports=root / 'reports',
            events=root / 'events',
        )

    def prepare(self, run_id: str, *, repo_ref: str | None = None) -> RunWorkspaces:
        paths = self.paths(run_id)
        for path in (
            paths.root,
            paths.protocol,
            paths.shared_artifacts,
            paths.reports,
            paths.events,
        ):
            path.mkdir(parents=True, exist_ok=True)
        if not (self.approved_repo_path / '.git').exists():
            raise WorkspaceError(
                f'approved repository is not a Git checkout: {self.approved_repo_path}'
            )
        ref = repo_ref or self.approved_repo_ref
        self._ensure_worktree(paths.beaker, ref)
        self._ensure_worktree(paths.honeydew, ref)
        self._seed_tool_roster(paths.beaker)
        self._seed_tool_roster(paths.honeydew)
        return paths

    # Models occasionally hallucinate tool names ('run', 'list') that do not
    # exist in the OpenCode runtime; repeated identical invalid calls trip the
    # doom-loop guard and abort the whole turn. The workspace-level AGENTS.md
    # is persistent context every session reads, so the authoritative roster
    # lives here in addition to per-turn prompts.
    TOOL_ROSTER_NOTE = (
        '\n## Available tools (authoritative)\n\n'
        'Exactly these tools exist: bash, edit, glob, grep, read, '
        'todowrite, write. There is no `run` tool and no `list` tool. '
        'Execute every command or script with bash; enumerate files with '
        'glob or `ls` through bash.\n'
    )

    def _seed_tool_roster(self, workspace: Path) -> None:
        agents_md = workspace / 'AGENTS.md'
        if agents_md.is_file():
            text = agents_md.read_text(encoding='utf-8')
            if '## Available tools (authoritative)' not in text:
                agents_md.write_text(
                    text.rstrip() + '\n' + self.TOOL_ROSTER_NOTE,
                    encoding='utf-8',
                )
            return
        agents_md.write_text(
            '# Workspace agent notes\n' + self.TOOL_ROSTER_NOTE,
            encoding='utf-8',
        )

    def _ensure_worktree(self, destination: Path, repo_ref: str) -> None:
        # Worktrees give each agent an isolated working tree while sharing the
        # approved repository's objects; detached HEAD means agents never
        # advance a branch in the approved checkout.
        if destination.exists():
            if not (destination / '.git').exists():
                # An existing non-worktree workspace is treated as corruption
                # and fails the run rather than being silently recreated.
                raise WorkspaceError(
                    f'workspace exists but is not a Git worktree: {destination}'
                )
            return
        destination.parent.mkdir(parents=True, exist_ok=True)
        completed = subprocess.run(
            [
                'git',
                '-C',
                str(self.approved_repo_path),
                'worktree',
                'add',
                '--detach',
                str(destination),
                repo_ref,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise WorkspaceError(completed.stderr.strip() or 'git worktree add failed')

    def worktree_base_commit(self, run_id: str) -> str:
        paths = self.paths(run_id)
        beaker = self._git(paths.beaker, 'rev-parse', 'HEAD')
        honeydew = self._git(paths.honeydew, 'rev-parse', 'HEAD')
        if beaker != honeydew:
            raise WorkspaceError('agent worktrees do not share a base commit')
        return beaker

    def agent_workspace(self, run_id: str, agent: AgentName) -> Path:
        paths = self.paths(run_id)
        if agent == AgentName.BEAKER:
            return paths.beaker
        if agent == AgentName.HONEYDEW:
            return paths.honeydew
        raise WorkspaceError(f'no workspace for agent: {agent}')

    def copy_agent_output(
        self,
        *,
        run_id: str,
        agent: AgentName,
        relative_path: str,
        destination_kind: str,
    ) -> tuple[Path, str]:
        workspace = self.agent_workspace(run_id, agent).resolve()
        source = (workspace / relative_path).resolve()
        if not source.is_relative_to(workspace):
            raise WorkspaceError('agent output escapes isolated workspace')
        if source.is_symlink() or not source.is_file():
            raise WorkspaceError(f'agent output is not a real file: {relative_path}')
        # The source must be a real file inside the agent's own workspace, so
        # an agent can only hand over bytes it actually produced; the returned
        # digest becomes the authoritative record of what was copied.
        paths = self.paths(run_id)
        if destination_kind == 'protocol':
            destination = paths.protocol / 'program.md'
        elif destination_kind == 'report':
            destination = paths.reports / 'report.md'
        else:
            destination = paths.shared_artifacts / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        digest = sha256(destination.read_bytes()).hexdigest()
        return destination, digest

    def freeze_protocol(self, run_id: str) -> None:
        protocol = self.paths(run_id).protocol / 'program.md'
        if protocol.is_symlink() or not protocol.is_file():
            raise WorkspaceError('program.md does not exist')
        protocol.chmod(0o444)
        # After approval the protocol is immutable everywhere (protocol dir and
        # both worktrees) so later phases cannot silently revise what was
        # approved.
        for workspace in (
            self.paths(run_id).beaker,
            self.paths(run_id).honeydew,
        ):
            target = workspace / 'program.md'
            # exists() is false for a dangling link, so test link identity
            # before existence or chmod/copy2 could follow it outside the
            # isolated worktree.
            if target.is_symlink():
                raise WorkspaceError('worktree program.md must not be a symlink')
            if target.exists():
                target.chmod(target.stat().st_mode | 0o200)
            shutil.copy2(protocol, target)
            target.chmod(0o444)

    def create_terminal_retry_checkpoint(
        self, *, parent_run_id: str, child_run_id: str, protocol_digest: str,
        contract: dict[str, str], task_binding: dict | None, base_commit: str,
        maximum_files: int = 128,
        maximum_bytes: int = 4 * 1024 * 1024,
    ) -> tuple[Path, str]:
        """Copy a small, unambiguous workspace delta into a retry child.

        Committed, deleted, renamed and conflicted worktrees are intentionally
        rejected.  A retry must be evidence-preserving, not a best-effort copy
        of an arbitrary Git history.
        """
        parent = self.paths(parent_run_id)
        child = self.paths(child_run_id)
        source_protocol = parent.protocol / 'program.md'
        if not source_protocol.is_file() or source_protocol.is_symlink():
            raise WorkspaceError('retry source protocol is unavailable')
        if sha256(source_protocol.read_bytes()).hexdigest() != protocol_digest:
            raise WorkspaceError('retry source protocol checksum mismatch')
        target_protocol = child.protocol / 'program.md'
        shutil.copy2(source_protocol, target_protocol)
        managed_task_files = self._managed_task_files(task_binding)
        files: list[dict[str, object]] = []
        total = 0
        for name, source_root, target_root in (
            ('beaker', parent.beaker, child.beaker),
            ('honeydew', parent.honeydew, child.honeydew),
        ):
            if self._git(source_root, 'rev-parse', 'HEAD') != base_commit:
                raise WorkspaceError('retry source contains an unbounded committed checkpoint')
            if self._git(target_root, 'rev-parse', 'HEAD') != base_commit:
                raise WorkspaceError('retry child was not created from source base commit')
            status = self._git_bytes(
                source_root, 'status', '--porcelain=v1', '-z',
                '--untracked-files=all',
            )
            for entry in status.split(b'\0'):
                if not entry:
                    continue
                code = entry[:2].decode('ascii')
                relative = entry[3:].decode('utf-8', errors='surrogateescape')
                if code not in {' M', 'M ', '??'}:
                    raise WorkspaceError(f'retry source has ambiguous worktree change: {entry!r}')
                rel = Path(relative)
                if rel.is_absolute() or '..' in rel.parts:
                    raise WorkspaceError('retry source path escapes worktree')
                # These are reconstructed by the orchestrator from their
                # authoritative sources; copying them as a delta would either
                # duplicate immutable task inputs or retain mode 0444.
                if rel == Path('program.md') or rel.parts[0] == 'benchmark-task':
                    continue
                source = source_root / rel
                if source.is_symlink() or not source.is_file():
                    raise WorkspaceError(f'retry source is not a regular file: {relative}')
                if len(files) >= maximum_files:
                    raise WorkspaceError('retry checkpoint exceeds file limit')
                size = source.stat().st_size
                if total + size > maximum_bytes:
                    raise WorkspaceError('retry checkpoint exceeds byte limit')
                target = target_root / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
                target.chmod(target.stat().st_mode | 0o200)
                digest = sha256(target.read_bytes()).hexdigest()
                files.append({'workspace': name, 'path': rel.as_posix(), 'size_bytes': size, 'sha256': digest})
                total += size
        manifest_path = child.events / 'terminal-retry-checkpoint.json'
        manifest = {'schema_version': 'glasslab-terminal-retry-checkpoint-v1', 'parent_run_id': parent_run_id, 'base_commit': base_commit, 'protocol': {'path': 'protocol/program.md', 'sha256': protocol_digest}, 'contract': contract, 'task_binding': task_binding, 'managed_task_files': managed_task_files, 'files': files, 'total_bytes': total}
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + '\n', encoding='utf-8')
        digest = sha256(manifest_path.read_bytes()).hexdigest()
        return manifest_path, digest

    def verify_terminal_retry_checkpoint(self, run_id: str, checkpoint_digest: str) -> dict:
        path = self.paths(run_id).events / 'terminal-retry-checkpoint.json'
        if not path.is_file() or path.is_symlink() or sha256(path.read_bytes()).hexdigest() != checkpoint_digest:
            raise WorkspaceError('retry checkpoint manifest checksum mismatch')
        manifest = json.loads(path.read_text(encoding='utf-8'))
        if manifest.get('schema_version') != 'glasslab-terminal-retry-checkpoint-v1':
            raise WorkspaceError('retry checkpoint schema is unsupported')
        base_commit = manifest.get('base_commit')
        if not isinstance(base_commit, str) or self.worktree_base_commit(run_id) != base_commit:
            raise WorkspaceError('retry worktree base commit mismatch')
        protocol = manifest.get('protocol', {})
        protocol_path = self.paths(run_id).root / str(protocol.get('path', ''))
        if protocol_path.is_symlink() or not protocol_path.is_file() or sha256(protocol_path.read_bytes()).hexdigest() != protocol.get('sha256'):
            raise WorkspaceError('retry protocol checksum mismatch')
        managed_task_files = manifest.get('managed_task_files')
        if not isinstance(managed_task_files, list):
            raise WorkspaceError('retry managed task manifest is invalid')
        for workspace in (self.paths(run_id).beaker, self.paths(run_id).honeydew):
            worktree_protocol = workspace / 'program.md'
            if worktree_protocol.is_symlink():
                raise WorkspaceError('retry worktree protocol must not be a symlink')
            if worktree_protocol.exists() and (
                not worktree_protocol.is_file()
                or sha256(worktree_protocol.read_bytes()).hexdigest()
                != protocol.get('sha256')
            ):
                raise WorkspaceError('retry worktree protocol does not match authoritative protocol')
            task_root = workspace / 'benchmark-task'
            if task_root.is_symlink():
                raise WorkspaceError('retry benchmark-task root must not be a symlink')
            if not managed_task_files:
                if task_root.exists() and any(task_root.rglob('*')):
                    raise WorkspaceError('taskless retry contains benchmark-task content')
                continue
            if not task_root.is_dir() or task_root.is_symlink():
                raise WorkspaceError('retry task inputs are unavailable')
            expected_task_files = {
                str(item.get('path')): str(item.get('sha256'))
                for item in managed_task_files
                if isinstance(item, dict)
            }
            if set(expected_task_files) != {
                'problem.md', 'eval_agent_prompt.md'
            }:
                raise WorkspaceError('retry managed task manifest is invalid')
            actual_task_files = {
                candidate.relative_to(task_root).as_posix()
                for candidate in task_root.rglob('*')
                if candidate.is_file() or candidate.is_symlink()
            }
            if actual_task_files != set(expected_task_files):
                raise WorkspaceError('retry task inputs contain unexpected or missing files')
            for relative, digest in expected_task_files.items():
                candidate = task_root / relative
                if (
                    candidate.is_symlink() or not candidate.is_file()
                    or sha256(candidate.read_bytes()).hexdigest() != digest
                ):
                    raise WorkspaceError('retry task input checksum mismatch')
        manifest_files: set[tuple[str, str]] = set()
        for item in manifest.get('files', []):
            if not isinstance(item, dict): raise WorkspaceError('retry checkpoint file entry is invalid')
            root = self.paths(run_id).beaker if item.get('workspace') == 'beaker' else self.paths(run_id).honeydew if item.get('workspace') == 'honeydew' else None
            rel = Path(str(item.get('path', '')))
            if root is None or rel.is_absolute() or '..' in rel.parts: raise WorkspaceError('retry checkpoint path is invalid')
            candidate = root / rel
            if not candidate.is_file() or candidate.is_symlink() or sha256(candidate.read_bytes()).hexdigest() != item.get('sha256'):
                raise WorkspaceError('retry checkpoint file checksum mismatch')
            manifest_files.add((str(item['workspace']), rel.as_posix()))
        observed_files: set[tuple[str, str]] = set()
        for name, root in (('beaker', self.paths(run_id).beaker), ('honeydew', self.paths(run_id).honeydew)):
            status = self._git_bytes(root, 'status', '--porcelain=v1', '-z', '--untracked-files=all')
            for entry in status.split(b'\0'):
                if not entry:
                    continue
                code = entry[:2].decode('ascii')
                relative = entry[3:].decode('utf-8', errors='surrogateescape')
                rel = Path(relative)
                if code not in {' M', 'M ', '??'} or rel.is_absolute() or '..' in rel.parts:
                    raise WorkspaceError('retry child worktree delta is ambiguous')
                if rel == Path('program.md') or rel.parts[0] == 'benchmark-task':
                    continue
                observed_files.add((name, rel.as_posix()))
        if observed_files != manifest_files:
            raise WorkspaceError('retry child worktree delta does not match manifest')
        return manifest

    @staticmethod
    def _git(path: Path, *args: str) -> str:
        result = subprocess.run(['git', '-C', str(path), *args], capture_output=True, text=True, check=False)
        if result.returncode != 0: raise WorkspaceError(result.stderr.strip() or 'git inspection failed')
        return result.stdout.strip()

    @staticmethod
    def _git_bytes(path: Path, *args: str) -> bytes:
        result = subprocess.run(['git', '-C', str(path), *args], capture_output=True, check=False)
        if result.returncode != 0:
            raise WorkspaceError(result.stderr.decode().strip() or 'git inspection failed')
        return result.stdout

    @staticmethod
    def _managed_task_files(task_binding: dict | None) -> list[dict[str, str]]:
        if task_binding is None:
            return []
        paths = {
            'problem.md': task_binding.get('problem_path'),
            'eval_agent_prompt.md': task_binding.get('evaluator_prompt_path'),
        }
        files: list[dict[str, str]] = []
        for destination, raw_source in paths.items():
            source = Path(str(raw_source or '')).resolve()
            if source.is_symlink() or not source.is_file():
                raise WorkspaceError('retry task source is unavailable')
            files.append({
                'path': destination,
                'sha256': sha256(source.read_bytes()).hexdigest(),
            })
        return files

    def create_review_snapshot(
        self,
        *,
        run_id: str,
        relative_paths: list[str],
        maximum_files: int = 128,
        maximum_bytes: int = 4 * 1024 * 1024,
    ) -> tuple[Path, list[dict[str, object]]]:
        paths = self.paths(run_id)
        source_root = paths.beaker.resolve()
        destination_root = paths.honeydew / '.glasslab-review'
        if destination_root.is_symlink():
            raise WorkspaceError('review snapshot destination is a symlink')
        if destination_root.exists():
            shutil.rmtree(destination_root)
        destination_root.mkdir(parents=True)

        manifest: list[dict[str, object]] = []
        total_bytes = 0
        for relative in sorted(set(relative_paths)):
            relative_path = Path(relative)
            if relative_path.is_absolute() or '..' in relative_path.parts:
                raise WorkspaceError(
                    f'review snapshot path is not relative: {relative}'
                )
            unresolved_source = source_root / relative_path
            path_cursor = source_root
            contains_symlink = False
            for part in relative_path.parts:
                path_cursor /= part
                if path_cursor.is_symlink():
                    contains_symlink = True
                    break
            if contains_symlink:
                raise WorkspaceError(
                    f'review snapshot source traverses a symlink: {relative}'
                )
            # Every path component is checked for symlinks so a symlinked
            # intermediate directory cannot redirect the copy outside the
            # Beaker worktree; the snapshot is read-only and digest-manifested.
            source = unresolved_source.resolve()
            if not source.is_relative_to(source_root):
                raise WorkspaceError(
                    f'review snapshot path escapes Beaker workspace: {relative}'
                )
            if not source.is_file():
                raise WorkspaceError(
                    f'review snapshot source is not a real file: {relative}'
                )
            size = source.stat().st_size
            if len(manifest) >= maximum_files:
                raise WorkspaceError('review snapshot exceeds file limit')
            if total_bytes + size > maximum_bytes:
                raise WorkspaceError('review snapshot exceeds byte limit')

            destination = destination_root / relative_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            destination.chmod(0o444)
            digest = sha256(destination.read_bytes()).hexdigest()
            manifest.append(
                {
                    'path': relative_path.as_posix(),
                    'size_bytes': size,
                    'sha256': digest,
                }
            )
            total_bytes += size

        manifest_path = destination_root / 'manifest.json'
        manifest_path.write_text(
            json.dumps(
                {
                    'schema_version': 'glasslab-review-snapshot-v1',
                    'source_agent': AgentName.BEAKER.value,
                    'files': manifest,
                    'total_bytes': total_bytes,
                },
                indent=2,
                sort_keys=True,
            )
            + '\n',
            encoding='utf-8',
        )
        manifest_path.chmod(0o444)
        return destination_root, manifest

    def write_recovery_checkpoint(
        self,
        *,
        run_id: str,
        agent: AgentName,
        payload: dict,
    ) -> Path:
        destination = self.paths(run_id).events / (
            f'{agent.value}-recovery-checkpoint.json'
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Temp-file plus atomic replace keeps the checkpoint durable and
        # complete; it lives under events so recovery replays from the same
        # ordered location as the rest of the log.
        temporary = destination.with_suffix('.tmp')
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + '\n',
            encoding='utf-8',
        )
        temporary.replace(destination)
        return destination

    def install_task_bundle(
        self,
        *,
        run_id: str,
        problem_path: str,
        evaluator_prompt_path: str,
    ) -> None:
        for workspace in (
            self.paths(run_id).beaker,
            self.paths(run_id).honeydew,
        ):
            destination = workspace / 'benchmark-task'
            destination.mkdir(parents=True, exist_ok=True)
            for source_name, destination_name in (
                (problem_path, 'problem.md'),
                (evaluator_prompt_path, 'eval_agent_prompt.md'),
            ):
                source = Path(source_name).resolve()
                if source.is_symlink() or not source.is_file():
                    raise WorkspaceError(
                        f'benchmark task file is unavailable: {source}'
                    )
                target = destination / destination_name
                shutil.copy2(source, target)
                target.chmod(0o444)
            # Task inputs land under a fixed, read-only location in both
            # workspaces: agents may read the task but cannot rewrite it.
            destination.chmod(0o555)

    def install_run_datasets(
        self,
        *,
        run_id: str,
        datasets: list[tuple[str, str]],
    ) -> list[str]:
        # Objective-referenced ingested datasets land read-only under
        # datasets/ in both workspaces so agent local checks never need the
        # network. Idempotent: an existing file is left untouched.
        installed: list[str] = []
        for workspace in (
            self.paths(run_id).beaker,
            self.paths(run_id).honeydew,
        ):
            destination = workspace / 'datasets'
            destination.mkdir(parents=True, exist_ok=True)
            for source_name, filename in datasets:
                source = Path(source_name).resolve()
                if source.is_symlink() or not source.is_file():
                    raise WorkspaceError(
                        f'run dataset is unavailable: {filename}'
                    )
                target = destination / Path(filename).name
                if not target.exists():
                    shutil.copy2(source, target)
                    target.chmod(0o444)
                installed.append(
                    (target.relative_to(workspace)).as_posix()
                )
            destination.chmod(0o555)
        return installed

    def package_source_bundle(
        self,
        *,
        run_id: str,
        source_subdirectory: str,
    ) -> tuple[Path, str]:
        workspace = self.paths(run_id).beaker.resolve()
        source = (workspace / source_subdirectory).resolve()
        if not source.is_relative_to(workspace) or not source.is_dir():
            raise WorkspaceError(
                f'benchmark source directory is missing: {source_subdirectory}'
            )
        entrypoint = source / 'run.py'
        if entrypoint.is_symlink() or not entrypoint.is_file():
            raise WorkspaceError('benchmark source requires a real run.py')
        destination = self.paths(run_id).shared_artifacts / 'source.zip'
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Deterministic archive (fixed mtime, fixed mode, sorted entries) so
        # the same source tree always produces the same sha256, making the
        # bundle digest meaningful across submissions.
        with zipfile.ZipFile(
            destination,
            mode='w',
            compression=zipfile.ZIP_DEFLATED,
        ) as archive:
            for path in sorted(source.rglob('*')):
                if path.is_symlink():
                    raise WorkspaceError(
                        f'benchmark source cannot contain symlinks: {path}'
                    )
                if path.is_file():
                    info = zipfile.ZipInfo(path.relative_to(source).as_posix())
                    info.date_time = (1980, 1, 1, 0, 0, 0)
                    info.external_attr = 0o100644 << 16
                    archive.writestr(info, path.read_bytes())
        digest = sha256(destination.read_bytes()).hexdigest()
        return destination, digest

    def copy_contract_candidate_for_review(
        self,
        *,
        run_id: str,
        source: Path,
        contract_id: str,
        version: str,
        digest: str,
    ) -> Path:
        source = source.resolve()
        if source.is_symlink() or not source.is_dir():
            raise WorkspaceError('sealed contract candidate is not a directory')
        destination = (
            self.paths(run_id).honeydew
            / 'contract-candidate-review'
            / contract_id
            / version
            / digest
        )
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(source, destination)
        # The reviewer sees exactly the sealed bytes (digest-named path, made
        # read-only after copy), not a freshly copied tree that could diverge.
        for path in destination.rglob('*'):
            path.chmod(0o555 if path.is_dir() else 0o444)
        return destination
