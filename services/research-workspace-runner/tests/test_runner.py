from __future__ import annotations

from hashlib import sha256
import json
import os
from pathlib import Path
import sys
import time
import zipfile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from runner import run_from_environment


def _zip(path: Path, files: dict[str, str]) -> str:
    with zipfile.ZipFile(path, 'w') as handle:
        for name, content in files.items():
            handle.writestr(name, content)
    return sha256(path.read_bytes()).hexdigest()


def _environment(tmp_path: Path, *, source_digest: str) -> dict[str, str]:
    dataset_root = tmp_path / 'datasets'
    artifacts_root = tmp_path / 'artifacts'
    task_path = dataset_root / 'task.zip'
    source_path = dataset_root / 'source.zip'
    task_digest = _zip(task_path, {'problem.md': '# Test problem\n'})
    if not source_path.exists():
        raise AssertionError('source fixture must be created first')

    manifest = {
        'run_id': 'run-1',
        'budget': {'max_wallclock_minutes': 1},
        'expected_artifacts': {
            'required': [
                'run_manifest.json',
                'config.json',
                'metrics.json',
                'artifacts_index.json',
                'report.md',
                'status.json',
                'logs/',
                'source.zip',
            ],
            'optional': [],
        },
    }
    config = {
        'workspace': {
            'task_bundle': {
                'uri': 's3://datasets/task.zip',
                'sha256': task_digest,
            },
            'source_bundle': {
                'uri': 's3://datasets/source.zip',
                'sha256': source_digest,
            },
            'working_directory': '.',
            'command': [sys.executable, 'run.py'],
            'output_directory': str(tmp_path / 'outputs'),
        }
    }
    return {
        'GLASSLAB_RUNNER_MANIFEST_JSON': json.dumps(manifest),
        'GLASSLAB_GENERIC_CONFIG_JSON': json.dumps(config),
        'GLASSLAB_GENERIC_DATASET_BINDINGS_JSON': '{}',
        'GLASSLAB_RUNNER_EXPERIMENT_ID': 'run-1',
        'GLASSLAB_RUNNER_ARTIFACTS_ROOT': str(artifacts_root),
        'GLASSLAB_DATASET_ROOT': str(dataset_root),
        'GLASSLAB_WORKSPACE_ROOT': str(tmp_path / 'work'),
    }


def test_verified_workspace_executes_and_writes_complete_bundle(tmp_path: Path) -> None:
    dataset_root = tmp_path / 'datasets'
    dataset_root.mkdir(parents=True)
    source_path = dataset_root / 'source.zip'
    source_digest = _zip(
        source_path,
        {
            'run.py': (
                'import json, os\n'
                'from pathlib import Path\n'
                'out = Path(os.environ["GLASSLAB_OUTPUT_DIR"])\n'
                '(out / "metrics.json").write_text(json.dumps({"rubric_score": 91}))\n'
                '(out / "report.md").write_text("# Verified report\\n")\n'
            )
        },
    )

    result = run_from_environment(_environment(tmp_path, source_digest=source_digest))

    run_root = tmp_path / 'artifacts' / 'run-1'
    assert result == 0
    assert json.loads((run_root / 'status.json').read_text())['status'] == 'succeeded'
    assert json.loads((run_root / 'metrics.json').read_text())['rubric_score'] == 91
    assert (run_root / 'source.zip').is_file()
    assert (run_root / 'logs' / 'runner.log').is_file()


def test_digest_mismatch_fails_before_workspace_execution(tmp_path: Path) -> None:
    dataset_root = tmp_path / 'datasets'
    dataset_root.mkdir(parents=True)
    source_path = dataset_root / 'source.zip'
    _zip(source_path, {'run.py': 'raise SystemExit("must not execute")\n'})

    result = run_from_environment(_environment(tmp_path, source_digest='0' * 64))

    run_root = tmp_path / 'artifacts' / 'run-1'
    assert result == 1
    status = json.loads((run_root / 'status.json').read_text())
    assert status['status'] == 'failed'
    assert 'digest mismatch' in status['detail']


