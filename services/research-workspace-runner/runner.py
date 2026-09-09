"""Execute one frozen research workspace under the generic Glasslab run contract.

Bounded execution: verify input digests, run one declared command under a
wall-clock timeout, then write a terminal bundle (manifest, config, artifact
index, status) even when verification or execution fails. The workload emits
metrics and evidence only; this module never writes evaluation.json or any
scoring record. The deterministic evaluator owns scoring and writes
evaluation.json after the run.
"""

from __future__ import annotations

from collections.abc import Mapping
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import tarfile
import traceback
from typing import Any
from urllib.parse import urlparse
import zipfile


# Names reserved for deterministic machinery (runner + evaluator), never the
# workload. The required-artifact check skips these, so a workload can never
# be required to emit evaluation material; evaluation.json is reserved for the
# evaluator and is deliberately not written here.
BASE_GENERATED_ARTIFACTS = {
    'run_manifest.json',
    'config.json',
    'artifacts_index.json',
    'status.json',
    'logs/',
    'evaluation.json',
}


def _read_json_env(env: Mapping[str, str], name: str) -> dict[str, Any]:
    raw = env.get(name, '').strip()
    if not raw:
        raise ValueError(f'missing required environment variable: {name}')
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError(f'{name} must contain a JSON object')
    return payload


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # sort_keys keeps emitted records byte-stable across runs for clean diffs.
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + '\n')


def _file_sha256(path: Path) -> str:
    # Chunked read keeps memory flat for multi-GB datasets and artifacts.
    digest = sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _is_within(path: Path, roots: list[Path]) -> bool:
    resolved = path.resolve()
    return any(resolved == root or resolved.is_relative_to(root) for root in roots)


def resolve_asset_uri(
    uri: str,
    *,
    dataset_root: Path,
    artifacts_root: Path,
) -> Path:
    # s3:// prefixes are namespace conventions mapped into the two approved
    # roots; file:// and bare absolute paths must still land inside them.
    if uri.startswith('s3://datasets/'):
        path = dataset_root / uri.removeprefix('s3://datasets/')
    elif uri.startswith('s3://glasslab-datasets/'):
        path = dataset_root / uri.removeprefix('s3://glasslab-datasets/')
    elif uri.startswith('s3://artifacts/'):
        path = artifacts_root / uri.removeprefix('s3://artifacts/')
    elif uri.startswith('file://'):
        path = Path(urlparse(uri).path)
    elif uri.startswith('/'):
        path = Path(uri)
    else:
        raise ValueError(f'unsupported immutable asset URI: {uri}')

    roots = [dataset_root.resolve(), artifacts_root.resolve()]
    if not _is_within(path, roots):
        raise ValueError(f'asset path is outside approved mounted roots: {path}')
    # Deny symlinks even when the resolved target stays inside the roots:
    # evidence provenance must be the actual on-disk file, not an alias.
    if path.is_symlink():
        raise ValueError(f'asset path cannot be a symlink: {path}')
    if not path.is_file():
        raise FileNotFoundError(f'asset file not found: {path}')
    return path


def _safe_zip_extract(archive: Path, destination: Path) -> None:
    # Validate every member (no traversal, no symlinks) before extracting so a
    # hostile archive cannot write outside the destination.
    with zipfile.ZipFile(archive) as handle:
        for member in handle.infolist():
            target = destination / member.filename
            if not _is_within(target, [destination.resolve()]):
                raise ValueError(f'zip member escapes workspace: {member.filename}')
            unix_mode = member.external_attr >> 16
            if unix_mode & 0o170000 == 0o120000:
                raise ValueError(f'zip symlinks are not allowed: {member.filename}')
        handle.extractall(destination)


def _safe_tar_extract(archive: Path, destination: Path) -> None:
    with tarfile.open(archive) as handle:
        for member in handle.getmembers():
            target = destination / member.name
            if not _is_within(target, [destination.resolve()]):
                raise ValueError(f'tar member escapes workspace: {member.name}')
            if member.issym() or member.islnk():
                raise ValueError(f'tar links are not allowed: {member.name}')
        # filter='data' additionally blocks special-file and setuid entries;
        # the explicit link checks above keep the failure messages precise.
        handle.extractall(destination, filter='data')


