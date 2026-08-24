"""Bounded agent-runtime replay facility.

Replays a frozen workspace-repair case against explicitly selected candidate
runtimes and scores each trial with the real deterministic preflight gate.
This module is standalone benchmark tooling: it never touches the orchestrator
store, engine, settings, workflow-api, or cluster execution paths.
"""

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

_TERMINAL_TOOL_STATUSES = {'completed', 'error'}

# Error-text fragments that indicate the RUNTIME rejected an unavailable or
# unknown tool name. Heuristic and deliberately narrow: a failed invocation of
# a valid tool must not be counted here. When no pattern matches,
# invalid_tool_call_count stays None (= unknown), never 0.
UNKNOWN_TOOL_PATTERNS: tuple[str, ...] = (
    'unknown tool',
    'invalid tool',
    'no such tool',
    'tool not found',
    'not a valid tool',
    'unrecognized tool',
)

_DB_LAYOUT_CANDIDATES: tuple[tuple[str, str], ...] = (
    ('xdg', 'data/opencode/opencode.db'),
    ('legacy', '.local/share/opencode/opencode.db'),
)


@dataclass(frozen=True)
class RawRunResult:
    exit_code: int | None
    wall_clock_seconds: float
    timed_out: bool


@dataclass(frozen=True)
class TrialUsage:
    model_request_count: int = 0
    tool_call_count: int = 0
    tool_error_count: int = 0
    invalid_tool_call_count: int | None = None
    tokens_input: int | None = None
    tokens_output: int | None = None
    provider_id: str | None = None
    model_id: str | None = None
    doom_loop_events: list[dict[str, Any]] = field(default_factory=list)


class RuntimeCandidateRunner(Protocol):
    def run(
        self,
        *,
        prompt: str,
        workspace: Path,
        home: Path,
        model_provider: str,
        model_name: str,
        timeout_seconds: int,
    ) -> RawRunResult:
        """Execute one candidate turn against an isolated workspace copy."""
        ...


def parse_opencode_usage(
    db_path: Path, doom_loop_threshold: int | None = None
) -> TrialUsage:
    """Extract usage metadata from a trial HOME's opencode sqlite database.

    Defensive by design: missing tables or absent token fields yield zeroed or
    None values rather than raising, because provider exposure of token counts
    is not guaranteed.
    """
    usage = TrialUsage()
    if not db_path.is_file():
        return usage
    con = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
    try:
        usage = _read_usage(con, doom_loop_threshold)
    finally:
        con.close()
    return usage