def test_dataset_digest_is_verified_before_workspace_execution(tmp_path: Path) -> None:
    dataset_root = tmp_path / 'datasets'
    dataset_root.mkdir(parents=True)
    source_path = dataset_root / 'source.zip'
    source_digest = _zip(source_path, {'run.py': 'raise SystemExit("must not execute")\n'})
    dataset_path = dataset_root / 'adult.data'
    dataset_path.write_text('sample row\n')
    env = _environment(tmp_path, source_digest=source_digest)
    config = json.loads(env['GLASSLAB_GENERIC_CONFIG_JSON'])
    config['dataset_contracts'] = [
        {
            'name': 'adult_train',
            'asset': {
                'uri': 's3://datasets/adult.data',
                'sha256': 'f' * 64,
            },
        }
    ]
    env['GLASSLAB_GENERIC_CONFIG_JSON'] = json.dumps(config)
    env['GLASSLAB_GENERIC_DATASET_BINDINGS_JSON'] = json.dumps(
        {'adult_train': str(dataset_path)}
    )

    result = run_from_environment(env)

    assert result == 1
    status = json.loads(
        (tmp_path / 'artifacts' / 'run-1' / 'status.json').read_text()
    )
    assert 'dataset digest mismatch for adult_train' in status['detail']


def test_dataset_bindings_expose_canonical_and_path_compatibility_env(
    tmp_path: Path,
) -> None:
    dataset_root = tmp_path / 'datasets'
    dataset_root.mkdir(parents=True)
    train_path = dataset_root / 'adult.data'
    test_path = dataset_root / 'adult.test'
    train_path.write_text('train row\n')
    test_path.write_text('test row\n')
    source_path = dataset_root / 'source.zip'
    source_digest = _zip(
        source_path,
        {
            'run.py': (
                'import json, os\n'
                'from pathlib import Path\n'
                'bindings = json.loads('
                'os.environ["GLASSLAB_DATASET_BINDINGS_JSON"])\n'
                'assert os.environ["ADULT_TRAIN_PATH"] == '
                'bindings["adult_train"]\n'
                'assert os.environ["ADULT_TEST_PATH"] == '
                'bindings["adult_test"]\n'
                'assert os.environ["GLASSLAB_DATASET_ADULT_TRAIN"] == '
                'bindings["adult_train"]\n'
                'out = Path(os.environ["GLASSLAB_OUTPUT_DIR"])\n'
                '(out / "metrics.json").write_text('
                'json.dumps({"rubric_score": 100}))\n'
                '(out / "report.md").write_text("# Compatible\\n")\n'
            )
        },
    )
    env = _environment(tmp_path, source_digest=source_digest)
    config = json.loads(env['GLASSLAB_GENERIC_CONFIG_JSON'])
    config['dataset_contracts'] = [
        {
            'name': name,
            'asset': {
                'uri': f's3://datasets/{path.name}',
                'sha256': sha256(path.read_bytes()).hexdigest(),
            },
        }
        for name, path in (
            ('adult_train', train_path),
            ('adult_test', test_path),
        )
    ]
    env['GLASSLAB_GENERIC_CONFIG_JSON'] = json.dumps(config)
    env['GLASSLAB_GENERIC_DATASET_BINDINGS_JSON'] = json.dumps(
        {
            'adult_train': str(train_path),
            'adult_test': str(test_path),
        }
    )

    result = run_from_environment(env)

    run_root = tmp_path / 'artifacts' / 'run-1'
    assert result == 0
    assert json.loads((run_root / 'status.json').read_text())['status'] == (
        'succeeded'
    )


def test_symlink_cannot_satisfy_required_workspace_artifact(tmp_path: Path) -> None:
    dataset_root = tmp_path / 'datasets'
    dataset_root.mkdir(parents=True)
    source_path = dataset_root / 'source.zip'
    source_digest = _zip(
        source_path,
        {
            'run.py': (
                'import os\n'
                'from pathlib import Path\n'
                'out = Path(os.environ["GLASSLAB_OUTPUT_DIR"])\n'
                '(out / "metrics.json").write_text("{}")\n'
                '(out / "report.md").symlink_to("/etc/hosts")\n'
            )
        },
    )

    result = run_from_environment(_environment(tmp_path, source_digest=source_digest))

    run_root = tmp_path / 'artifacts' / 'run-1'
    assert result == 1
    status = json.loads((run_root / 'status.json').read_text())
    assert status['status'] == 'failed'
    assert 'report.md' in status['detail']
    index = json.loads((run_root / 'artifacts_index.json').read_text())
    assert all(item['name'] != 'report.md' for item in index['artifacts'])


