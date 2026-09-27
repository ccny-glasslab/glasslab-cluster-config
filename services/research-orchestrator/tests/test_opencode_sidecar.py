"""Tests for the opencode sidecar broker and the remote process handle.

Issue #597: the agent runtime is forked by the uid-10002 opencode sidecar
container, not by the orchestrator. These tests pin the broker's validation
surface (it must refuse anything but the configured `opencode serve` argv, a
workspace-root cwd, and a secret-free environment) and the `_RemoteProcess`
handle the runtime uses to observe and stop the server.
"""

from __future__ import annotations

import os
import stat
import threading
from pathlib import Path

import httpx
import pytest

from app.config import Settings
from app.opencode_runtime import _RemoteProcess, OpenCodeProcessRuntime
from app.opencode_sidecar import (
    SidecarConfig,
    SidecarRequestError,
    ServerRegistry,
    create_server,
)
from app.process_permissions import (
    enable_shared_group_write,
    ensure_group_writable_tree,
    make_directory_group_writable,
)


def _fake_executable(tmp_path: Path) -> Path:
    script = tmp_path / 'fake-opencode'
    script.write_text('#!/bin/sh\nsleep 30\n', encoding='utf-8')
    script.chmod(0o755)
    return script


def _config(tmp_path: Path, executable: Path, port: int = 0) -> SidecarConfig:
    return SidecarConfig(
        host='127.0.0.1',
        port=port,
        executable=str(executable),
        workspace_root=tmp_path,
    )


def _spawn_request(tmp_path: Path, executable: Path) -> dict:
    workspace = tmp_path / 'run-1' / 'beaker-worktree'
    workspace.mkdir(parents=True, exist_ok=True)
    return {
        'argv': [
            str(executable),
            'serve',
            '--hostname',
            '127.0.0.1',
            '--port',
            '4242',
        ],
        'cwd': str(workspace),
        'env': {'PATH': '/usr/bin:/bin', 'HOME': str(tmp_path / 'home')},
        'log_path': str(tmp_path / 'opencode.log'),
    }


def test_broker_rejects_a_non_opencode_executable(tmp_path: Path) -> None:
    registry = ServerRegistry(_config(tmp_path, _fake_executable(tmp_path)))
    request = _spawn_request(tmp_path, _fake_executable(tmp_path))
    request['argv'][0] = '/usr/bin/env'
    with pytest.raises(SidecarRequestError):
        registry.spawn(request)


def test_broker_rejects_a_non_serve_subcommand(tmp_path: Path) -> None:
    executable = _fake_executable(tmp_path)
    registry = ServerRegistry(_config(tmp_path, executable))
    request = _spawn_request(tmp_path, executable)
    request['argv'][1] = 'run'
    with pytest.raises(SidecarRequestError):
        registry.spawn(request)


def test_broker_rejects_a_non_loopback_host(tmp_path: Path) -> None:
    executable = _fake_executable(tmp_path)
    registry = ServerRegistry(_config(tmp_path, executable))
    request = _spawn_request(tmp_path, executable)
    request['argv'][3] = '0.0.0.0'
    with pytest.raises(SidecarRequestError):
        registry.spawn(request)


def test_broker_rejects_a_cwd_outside_the_workspace_root(tmp_path: Path) -> None:
    executable = _fake_executable(tmp_path)
    registry = ServerRegistry(_config(tmp_path, executable))
    request = _spawn_request(tmp_path, executable)
    request['cwd'] = '/etc'
    with pytest.raises(SidecarRequestError):
        registry.spawn(request)


def test_broker_rejects_a_control_plane_env_key(tmp_path: Path) -> None:
    executable = _fake_executable(tmp_path)
    registry = ServerRegistry(_config(tmp_path, executable))
    request = _spawn_request(tmp_path, executable)
    request['env']['GLASSLAB_ORCHESTRATOR_OPERATOR_API_TOKEN'] = 'stolen'
    with pytest.raises(SidecarRequestError):
        registry.spawn(request)


