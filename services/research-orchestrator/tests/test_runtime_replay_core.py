"""Tests for runtime replay metadata capture, isolation, and campaign plumbing."""

import json
import sqlite3
from pathlib import Path

import pytest

from app.runtime_replay import (
    TrialUsage,
    count_doom_loop_events,
    find_trial_database,
    prepare_trial_workspace,
    parse_opencode_usage,
)
from app.runtime_replay import OpenCodeCliRunner
from app.runtime_replay import _usage_fields


def _write_db(path: Path, messages: list[dict], parts: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute('CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, data TEXT)')
    con.execute('CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT, data TEXT)')
    for m in messages:
        con.execute('INSERT INTO message VALUES (?, ?, ?)', (m['id'], 's1', json.dumps(m)))
    for p in parts:
        con.execute('INSERT INTO part VALUES (?, ?, ?, ?)', (p['id'], p.get('message_id', 'm1'), 's1', json.dumps(p)))
    con.commit()
    con.close()


def _tool_part(part_id: str, tool: str, status: str = 'completed', **input_keys: str) -> dict:
    return {
        'id': part_id,
        'type': 'tool',
        'tool': tool,
        'state': {'status': status, 'input': input_keys or {'path': f'{tool}.txt'}},
    }


def test_metadata_parser_counts_requests_tools_and_invalid_calls(tmp_path: Path) -> None:
    db = tmp_path / 'opencode.db'
    messages = [
        {'id': 'u1', 'role': 'user'},
        {'id': 'a1', 'role': 'assistant', 'tokens': {'input': 100, 'output': 50}, 'providerID': 'exo', 'modelID': 'Qwen3-Coder-Next-4bit'},
        {'id': 'a2', 'role': 'assistant', 'tokens': {'input': 200, 'output': 80}, 'providerID': 'exo', 'modelID': 'Qwen3-Coder-Next-4bit'},
    ]
    parts = [
        {**_tool_part('t1', 'read'), 'message_id': 'a1'},
        {**_tool_part('t2', 'write'), 'message_id': 'a1'},
        {**_tool_part('t3', 'bash', status='error'), 'message_id': 'a2'},
    ]
    _write_db(db, messages, parts)

    usage = parse_opencode_usage(db)
    assert usage.model_request_count == 2
    assert usage.tool_call_count == 3
    assert usage.tool_error_count == 1
    assert usage.invalid_tool_call_count is None
    assert usage.tokens_input == 300
    assert usage.tokens_output == 130
    assert usage.provider_id == 'exo'
    assert usage.model_id == 'Qwen3-Coder-Next-4bit'


def test_metadata_parser_survives_missing_tables_and_absent_tokens(tmp_path: Path) -> None:
    db = tmp_path / 'empty.db'
    db.write_bytes(b'')
    con = sqlite3.connect(db)
    con.execute('CREATE TABLE other (x TEXT)')
    con.commit()
    con.close()

    usage = parse_opencode_usage(db)
    assert usage.model_request_count == 0
    assert usage.tool_call_count == 0
    assert usage.invalid_tool_call_count is None
    assert usage.tool_error_count == 0
    assert usage.tokens_input is None
    assert usage.tokens_output is None


def test_doom_loop_threshold_flags_identical_terminal_signatures() -> None:
    threshold = 4
    same = [_tool_part(f't{i}', 'bash', command='same') for i in range(threshold)]
    events = count_doom_loop_events(same, threshold)
    assert len(events) == 1

    below = same[:-1]
    assert count_doom_loop_events(below, threshold) == []

    interleaved = []
    for i in range(threshold):
        interleaved.append(_tool_part(f'a{i}', 'bash', command='same'))
        interleaved.append(_tool_part(f'b{i}', 'read', path='x'))
    assert count_doom_loop_events(interleaved, threshold) == []


def test_doom_loop_ignores_non_terminal_parts() -> None:
    pending = [
        {'id': f'p{i}', 'type': 'tool', 'tool': 'bash',
         'state': {'status': 'pending', 'input': {'command': 'same'}}}
        for i in range(6)
    ]
    assert count_doom_loop_events(pending, 4) == []


def test_trial_workspace_starts_pristine_per_repeat(tmp_path: Path) -> None:
    fixture_ws = tmp_path / 'fixture-ws'
    (fixture_ws / 'configs').mkdir(parents=True)
    (fixture_ws / 'configs' / 'candidate.yaml').write_text('name: frozen\n')

    first = prepare_trial_workspace(fixture_ws, tmp_path / 'trial-a')
    (first / 'outputs').mkdir()
    (first / 'outputs' / 'answer.txt').write_text('candidate A debris')

    second = prepare_trial_workspace(fixture_ws, tmp_path / 'trial-b')
    assert not (second / 'outputs').exists()
    assert (second / 'configs' / 'candidate.yaml').read_text() == 'name: frozen\n'
    assert (first / 'outputs' / 'answer.txt').exists()


def test_two_candidate_trials_are_isolated(tmp_path: Path) -> None:
    fixture_ws = tmp_path / 'fixture-ws'
    (fixture_ws / 'configs').mkdir(parents=True)
    (fixture_ws / 'configs' / 'candidate.yaml').write_text('name: frozen\n')

    ws_a = prepare_trial_workspace(fixture_ws, tmp_path / 'trial-a')
    ws_b = prepare_trial_workspace(fixture_ws, tmp_path / 'trial-b')
    (ws_a / 'contamination.txt').write_text('from candidate A')

    assert not (ws_b / 'contamination.txt').exists()
    assert not (fixture_ws / 'contamination.txt').exists()


def test_opencode_cli_runner_builds_isolated_invocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded: dict[str, object] = {}

    def fake_run(argv, **kwargs):
        recorded['argv'] = argv
        recorded['cwd'] = kwargs.get('cwd')
        recorded['env_home'] = kwargs['env']['HOME']
        recorded['timeout'] = kwargs.get('timeout')
        return subprocess_completed(returncode=0)

    monkeypatch.setattr('app.runtime_replay.subprocess.run', fake_run)
    runner = OpenCodeCliRunner(opencode_bin='opencode')
    result = runner.run(
        prompt='fix the config',
        workspace=tmp_path / 'ws',
        home=tmp_path / 'home',
        model_provider='exo',
        model_name='Qwen3-Coder-Next-4bit',
        timeout_seconds=120,
    )

    assert result.exit_code == 0
    argv = recorded['argv']
    assert argv[:3] == ['opencode', 'run', '--model']
    assert argv[3] == 'exo/Qwen3-Coder-Next-4bit'
    assert Path(str(recorded['cwd'])) == tmp_path / 'ws'
    assert Path(str(recorded['env_home'])) == tmp_path / 'home'
    assert recorded['timeout'] == 120


def subprocess_completed(returncode: int):
    import types

    done = types.SimpleNamespace(returncode=returncode, stdout=b'', stderr=b'')
    return done


def test_load_case_verifies_manifest_identity(tmp_path: Path) -> None:
    import shutil

    from app.runtime_replay import ReplayFixtureError, load_case

    fixture_root = Path(__file__).resolve().parents[1] / 'fixtures' / 'runtime-replay' / 'wine-classification-v1'
    case = load_case(fixture_root)
    assert case.case_id == 'wine-classification-v1'

    tampered = tmp_path / 'tampered'
    shutil.copytree(fixture_root, tampered)
    manifest_path = tampered / 'MANIFEST.json'
    manifest = json.loads(manifest_path.read_text())
    manifest['files']['PROMPT.txt'] = '0' * 64
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ReplayFixtureError):
        load_case(tampered)