def materialize_asset(
    reference: Mapping[str, Any],
    destination: Path,
    *,
    dataset_root: Path,
    artifacts_root: Path,
) -> Path:
    uri = str(reference.get('uri', '')).strip()
    expected_sha256 = str(reference.get('sha256', '')).strip().lower()
    if len(expected_sha256) != 64:
        raise ValueError('immutable asset reference requires a sha256 digest')

    source = resolve_asset_uri(
        uri,
        dataset_root=dataset_root,
        artifacts_root=artifacts_root,
    )
    actual_sha256 = _file_sha256(source)
    if actual_sha256 != expected_sha256:
        raise ValueError(
            f'asset digest mismatch for {uri}: expected {expected_sha256}, got {actual_sha256}'
        )

    # Digest is verified against the archive file itself before anything is
    # extracted; only archive media are unpacked, everything else is copied
    # verbatim so the workspace sees byte-identical inputs.

    destination.mkdir(parents=True, exist_ok=True)
    if zipfile.is_zipfile(source):
        _safe_zip_extract(source, destination)
    elif tarfile.is_tarfile(source):
        _safe_tar_extract(source, destination)
    else:
        shutil.copy2(source, destination / source.name)
    return source


def verify_dataset_bindings(
    contracts: Any,
    resolved_bindings: Mapping[str, Any],
    *,
    dataset_root: Path,
    artifacts_root: Path,
) -> None:
    if not isinstance(contracts, list):
        raise ValueError('config_payload.dataset_contracts must be a list')
    expected_names: set[str] = set()
    for contract in contracts:
        if not isinstance(contract, dict):
            raise ValueError('dataset contracts must be JSON objects')
        name = str(contract.get('name', '')).strip()
        asset = contract.get('asset')
        if not name or not isinstance(asset, dict):
            raise ValueError('dataset contract requires name and asset')
        if name in expected_names:
            raise ValueError(f'dataset contract name is duplicated: {name}')
        expected_names.add(name)
        resolved_value = resolved_bindings.get(name)
        if not isinstance(resolved_value, str) or not resolved_value.strip():
            raise ValueError(f'dataset binding was not resolved: {name}')
        path = Path(resolved_value)
        if not _is_within(
            path,
            [dataset_root.resolve(), artifacts_root.resolve()],
        ):
            raise ValueError(f'dataset binding is outside approved mounted roots: {name}')
        if not path.is_file():
            raise FileNotFoundError(f'dataset binding file not found: {name}')
        expected_sha256 = str(asset.get('sha256', '')).strip().lower()
        actual_sha256 = _file_sha256(path)
        if actual_sha256 != expected_sha256:
            raise ValueError(
                f'dataset digest mismatch for {name}: '
                f'expected {expected_sha256}, got {actual_sha256}'
            )
    # Reverse-direction check closes the loop: every resolved binding must be
    # declared by a contract, so the workload can never be handed undeclared
    # files through the environment.
    unexpected_names = sorted(set(resolved_bindings) - expected_names)
    if unexpected_names:
        raise ValueError(
            'resolved dataset bindings were not declared: '
            + ', '.join(unexpected_names)
        )


def _artifact_exists(run_root: Path, name: str) -> bool:
    # Symlinks and anything escaping the run root can never satisfy a required
    # artifact; directory names must carry the trailing slash used by the
    # manifest naming convention.
    path = run_root / name.rstrip('/')
    if path.is_symlink() or not _is_within(path, [run_root.resolve()]):
        return False
    return path.is_dir() if name.endswith('/') else path.is_file()


def _build_artifact_index(run_id: str, run_root: Path, required: set[str]) -> dict[str, Any]:
    # Snapshots every regular file under the run root so all evidence plus the
    # runner records present at this point are enumerated with digests. Runs
    # before status.json and the index itself are written, so those two are
    # intentionally absent rather than self-referential entries.
    artifacts: list[dict[str, Any]] = []
    for path in sorted(run_root.rglob('*')):
        relative = path.relative_to(run_root).as_posix()
        entry: dict[str, Any] = {
            'name': relative,
            'path': str(path),
            'media_type': 'application/octet-stream',
            'required': relative in required,
        }
        try:
            if (
                path.is_symlink()
                or not _is_within(path, [run_root.resolve()])
                or path.is_dir()
                or not path.is_file()
            ):
                continue
            entry['size_bytes'] = path.stat().st_size
            entry['sha256'] = _file_sha256(path)
            entry['description'] = 'Emitted by the frozen research workspace.'
        except (OSError, RuntimeError) as exc:
            # A workload-controlled file must never abort the bundle: an
            # unreadable, vanished, or looping path is recorded as an
            # unhashable entry so the anomaly stays visible in the evidence
            # trail instead of suppressing it.
            entry['size_bytes'] = None
            entry['sha256'] = None
            entry['unhashable'] = True
            entry['description'] = f'Unreadable or vanished at bundle time: {exc}'
        artifacts.append(entry)
    return {'run_id': run_id, 'artifacts': artifacts}