def test_fifo_cannot_satisfy_required_workspace_artifact(tmp_path: Path) -> None:
    dataset_root = tmp_path / 'datasets'
    dataset_root.mkdir(parents=True)
    source_path = dataset_root / 'source.zip'
    source_digest = _zip(
        source_path,
        {
            'run.py': (
                'import os\n'
                'from pathlib import Path\n'
                'out = Path(os.environ["GLASSLAB_OUTPUT_DIR"])\n'
                '(out / "metrics.json").write_text("{}")\n'
                'os.mkfifo(out / "report.md")\n'
            )
        },
    )

    result = run_from_environment(_environment(tmp_path, source_digest=source_digest))

    run_root = tmp_path / 'artifacts' / 'run-1'
    assert result == 1
    status = json.loads((run_root / 'status.json').read_text())
    assert status['status'] == 'failed'
    assert 'report.md' in status['detail']


def test_unreadable_workload_file_still_yields_terminal_bundle(tmp_path: Path) -> None:
    dataset_root = tmp_path / 'datasets'
    dataset_root.mkdir(parents=True)
    source_path = dataset_root / 'source.zip'
    source_digest = _zip(
        source_path,
        {
            'run.py': (
                'import json, os\n'
                'from pathlib import Path\n'
                'out = Path(os.environ["GLASSLAB_OUTPUT_DIR"])\n'
                '(out / "metrics.json").write_text('
                'json.dumps({"rubric_score": 95}))\n'
                '(out / "report.md").write_text("# Report\\n")\n'
                'locked = out / "locked.bin"\n'
                'locked.write_bytes(b"secret")\n'
                'locked.chmod(0)\n'
            )
        },
    )

    result = run_from_environment(_environment(tmp_path, source_digest=source_digest))

    run_root = tmp_path / 'artifacts' / 'run-1'
    assert result == 0
    assert json.loads((run_root / 'status.json').read_text())['status'] == (
        'succeeded'
    )
    index = json.loads((run_root / 'artifacts_index.json').read_text())
    locked = next(
        item for item in index['artifacts'] if item['name'] == 'locked.bin'
    )
    assert locked['unhashable'] is True
    assert locked['sha256'] is None


def test_run_id_escape_rejected(tmp_path: Path) -> None:
    dataset_root = tmp_path / 'datasets'
    dataset_root.mkdir(parents=True)
    source_path = dataset_root / 'source.zip'
    source_digest = _zip(
        source_path,
        {'run.py': 'raise SystemExit("must not execute")\n'},
    )

    for bad_run_id in ('../x', '/abs', '.hidden'):
        env = _environment(tmp_path, source_digest=source_digest)
        env['GLASSLAB_RUNNER_EXPERIMENT_ID'] = bad_run_id
        with pytest.raises(ValueError):
            run_from_environment(env)


def _is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A zombie cannot execute or write; treat it as dead.
    try:
        state = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[0]
    except (OSError, IndexError):
        return False
    return state != 'Z'


def test_timeout_kills_grandchild_process_group(tmp_path: Path) -> None:
    dataset_root = tmp_path / 'datasets'
    dataset_root.mkdir(parents=True)
    source_path = dataset_root / 'source.zip'
    source_digest = _zip(
        source_path,
        {
            'run.py': (
                'import os, subprocess, time\n'
                'from pathlib import Path\n'
                'out = Path(os.environ["GLASSLAB_OUTPUT_DIR"])\n'
                'grandchild = subprocess.Popen(\n'
                '    ["nohup", "sleep", "300"],\n'
                '    stdout=subprocess.DEVNULL,\n'
                '    stderr=subprocess.DEVNULL,\n'
                ')\n'
                '(out / "grandchild.pid").write_text(str(grandchild.pid))\n'
                'time.sleep(300)\n'
            )
        },
    )

    result = run_from_environment(_environment(tmp_path, source_digest=source_digest))

    run_root = tmp_path / 'artifacts' / 'run-1'
    grandchild_pid = int((run_root / 'grandchild.pid').read_text())
    assert result == 1
    status = json.loads((run_root / 'status.json').read_text())
    assert status['status'] == 'failed'
    assert 'wall-clock budget' in status['detail']
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if not _is_running(grandchild_pid):
            break
        time.sleep(0.2)
    else:
        pytest.fail('grandchild survived the wall-clock budget')