def test_acceptance_gate_uses_real_preflight_on_fixture(tmp_path: Path) -> None:
    from app.runtime_replay import acceptance_gate, load_case

    fixture_root = Path(__file__).resolve().parents[1] / 'fixtures' / 'runtime-replay' / 'wine-classification-v1'
    case = load_case(fixture_root)
    pre = acceptance_gate(case, case.pristine_workspace())
    assert pre.passed is False

    repaired = case.materialize_repaired_workspace(tmp_path / 'gold')
    post = acceptance_gate(case, repaired)
    assert post.passed is True


def test_scorer_is_independent_of_latency(tmp_path: Path) -> None:
    from app.runtime_replay import acceptance_gate, load_case, score_correctness

    fixture_root = Path(__file__).resolve().parents[1] / 'fixtures' / 'runtime-replay' / 'wine-classification-v1'
    case = load_case(fixture_root)

    wrong_ws = case.pristine_workspace()
    right_ws = case.materialize_repaired_workspace(tmp_path / 'right')

    slow_but_right = score_correctness(acceptance_gate(case, right_ws), case)
    fast_but_wrong = score_correctness(acceptance_gate(case, wrong_ws), case)

    assert slow_but_right.correct is True
    assert fast_but_wrong.correct is False
    assert not hasattr(slow_but_right, 'wall_clock_seconds')