def _write_status(run_root: Path, run_id: str, terminal_status: str, detail: str) -> None:
    status_payload: dict[str, Any] = {
        'run_id': run_id,
        'status': terminal_status,
        'detail': detail,
    }
    status_path = run_root / 'status.json'
    temporary_status_path = run_root / '.status.json.tmp'
    # Temp-then-rename keeps status.json atomic; a reader never observes a
    # partially written terminal status.
    _write_json(temporary_status_path, status_payload)
    temporary_status_path.replace(status_path)


def _write_terminal_bundle(
    *,
    run_id: str,
    run_root: Path,
    manifest: dict[str, Any],
    config: dict[str, Any],
    terminal_status: str,
    detail: str,
) -> tuple[list[str], str, str]:
    # The bundle is always written, even after a failed verification or run,
    # so a run id always has readable terminal state.
    _write_json(run_root / 'run_manifest.json', manifest)
    _write_json(run_root / 'config.json', config)
    required = set(manifest.get('expected_artifacts', {}).get('required', []))
    missing = sorted(
        name
        for name in required
        if (
            name not in BASE_GENERATED_ARTIFACTS
            and not _artifact_exists(run_root, name)
        )
    )
    if missing and terminal_status == 'succeeded':
        # A successful exit is not enough: a run is still failed when declared
        # workload artifacts are absent, so evidence obligations are enforced
        # regardless of the command's exit code.
        terminal_status = 'failed'
        detail = 'workspace command exited successfully but required artifacts are missing: ' + ', '.join(missing)
    _write_json(
        run_root / 'artifacts_index.json',
        _build_artifact_index(run_id, run_root, required),
    )
    return missing, terminal_status, detail