def _read_usage(
    con: sqlite3.Connection, doom_loop_threshold: int | None
) -> TrialUsage:
    tables = {
        row[0]
        for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    messages: list[dict[str, Any]] = []
    parts: list[dict[str, Any]] = []
    if 'message' in tables:
        for (data,) in con.execute('SELECT data FROM message'):
            try:
                messages.append(json.loads(data))
            except json.JSONDecodeError:
                continue
    if 'part' in tables:
        for (data,) in con.execute('SELECT data FROM part'):
            try:
                parts.append(json.loads(data))
            except json.JSONDecodeError:
                continue

    assistant = [m for m in messages if m.get('role') == 'assistant']
    provider_ids = {m.get('providerID') for m in assistant if m.get('providerID')}
    model_ids = {m.get('modelID') for m in assistant if m.get('modelID')}
    tokens_in: list[int] = []
    tokens_out: list[int] = []
    for m in assistant:
        tokens = m.get('tokens')
        if not isinstance(tokens, dict):
            continue
        for key, bucket in (('input', tokens_in), ('output', tokens_out)):
            value = tokens.get(key)
            if isinstance(value, int):
                bucket.append(value)

    terminal_tools = [
        p
        for p in parts
        if p.get('type') == 'tool'
        and isinstance(p.get('state'), dict)
        and p['state'].get('status') in _TERMINAL_TOOL_STATUSES
    ]
    error_tools = [p for p in terminal_tools if p['state'].get('status') == 'error']
    unknown_tool_hits = [
        p
        for p in error_tools
        if _matches_unknown_tool_pattern(p['state'].get('error'))
    ]

    return TrialUsage(
        model_request_count=len(assistant),
        tool_call_count=len(terminal_tools),
        tool_error_count=len(error_tools),
        invalid_tool_call_count=(
            len(unknown_tool_hits) if unknown_tool_hits else None
        ),
        tokens_input=sum(tokens_in) if tokens_in else None,
        tokens_output=sum(tokens_out) if tokens_out else None,
        provider_id=next(iter(provider_ids)) if len(provider_ids) == 1 else None,
        model_id=next(iter(model_ids)) if len(model_ids) == 1 else None,
        doom_loop_events=(
            count_doom_loop_events(parts, doom_loop_threshold)
            if doom_loop_threshold is not None
            else []
        ),
    )


def _matches_unknown_tool_pattern(error_text: Any) -> bool:
    if not isinstance(error_text, str):
        return False
    lowered = error_text.lower()
    return any(pattern in lowered for pattern in UNKNOWN_TOOL_PATTERNS)


@dataclass(frozen=True)
class TrialDatabase:
    path: Path
    layout: str


def find_trial_database(home: Path) -> TrialDatabase | None:
    """Locate the trial's opencode session database across known layouts.

    OpenCode versions differ on whether the session store follows XDG_DATA_HOME
    or the legacy ~/.local/share path; both are checked and the most recently
    modified database wins.
    """
    candidates: list[tuple[float, str, Path]] = []
    seen_inodes: set[tuple[int, int]] = set()
    for layout, relative in _DB_LAYOUT_CANDIDATES:
        path = home / relative
        if not path.is_file():
            continue
        stat = path.stat()
        key = (stat.st_dev, stat.st_ino)
        if key in seen_inodes:
            continue
        seen_inodes.add(key)
        candidates.append((stat.st_mtime, layout, path))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (-item[0], item[1]))
    _, layout, path = candidates[0]
    return TrialDatabase(path=path, layout=layout)