def test_campaign_records_observations_and_no_winner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import time as time_module

    from app.runtime_replay import run_campaign

    fixture_root = Path(__file__).resolve().parents[1] / 'fixtures' / 'runtime-replay' / 'wine-classification-v1'

    class ScriptedRunner:
        def __init__(self, behavior: str) -> None:
            self._behavior = behavior

        def run(self, *, prompt, workspace, home, model_provider, model_name, timeout_seconds):
            if self._behavior == 'repair':
                import subprocess
                subprocess.run(['git', 'apply', str(fixture_root / 'gold_repair.diff')], cwd=workspace, check=True)
                time_module.sleep(0.05)
                return types_simple(returncode=0, seconds=2.0)
            if self._behavior == 'wrong':
                (workspace / 'configs' / 'candidate.yaml').write_text('name: broken-harder\n')
                return types_simple(returncode=0, seconds=0.01)
            return types_simple(returncode=7, seconds=1.0)

    def types_simple(*, returncode: int, seconds: float):
        import types
        return types.SimpleNamespace(_fake='RawRunResult', returncode=returncode, seconds=seconds)

    from app.runtime_replay import RawRunResult

    class AdapterRunner:
        def __init__(self, behavior: str) -> None:
            self._behavior = behavior

        def run(self, *, prompt, workspace, home, model_provider, model_name, timeout_seconds):
            scripted = ScriptedRunner(self._behavior).run(
                prompt=prompt,
                workspace=workspace,
                home=home,
                model_provider=model_provider,
                model_name=model_name,
                timeout_seconds=timeout_seconds,
            )
            return RawRunResult(
                exit_code=scripted.returncode,
                wall_clock_seconds=scripted.seconds,
                timed_out=False,
            )

    def runner_factory(model_provider: str, model_name: str):
        if 'Qwen' in model_name:
            return AdapterRunner('repair')
        return AdapterRunner('wrong')

    out_dir = tmp_path / 'out'
    summary = run_campaign(
        case_root=fixture_root,
        candidates=[('exo', 'Qwen3-Coder-Next-4bit'), ('exo', 'WrongModel')],
        repeats=1,
        runner_factory=runner_factory,
        out_dir=out_dir,
        timeout_seconds=60,
    )
    jsonl_lines = (out_dir / 'observations.jsonl').read_text().strip().splitlines()
    assert len(jsonl_lines) == 2
    rows = [json.loads(line) for line in jsonl_lines]
    by_model = {row['model_id'] or row['candidate']: row for row in rows}
    qwen_row = next(r for r in rows if 'Qwen' in r['candidate'])
    wrong_row = next(r for r in rows if 'WrongModel' in r['candidate'])
    assert qwen_row['correctness_passed'] is True
    assert qwen_row['wall_clock_seconds'] >= 0.05
    assert wrong_row['correctness_passed'] is False
    assert wrong_row['terminal_outcome'] == 'rejected'
    assert summary['winner'] is None
    assert summary['trials'] == 2