def run_from_environment(env: Mapping[str, str] | None = None) -> int:
    env = dict(env or os.environ)
    manifest = _read_json_env(env, 'GLASSLAB_RUNNER_MANIFEST_JSON')
    config = _read_json_env(env, 'GLASSLAB_GENERIC_CONFIG_JSON')
    run_id = str(
        env.get('GLASSLAB_RUNNER_EXPERIMENT_ID')
        or manifest.get('run_id')
        or ''
    ).strip()
    if not run_id:
        raise ValueError('run_id is required')
    # run_id becomes a single path component under artifacts_root; reject
    # anything that could escape the root or hide a dotfile.
    if not re.fullmatch(r'[A-Za-z0-9._-]+', run_id) or run_id.startswith('.'):
        raise ValueError(f'run_id is not a safe path component: {run_id}')

    artifacts_root = Path(env.get('GLASSLAB_RUNNER_ARTIFACTS_ROOT', '/mnt/artifacts'))
    dataset_root = Path(env.get('GLASSLAB_DATASET_ROOT', '/mnt/datasets'))
    workspace_root = Path(env.get('GLASSLAB_WORKSPACE_ROOT', '/work'))
    run_root = artifacts_root / run_id
    # run_root is both the workload's writable output directory and the home
    # of the authoritative terminal bundle, so evidence and records stay in
    # one per-run location.
    logs_dir = run_root / 'logs'
    logs_dir.mkdir(parents=True, exist_ok=True)
    runner_log = logs_dir / 'runner.log'

    workspace = config.get('workspace')
    if not isinstance(workspace, dict):
        raise ValueError('config_payload.workspace is required')
    command = workspace.get('command')
    if not isinstance(command, list) or not command or any(not str(item).strip() for item in command):
        raise ValueError('workspace.command must be a non-empty list')

    detail = 'workspace command completed'
    terminal_status = 'failed'
    try:
        task_dir = workspace_root / 'task'
        source_dir = workspace_root / 'source'
        task_asset = materialize_asset(
            workspace['task_bundle'],
            task_dir,
            dataset_root=dataset_root,
            artifacts_root=artifacts_root,
        )
        shutil.copy2(task_asset, run_root / 'task.zip')

        source_reference = workspace.get('source_bundle')
        execution_root = task_dir
        if isinstance(source_reference, dict):
            source_asset = materialize_asset(
                source_reference,
                source_dir,
                dataset_root=dataset_root,
                artifacts_root=artifacts_root,
            )
            shutil.copy2(source_asset, run_root / 'source.zip')
            execution_root = source_dir

        working_directory = str(workspace.get('working_directory', '.')).strip()
        cwd = (execution_root / working_directory).resolve()
        if not _is_within(cwd, [execution_root.resolve()]) or not cwd.is_dir():
            raise ValueError(f'workspace working_directory is invalid: {working_directory}')

        output_path = Path(str(workspace.get('output_directory', '/outputs')))
        if output_path != run_root:
            # Point the configured output path at run_root instead of copying,
            # so the workload writes straight into the authoritative run
            # directory. An existing path is only tolerated when it already
            # resolves to this run's root.
            if output_path.is_symlink() or output_path.exists():
                if output_path.resolve() != run_root.resolve():
                    raise ValueError(f'workspace output path already exists: {output_path}')
            else:
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.symlink_to(run_root, target_is_directory=True)

        resolved_bindings = _read_json_env(
            env,
            'GLASSLAB_GENERIC_DATASET_BINDINGS_JSON',
        )
        verify_dataset_bindings(
            config.get('dataset_contracts', []),
            resolved_bindings,
            dataset_root=dataset_root,
            artifacts_root=artifacts_root,
        )
        process_env = {
            **env,
            'GLASSLAB_TASK_DIR': str(task_dir),
            'GLASSLAB_SOURCE_DIR': str(source_dir if source_reference else task_dir),
            'GLASSLAB_OUTPUT_DIR': str(run_root),
            'GLASSLAB_DATASET_BINDINGS_JSON': json.dumps(
                resolved_bindings,
                sort_keys=True,
            ),
        }
        # The workload sees only resolved, read-only inputs plus its output
        # directory; no evaluation contract or rubric is exposed here, so
        # self-scoring cannot reference scoring inputs from this process.
        normalized_bindings: dict[str, str] = {}
        # Bindings are also exposed as an uppercase normalized env namespace;
        # the collision check keeps that namespace unambiguous.
        for name, path in resolved_bindings.items():
            normalized_name = ''.join(
                char if char.isalnum() else '_'
                for char in str(name).upper()
            )
            previous = normalized_bindings.get(normalized_name)
            if previous is not None:
                raise ValueError(
                    'dataset binding names collide after environment '
                    f'normalization: {previous}, {name}'
                )
            normalized_bindings[normalized_name] = str(name)
            process_env[f'GLASSLAB_DATASET_{normalized_name}'] = str(path)
            process_env[f'{normalized_name}_PATH'] = str(path)

        budget = manifest.get('budget', {})
        max_minutes = int(budget.get('max_wallclock_minutes', 0))
        if max_minutes < 1:
            raise ValueError('manifest budget.max_wallclock_minutes must be positive')
        timeout_seconds = max(1, max_minutes * 60 - 5)

        # Five seconds of headroom before the platform budget expires, so the
        # runner can still write the terminal bundle instead of being killed
        # mid-run.

        with runner_log.open('a', encoding='utf-8') as log_handle:
            log_handle.write('Executing frozen workspace command: ' + json.dumps(command) + '\n')
            process = subprocess.Popen(
                [str(item) for item in command],
                cwd=cwd,
                env=process_env,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            try:
                process.communicate(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                # The workload runs in its own session, so its process group
                # is exactly the workload tree; kill the group so forked
                # grandchildren cannot outlive the budget and keep writing
                # into run_root after the artifact index snapshot.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                process.wait()
                raise
            # The exit code is recorded and mapped to terminal status here,
            # and the bundle is written either way.
            log_handle.write(f'Workspace command exit code: {process.returncode}\n')
        if process.returncode != 0:
            detail = f'workspace command failed with exit code {process.returncode}'
        else:
            terminal_status = 'succeeded'
    except subprocess.TimeoutExpired:
        detail = 'workspace command exceeded its wall-clock budget'
        with runner_log.open('a', encoding='utf-8') as log_handle:
            log_handle.write(detail + '\n')
    except Exception as exc:
        detail = str(exc)
        with runner_log.open('a', encoding='utf-8') as log_handle:
            log_handle.write(traceback.format_exc())

    # Terminal records are written outside the try so even failures produce a
    # complete bundle; the exit code reflects the final terminal status.
    missing: list[str] = []
    try:
        missing, terminal_status, detail = _write_terminal_bundle(
            run_id=run_id,
            run_root=run_root,
            manifest=manifest,
            config=config,
            terminal_status=terminal_status,
            detail=detail,
        )
    finally:
        # status.json is the one record every consumer requires; it is written
        # unconditionally in a finally block so a bundle failure can never
        # suppress the run's terminal state.
        _write_status(run_root, run_id, terminal_status, detail)
    return 0 if terminal_status == 'succeeded' and not missing else 1


def main() -> int:
    return run_from_environment()


if __name__ == '__main__':
    raise SystemExit(main())