def test_broker_spawns_and_terminates_the_process(tmp_path: Path) -> None:
    executable = _fake_executable(tmp_path)
    registry = ServerRegistry(_config(tmp_path, executable))
    spawned = registry.spawn(_spawn_request(tmp_path, executable))
    runtime_id = spawned['runtime_id']
    assert registry.status(runtime_id)['running'] is True
    assert registry.terminate(runtime_id)['running'] is False
    assert registry.status(runtime_id) is None
    registry.terminate_all()


@pytest.fixture()
def broker(tmp_path: Path):
    executable = _fake_executable(tmp_path)
    server = create_server(_config(tmp_path, executable, port=0))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f'http://127.0.0.1:{server.server_address[1]}'
    try:
        yield base_url, executable, tmp_path
    finally:
        server.shutdown()
        server.server_close()
        server.registry.terminate_all()
        thread.join(timeout=5)


def test_remote_process_tracks_and_stops_a_broker_child(broker) -> None:
    base_url, executable, tmp_path = broker
    request = _spawn_request(tmp_path, executable)
    response = httpx.post(f'{base_url}/v1/servers', json=request, timeout=5)
    response.raise_for_status()
    runtime_id = response.json()['runtime_id']

    process = _RemoteProcess(
        runtime_id=runtime_id, base_url=base_url, timeout=5
    )
    assert process.poll() is None
    process.terminate()
    assert process.wait(timeout=5) is not None
    assert process.poll() is not None


def test_runtime_sidecar_backend_delegates_the_exec(broker) -> None:
    base_url, executable, tmp_path = broker
    runtime = OpenCodeProcessRuntime(
        Settings(
            opencode_spawn_backend='sidecar',
            opencode_sidecar_url=base_url,
            opencode_sidecar_timeout_seconds=5,
            opencode_executable=str(executable),
        )
    )
    workspace = tmp_path / 'run-1' / 'honeydew-worktree'
    workspace.mkdir(parents=True, exist_ok=True)
    log_path = tmp_path / 'opencode.log'
    process = runtime._spawn_sidecar_server(
        argv=[
            str(executable),
            'serve',
            '--hostname',
            '127.0.0.1',
            '--port',
            '4242',
        ],
        cwd=workspace,
        environment={'PATH': '/usr/bin:/bin'},
        log_path=log_path,
    )
    assert isinstance(process, _RemoteProcess)
    assert process.poll() is None
    process.terminate()
    assert process.wait(timeout=5) is not None


def test_enable_shared_group_write_makes_new_dirs_group_writable(
    tmp_path: Path,
) -> None:
    previous = os.umask(0o022)
    try:
        enable_shared_group_write()
        created = tmp_path / 'runtime'
        created.mkdir()
        assert created.stat().st_mode & 0o070 == 0o070
    finally:
        os.umask(previous)


def test_make_directory_group_writable_adds_group_bits(tmp_path: Path) -> None:
    directory = tmp_path / 'runtime'
    directory.mkdir(mode=0o755)
    make_directory_group_writable(directory)
    assert directory.stat().st_mode & 0o070 == 0o070


def test_ensure_group_writable_tree_skips_symlinks(tmp_path: Path) -> None:
    target = tmp_path / 'target'
    target.mkdir()
    tree = tmp_path / 'tree'
    tree.mkdir()
    (tree / 'file.txt').write_text('x', encoding='utf-8')
    link = tree / 'link'
    link.symlink_to(target)
    target_before = stat.S_IMODE(target.stat().st_mode)
    ensure_group_writable_tree(tree)
    assert stat.S_IMODE(tree.stat().st_mode) & 0o070 == 0o070
    assert stat.S_IMODE((tree / 'file.txt').stat().st_mode) & 0o060 == 0o060
    assert link.is_symlink()
    assert stat.S_IMODE(target.stat().st_mode) == target_before