def _tool_signature(part: dict[str, Any]) -> str | None:
    state = part.get('state')
    if (
        part.get('type') != 'tool'
        or not isinstance(state, dict)
        or state.get('status') not in _TERMINAL_TOOL_STATUSES
    ):
        return None
    canonical = json.dumps(
        {'tool': part.get('tool'), 'input': state.get('input')},
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()


def count_doom_loop_events(
    parts: list[dict[str, Any]], threshold: int
) -> list[dict[str, Any]]:
    """Count runs of >= threshold consecutive identical terminal tool calls."""
    events: list[dict[str, Any]] = []
    streak: list[tuple[str, dict[str, Any]]] = []
    for part in parts:
        signature = _tool_signature(part)
        if signature is None:
            if len(streak) >= threshold:
                events.append(_streak_event(streak))
            streak = []
            continue
        if streak and streak[-1][0] == signature:
            streak.append((signature, part))
        else:
            if len(streak) >= threshold:
                events.append(_streak_event(streak))
            streak = [(signature, part)]
    if len(streak) >= threshold:
        events.append(_streak_event(streak))
    return events


def _streak_event(streak: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    last = streak[-1][1]
    state = last.get('state', {})
    return {
        'repeats': len(streak),
        'tool': last.get('tool'),
        'example_signature': streak[-1][0][:16],
    }


def prepare_trial_workspace(fixture_workspace: Path, destination: Path) -> Path:
    """Materialize one pristine per-trial copy of the frozen fixture workspace."""
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(fixture_workspace, destination)
    return destination


class OpenCodeCliRunner:
    """Invoke one candidate through the same `opencode run` CLI used live."""

    def __init__(self, opencode_bin: str = 'opencode') -> None:
        self._opencode_bin = opencode_bin

    def run(
        self,
        *,
        prompt: str,
        workspace: Path,
        home: Path,
        model_provider: str,
        model_name: str,
        timeout_seconds: int,
    ) -> RawRunResult:
        home.mkdir(parents=True, exist_ok=True)
        stdout_path = home / 'trial-stdout.txt'
        stderr_path = home / 'trial-stderr.txt'
        env_home = str(home.resolve())
        base_env = {
            key: value
            for key, value in (
                ('PATH', os.environ.get('PATH')),
                ('HOME', env_home),
                ('XDG_DATA_HOME', str(home / 'data')),
                ('XDG_CONFIG_HOME', str(home / 'config')),
            )
            if value is not None
        }
        argv = [
            self._opencode_bin,
            'run',
            '--model',
            f'{model_provider}/{model_name}',
            prompt,
        ]
        started = time.monotonic()
        try:
            completed = subprocess.run(
                argv,
                cwd=str(workspace),
                env=base_env,
                timeout=timeout_seconds,
                capture_output=True,
            )
            stdout_path.write_bytes(completed.stdout)
            stderr_path.write_bytes(completed.stderr)
            return RawRunResult(
                exit_code=completed.returncode,
                wall_clock_seconds=time.monotonic() - started,
                timed_out=False,
            )
        except subprocess.TimeoutExpired as exc:
            stdout_path.write_bytes(exc.stdout or b'')
            stderr_path.write_bytes(exc.stderr or b'')
            return RawRunResult(
                exit_code=None,
                wall_clock_seconds=time.monotonic() - started,
                timed_out=True,
            )


class ReplayFixtureError(RuntimeError):
    """Raised when committed fixture bytes drift from MANIFEST.json."""


@dataclass(frozen=True)
class ReplayCase:
    root: Path
    case_id: str

    def prompt_text(self) -> str:
        return (self.root / 'PROMPT.txt').read_text()

    def _workspace_template(self) -> Path:
        return self.root / 'workspace'

    def pristine_workspace(self) -> Path:
        base = Path(tempfile.mkdtemp(prefix='replay-pristine-'))
        return prepare_trial_workspace(self._workspace_template(), base / 'ws')

    def materialize_repaired_workspace(self, destination: Path) -> Path:
        workspace = prepare_trial_workspace(self._workspace_template(), destination)
        subprocess.run(
            ['git', 'apply', str(self.root / 'gold_repair.diff')],
            cwd=str(workspace),
            check=True,
            capture_output=True,
        )
        return workspace


@dataclass(frozen=True)
class CorrectnessVerdict:
    correct: bool
    preflight_errors: list[str] = field(default_factory=list)


def load_case(case_root: Path) -> ReplayCase:
    manifest_path = case_root / 'MANIFEST.json'
    manifest = json.loads(manifest_path.read_text())
    if manifest.get('schema_version') != 'glasslab-runtime-replay-fixture-v1':
        raise ReplayFixtureError('unsupported fixture schema_version')
    for relative, expected_sha in manifest['files'].items():
        path = case_root / relative
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != expected_sha:
            raise ReplayFixtureError(f'fixture drift detected: {relative}')
    return ReplayCase(root=case_root, case_id=manifest['case_id'])


def _contract_for_case(case: ReplayCase, workspace: Path):
    from app.schemas import EvaluationContractDescriptor, ResolvedEvaluationContract

    contract_json = (
        case.root / 'contract' / 'classification-metric-v1' / '1.0.0' / 'contract.json'
    ).read_bytes()
    return ResolvedEvaluationContract(
        descriptor=EvaluationContractDescriptor.model_validate_json(contract_json),
        digest=hashlib.sha256(contract_json).hexdigest(),
        root_path=str(workspace),
    )


def acceptance_gate(case: ReplayCase, workspace: Path):
    """Run the real deterministic preflight against one trial workspace."""
    from app.preflight import preflight_matrix
    from app.schemas import ExperimentMatrix, RunRecord

    matrix = ExperimentMatrix.model_validate(
        json.loads((case.root / 'matrix.json').read_text())
    )
    run = RunRecord.model_construct(beaker_workspace=str(workspace))
    return preflight_matrix(
        run=run,
        matrix=matrix,
        contract=_contract_for_case(case, workspace),
    )


def score_correctness(report: Any, case: ReplayCase) -> CorrectnessVerdict:
    """Independent correctness verdict derived only from the preflight report."""
    expected = json.loads((case.root / 'EXPECTED_PREFLIGHT.json').read_text())
    correct = bool(report.passed) and report.errors == []
    if correct:
        correct = (
            report.comparisons == expected['comparisons']
            and report.decisions == expected['decisions']
        )
    return CorrectnessVerdict(
        correct=correct,
        preflight_errors=[str(error) for error in report.errors],
    )


_TRIAL_SCHEMA_VERSION = 'glasslab-runtime-replay-observation-v1'


def run_campaign(
    *,
    case_root: Path,
    candidates: list[tuple[str, str]],
    repeats: int,
    runner_factory: Any,
    out_dir: Path,
    timeout_seconds: int,
) -> dict[str, Any]:
    """Execute bounded replays; write crash-safe JSONL rows plus a summary.

    The summary never declares a winner: correctness and latency are recorded
    per observation and aggregation is left to the reader.
    """
    case = load_case(case_root)
    out_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = out_dir / 'observations.jsonl'
    trials = 0
    for provider, model_name in candidates:
        candidate_label = f'{provider}/{model_name}'
        for repeat_index in range(repeats):
            home = out_dir / 'trials' / f'{provider}-{model_name}-{repeat_index}' / 'home'
            workspace = prepare_trial_workspace(
                case._workspace_template(),
                out_dir / 'trials' / f'{provider}-{model_name}-{repeat_index}' / 'ws',
            )
            runner = runner_factory(provider, model_name)
            raw = runner.run(
                prompt=case.prompt_text(),
                workspace=workspace,
                home=home,
                model_provider=provider,
                model_name=model_name,
                timeout_seconds=timeout_seconds,
            )
            usage = parse_opencode_usage(home / 'data' / 'opencode' / 'opencode.db')
            report = acceptance_gate(case, workspace)
            verdict = score_correctness(report, case)
            outcome = _terminal_outcome(raw, verdict)
            observation = {
                'schema_version': _TRIAL_SCHEMA_VERSION,
                'case_id': case.case_id,
                'candidate': candidate_label,
                'trial_index': repeat_index,
                'terminal_outcome': outcome,
                'exit_code': raw.exit_code,
                'timed_out': raw.timed_out,
                'wall_clock_seconds': round(raw.wall_clock_seconds, 3),
                'correctness_passed': verdict.correct,
                'preflight_errors': verdict.preflight_errors[:4],
                'doom_loop_event_count': len(usage.doom_loop_events),
                **_usage_fields(usage),
            }
            with jsonl_path.open('a', encoding='utf-8') as handle:
                handle.write(json.dumps(observation, sort_keys=True) + '\n')
            trials += 1
            if not (out_dir / 'trials').exists():
                continue
    summary = {
        'schema_version': 'glasslab-runtime-replay-summary-v1',
        'case_id': case.case_id,
        'candidates': [f'{provider}/{name}' for provider, name in candidates],
        'repeats': repeats,
        'trials': trials,
        'winner': None,
        'note': 'raw observations only; correctness and latency are reported separately',
    }
    (out_dir / 'summary.json').write_text(json.dumps(summary, indent=2, sort_keys=True) + '\n')
    return summary


def _usage_fields(usage: TrialUsage) -> dict[str, Any]:
    return {
        'model_request_count': usage.model_request_count,
        'tool_call_count': usage.tool_call_count,
        'tool_error_count': usage.tool_error_count,
        'invalid_tool_call_count': usage.invalid_tool_call_count,
        'tokens_input': usage.tokens_input,
        'tokens_output': usage.tokens_output,
        'provider_id': usage.provider_id,
        'model_id': usage.model_id,
    }


def _terminal_outcome(raw: RawRunResult, verdict: CorrectnessVerdict) -> str:
    if raw.timed_out:
        return 'timeout'
    if raw.exit_code not in (0, None):
        return 'runner_error'
    return 'accepted' if verdict.correct else 'rejected'