def test_cli_requires_explicit_candidates_and_paths(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import importlib

    cli = importlib.import_module('scripts.replay-runtime-benchmark')

    with pytest.raises(SystemExit):
        cli.main(['--out-dir', str(tmp_path), '--timeout-seconds', '30'])
    with pytest.raises(SystemExit):
        cli.main(['--candidate', 'exo/model', '--timeout-seconds', '30'])
    with pytest.raises(SystemExit):
        cli.main(['--candidate', 'exo/model', '--out-dir', str(tmp_path)])
    with pytest.raises(SystemExit):
        cli.main(
            [
                '--candidate',
                'no-slash-model',
                '--out-dir',
                str(tmp_path),
                '--timeout-seconds',
                '30',
            ]
        )


def test_cli_end_to_end_uses_real_preflight_on_fixture(tmp_path: Path) -> None:
    import importlib

    from app.runtime_replay import RawRunResult

    fixture_root = Path(__file__).resolve().parents[1] / 'fixtures' / 'runtime-replay' / 'wine-classification-v1'
    cli = importlib.import_module('scripts.replay-runtime-benchmark')

    def fake_factory(model_provider: str, model_name: str):
        class _Runner:
            def run(self, *, prompt, workspace, home, model_provider, model_name, timeout_seconds):
                if 'Qwen' in model_name:
                    import subprocess
                    subprocess.run(
                        ['git', 'apply', str(fixture_root / 'gold_repair.diff')],
                        cwd=workspace,
                        check=True,
                    )
                    return RawRunResult(exit_code=0, wall_clock_seconds=2.0, timed_out=False)
                (workspace / 'configs' / 'candidate.yaml').write_text('name: still-broken\n')
                return RawRunResult(exit_code=0, wall_clock_seconds=0.01, timed_out=False)

        return _Runner()

    out_dir = tmp_path / 'cli-out'
    code = cli.main(
        [
            '--fixture',
            str(fixture_root),
            '--candidate',
            'exo/Qwen3-Coder-Next-4bit',
            '--candidate',
            'exo/WrongModel',
            '--repeats',
            '2',
            '--out-dir',
            str(out_dir),
            '--timeout-seconds',
            '60',
        ],
        runner_factory=fake_factory,
    )
    assert code == 0
    rows = [json.loads(line) for line in (out_dir / 'observations.jsonl').read_text().strip().splitlines()]
    assert len(rows) == 4
    qwen = [r for r in rows if 'Qwen' in r['candidate']]
    wrong = [r for r in rows if 'WrongModel' in r['candidate']]
    assert all(r['correctness_passed'] is True for r in qwen)
    assert all(r['correctness_passed'] is False for r in wrong)
    assert {r['trial_index'] for r in rows} == {0, 1}


def test_metadata_parser_distinguishes_tool_error_from_unknown_tool(tmp_path: Path) -> None:
    db = tmp_path / 'opencode.db'
    parts = [
        _tool_part(
            't1',
            'grep',
            status='error',
        ),
    ]
    # valid tool, execution failure: error text carries no unknown-tool signal
    parts[0]['state']['error'] = 'ripgrep execution failed'
    _write_db(db, [{'id': 'a1', 'role': 'assistant'}], parts)

    usage = parse_opencode_usage(db)
    assert usage.tool_error_count == 1
    assert usage.invalid_tool_call_count is None


def test_invalid_tool_call_count_counts_only_matching_parts(tmp_path: Path) -> None:
    db = tmp_path / 'opencode.db'
    unknown = _tool_part('t1', 'run', status='error')
    unknown['state']['error'] = 'Unknown tool: run'
    exec_fail = _tool_part('t2', 'bash', status='error')
    exec_fail['state']['error'] = 'exit code 1'
    another_exec_fail = _tool_part('t3', 'grep', status='error')
    another_exec_fail['state']['error'] = 'ripgrep execution failed'
    _write_db(db, [{'id': 'a1', 'role': 'assistant'}], [unknown, exec_fail, another_exec_fail])

    usage = parse_opencode_usage(db)
    assert usage.tool_error_count == 3
    assert usage.invalid_tool_call_count == 1


def test_unknown_tool_patterns_match_case_insensitively(tmp_path: Path) -> None:
    db = tmp_path / 'opencode.db'
    part = _tool_part('t1', 'todo', status='error')
    part['state']['error'] = 'No Such Tool: todo'
    _write_db(db, [{'id': 'a1', 'role': 'assistant'}], [part])

    usage = parse_opencode_usage(db)
    assert usage.tool_error_count == 1
    assert usage.invalid_tool_call_count == 1


def test_usage_fields_emit_v2_schema_keys(tmp_path: Path) -> None:
    from app.runtime_replay import _usage_fields

    fields = _usage_fields(TrialUsage())
    assert set(fields) == {
        'model_request_count',
        'tool_call_count',
        'tool_error_count',
        'invalid_tool_call_count',
        'tokens_input',
        'tokens_output',
        'provider_id',
        'model_id',
    }


def test_find_trial_database_prefers_newest_across_layouts(tmp_path: Path) -> None:
    import os

    xdg = tmp_path / 'data' / 'opencode' / 'opencode.db'
    legacy = tmp_path / '.local' / 'share' / 'opencode' / 'opencode.db'
    xdg.parent.mkdir(parents=True)
    legacy.parent.mkdir(parents=True)
    xdg.write_bytes(b'newer')
    legacy.write_bytes(b'older')
    older_ts = os.path.getmtime(xdg) - 100
    os.utime(xdg, (older_ts, older_ts))

    found = find_trial_database(tmp_path)
    assert found is not None
    assert found.layout == 'legacy'

    newer_ts = os.path.getmtime(legacy) + 100
    os.utime(xdg, (newer_ts, newer_ts))
    found = find_trial_database(tmp_path)
    assert found is not None
    assert found.layout == 'xdg'


def test_find_trial_database_returns_none_when_absent(tmp_path: Path) -> None:
    assert find_trial_database(tmp_path) is None


def test_parse_opencode_usage_reads_legacy_layout_copy(tmp_path: Path) -> None:
    home = tmp_path / 'home'
    db = home / '.local' / 'share' / 'opencode' / 'opencode.db'
    _write_db(db, [{'id': 'a1', 'role': 'assistant', 'tokens': {'input': 5, 'output': 7}}], [])
    found = find_trial_database(home)
    assert found is not None and found.layout == 'legacy'
    usage = parse_opencode_usage(found.path)
    assert usage.model_request_count == 1


def test_cli_runner_env_denies_unlisted_variables_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv('MY_SECRET_TOKEN', 'leak-me')
    monkeypatch.setenv('OPENCODE_API_KEY', 'pass-me')
    recorded: dict[str, object] = {}

    def fake_run(argv, **kwargs):
        recorded['env'] = kwargs['env']
        return subprocess_completed(returncode=0)

    monkeypatch.setattr('app.runtime_replay.subprocess.run', fake_run)
    runner = OpenCodeCliRunner()
    runner.run(
        prompt='p',
        workspace=tmp_path / 'ws',
        home=tmp_path / 'home',
        model_provider='exo',
        model_name='m',
        timeout_seconds=10,
    )
    env = recorded['env']
    assert 'MY_SECRET_TOKEN' not in env
    assert 'OPENCODE_API_KEY' not in env


def test_cli_runner_env_allowlist_passes_exact_name_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv('MY_SECRET_TOKEN', 'leak-me')
    monkeypatch.setenv('OPENCODE_API_KEY', 'pass-me')
    recorded: dict[str, object] = {}

    def fake_run(argv, **kwargs):
        recorded['env'] = kwargs['env']
        return subprocess_completed(returncode=0)

    monkeypatch.setattr('app.runtime_replay.subprocess.run', fake_run)
    runner = OpenCodeCliRunner(env_allowlist=frozenset({'OPENCODE_API_KEY'}))
    runner.run(
        prompt='p',
        workspace=tmp_path / 'ws',
        home=tmp_path / 'home',
        model_provider='exo',
        model_name='m',
        timeout_seconds=10,
    )
    env = recorded['env']
    assert env.get('OPENCODE_API_KEY') == 'pass-me'
    assert 'MY_SECRET_TOKEN' not in env


def test_cli_runner_rejects_reserved_env_keys() -> None:
    with pytest.raises(ValueError):
        OpenCodeCliRunner(env_allowlist=frozenset({'HOME'}))
    with pytest.raises(ValueError):
        OpenCodeCliRunner(env_allowlist=frozenset({'PATH'}))
    with pytest.raises(ValueError):
        OpenCodeCliRunner(env_allowlist=frozenset({'XDG_DATA_HOME'}))


def test_seed_auth_file_copies_to_both_layouts_with_0600(tmp_path: Path) -> None:
    from app.runtime_replay import seed_trial_auth

    auth = tmp_path / 'operator-auth.json'
    auth.write_text('{"opencode-go": {"type": "api", "key": "k"}}')
    home = tmp_path / 'trial-home'

    seed_trial_auth(home, auth)

    for relative in (
        '.local/share/opencode/auth.json',
        'data/opencode/auth.json',
    ):
        seeded = home / relative
        assert seeded.is_file(), relative
        assert seeded.read_text() == auth.read_text()
        assert (seeded.stat().st_mode & 0o777) == 0o600
    assert (home / '.local/share/opencode').stat().st_mode & 0o777 == 0o700


def test_cleanup_of_trial_home_removes_seeded_auth(tmp_path: Path) -> None:
    import shutil

    from app.runtime_replay import seed_trial_auth

    auth = tmp_path / 'operator-auth.json'
    auth.write_text('{}')
    home = tmp_path / 'trial-home'
    seed_trial_auth(home, auth)
    shutil.rmtree(home)
    assert not home.exists()


def test_campaign_emits_null_doom_loop_count_without_threshold(
    tmp_path: Path,
) -> None:
    from app.runtime_replay import RawRunResult, load_case, run_campaign

    fixture_root = Path(__file__).resolve().parents[1] / 'fixtures' / 'runtime-replay' / 'wine-classification-v1'
    case = load_case(fixture_root)

    class NullRunner:
        def run(self, *, prompt, workspace, home, model_provider, model_name, timeout_seconds):
            return RawRunResult(exit_code=0, wall_clock_seconds=0.01, timed_out=False)

    summary = run_campaign(
        case_root=fixture_root,
        candidates=[('exo', 'm')],
        repeats=1,
        runner_factory=lambda p, n: NullRunner(),
        out_dir=tmp_path / 'out',
        timeout_seconds=30,
        doom_loop_threshold=None,
    )
    row = json.loads((tmp_path / 'out' / 'observations.jsonl').read_text().strip())
    assert row['doom_loop_event_count'] is None
    assert row['doom_loop_threshold'] is None
    assert 'threshold not specified' in row['notes']
    assert summary['winner'] is None


def test_campaign_records_threshold_next_to_count(tmp_path: Path) -> None:
    from app.runtime_replay import RawRunResult, run_campaign

    fixture_root = Path(__file__).resolve().parents[1] / 'fixtures' / 'runtime-replay' / 'wine-classification-v1'

    class LoopRunner:
        def run(self, *, prompt, workspace, home, model_provider, model_name, timeout_seconds):
            db = home / '.local' / 'share' / 'opencode' / 'opencode.db'
            _write_db(db, [{'id': 'a1', 'role': 'assistant'}], [
                _tool_part(f't{i}', 'bash', command='same') for i in range(5)
            ])
            return RawRunResult(exit_code=0, wall_clock_seconds=0.01, timed_out=False)

    out = tmp_path / 'out2'
    run_campaign(
        case_root=fixture_root,
        candidates=[('exo', 'm')],
        repeats=1,
        runner_factory=lambda p, n: LoopRunner(),
        out_dir=out,
        timeout_seconds=30,
        doom_loop_threshold=6,
    )
    row = json.loads((out / 'observations.jsonl').read_text().strip())
    assert row['doom_loop_threshold'] == 6
    assert row['doom_loop_event_count'] == 0
    assert row['session_db_layout'] == 'legacy'


def test_campaign_parses_legacy_layout_session_db(tmp_path: Path) -> None:
    from app.runtime_replay import RawRunResult, run_campaign

    fixture_root = Path(__file__).resolve().parents[1] / 'fixtures' / 'runtime-replay' / 'wine-classification-v1'

    class DbRunner:
        def run(self, *, prompt, workspace, home, model_provider, model_name, timeout_seconds):
            db = home / '.local' / 'share' / 'opencode' / 'opencode.db'
            _write_db(db, [{'id': 'a1', 'role': 'assistant', 'tokens': {'input': 11, 'output': 3}}], [])
            return RawRunResult(exit_code=0, wall_clock_seconds=0.01, timed_out=False)

    out = tmp_path / 'out3'
    run_campaign(
        case_root=fixture_root,
        candidates=[('exo', 'm')],
        repeats=1,
        runner_factory=lambda p, n: DbRunner(),
        out_dir=out,
        timeout_seconds=30,
        doom_loop_threshold=None,
    )
    row = json.loads((out / 'observations.jsonl').read_text().strip())
    assert row['model_request_count'] == 1
    assert row['tokens_input'] == 11
    assert row['session_db_layout'] == 'legacy'


def test_campaign_survives_missing_session_db_with_nulls(tmp_path: Path) -> None:
    from app.runtime_replay import RawRunResult, run_campaign

    fixture_root = Path(__file__).resolve().parents[1] / 'fixtures' / 'runtime-replay' / 'wine-classification-v1'

    class NoDbRunner:
        def run(self, *, prompt, workspace, home, model_provider, model_name, timeout_seconds):
            return RawRunResult(exit_code=0, wall_clock_seconds=0.01, timed_out=False)

    out = tmp_path / 'out4'
    run_campaign(
        case_root=fixture_root,
        candidates=[('exo', 'm')],
        repeats=1,
        runner_factory=lambda p, n: NoDbRunner(),
        out_dir=out,
        timeout_seconds=30,
        doom_loop_threshold=None,
    )
    row = json.loads((out / 'observations.jsonl').read_text().strip())
    assert row['model_request_count'] is None
    assert row['tool_call_count'] is None
    assert row['session_db_layout'] is None
    assert 'usage unmeasured: no session database found' in row['notes']
    assert row['correctness_passed'] is False


def test_schema_version_is_v2_in_campaign_rows(tmp_path: Path) -> None:
    from app.runtime_replay import RawRunResult, run_campaign

    fixture_root = Path(__file__).resolve().parents[1] / 'fixtures' / 'runtime-replay' / 'wine-classification-v1'

    class QuietRunner:
        def run(self, *, prompt, workspace, home, model_provider, model_name, timeout_seconds):
            return RawRunResult(exit_code=0, wall_clock_seconds=0.01, timed_out=False)

    out = tmp_path / 'out5'
    run_campaign(
        case_root=fixture_root,
        candidates=[('exo', 'm')],
        repeats=1,
        runner_factory=lambda p, n: QuietRunner(),
        out_dir=out,
        timeout_seconds=30,
    )
    row = json.loads((out / 'observations.jsonl').read_text().strip())
    assert row['schema_version'] == 'glasslab-runtime-replay-observation-v2'


def test_cli_threads_threshold_env_and_auth(tmp_path: Path) -> None:
    import importlib

    from app.runtime_replay import RawRunResult

    fixture_root = Path(__file__).resolve().parents[1] / 'fixtures' / 'runtime-replay' / 'wine-classification-v1'
    cli = importlib.import_module('scripts.replay-runtime-benchmark')
    seen: dict[str, object] = {}

    def fake_factory_builder(env_pass, seed_auth_file):
        seen['env_pass'] = env_pass
        seen['seed_auth_file'] = seed_auth_file

        def factory(provider: str, name: str):
            class _R:
                def run(self, *, prompt, workspace, home, model_provider, model_name, timeout_seconds):
                    return RawRunResult(exit_code=0, wall_clock_seconds=0.01, timed_out=False)
            return _R()

        return factory

    auth = tmp_path / 'auth.json'
    auth.write_text('{}')
    code = cli.main(
        [
            '--fixture', str(fixture_root),
            '--candidate', 'exo/m',
            '--out-dir', str(tmp_path / 'o'),
            '--timeout-seconds', '30',
            '--doom-loop-threshold', '6',
            '--env-pass', 'OPENCODE_API_KEY',
            '--seed-auth-file', str(auth),
        ],
        runner_factory=fake_factory_builder('OPENCODE_API_KEY', auth),
    )
    assert code == 0
    row = json.loads((tmp_path / 'o' / 'observations.jsonl').read_text().strip())
    assert row['doom_loop_threshold'] == 6


def test_cli_warns_when_threshold_omitted(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import importlib

    from app.runtime_replay import RawRunResult

    fixture_root = Path(__file__).resolve().parents[1] / 'fixtures' / 'runtime-replay' / 'wine-classification-v1'
    cli = importlib.import_module('scripts.replay-runtime-benchmark')

    def factory(provider: str, name: str):
        class _R:
            def run(self, *, prompt, workspace, home, model_provider, model_name, timeout_seconds):
                return RawRunResult(exit_code=0, wall_clock_seconds=0.01, timed_out=False)
        return _R()

    cli.main(
        [
            '--fixture', str(fixture_root),
            '--candidate', 'exo/m',
            '--out-dir', str(tmp_path / 'o2'),
            '--timeout-seconds', '30',
        ],
        runner_factory=factory,
    )
    assert 'doom-loop-threshold omitted' in capsys.readouterr().out
