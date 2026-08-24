#!/usr/bin/env python3
"""Bounded agent-runtime replay benchmark CLI.

Replays a frozen workspace-repair case against explicitly named candidate
runtimes. Never touches the orchestrator store/engine/settings or any cluster
execution path; candidates must be passed explicitly on the command line.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.runtime_replay import OpenCodeCliRunner, load_case, run_campaign

BUNDLED_CASE = (
    Path(__file__).resolve().parents[1]
    / 'fixtures'
    / 'runtime-replay'
    / 'wine-classification-v1'
)


def build_runner_factory(
    env_pass: list[str], seed_auth_file: Path | None
) -> Any:
    allowlist = frozenset(env_pass)

    def factory(model_provider: str, model_name: str) -> Any:
        return OpenCodeCliRunner(
            env_allowlist=allowlist,
            seed_auth_file=seed_auth_file,
        )

    return factory


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='replay-runtime-benchmark',
        description=(
            'Bounded agent-runtime replay: identical frozen task per candidate, '
            'correctness scored by the real deterministic preflight gate.'
        ),
    )
    parser.add_argument(
        '--fixture',
        type=Path,
        default=BUNDLED_CASE,
        help=f'frozen case directory (default: {BUNDLED_CASE})',
    )
    parser.add_argument(
        '--candidate',
        action='append',
        required=True,
        metavar='PROVIDER/MODEL',
        help='candidate runtime id, repeatable; e.g. exo/mlx-community/Qwen3-Coder-Next-4bit',
    )
    parser.add_argument('--repeats', type=int, default=1)
    parser.add_argument(
        '--out-dir', type=Path, required=True, help='directory for observations.jsonl and trials/'
    )
    parser.add_argument(
        '--timeout-seconds',
        type=int,
        required=True,
        help='per-trial wall-clock budget; trials exceeding it are marked timeout',
    )
    parser.add_argument(
        '--doom-loop-threshold',
        type=int,
        default=None,
        help='repeated-identical-tool-call threshold for doom_loop_event_count; '
        'if omitted the count is emitted as null (production orchestrator has used 6)',
    )
    parser.add_argument(
        '--env-pass',
        action='append',
        default=[],
        metavar='NAME',
        help='operator environment variable name to pass through to trials '
        '(deny-by-default; repeatable; never inherits the whole environment)',
    )
    parser.add_argument(
        '--seed-auth-file',
        type=Path,
        default=None,
        help='operator-supplied provider auth file copied into each trial HOME '
        '(explicit credential input; copies are removed with the trial directory)',
    )
    parser.add_argument(
        '--keep-workspaces',
        action='store_true',
        help='keep per-trial workspaces/HOMEs under out-dir (may contain session data and seeded auth)',
    )
    return parser


def main(argv: list[str] | None = None, runner_factory: Any = None) -> int:
    args = build_parser().parse_args(argv)
    if args.repeats < 1:
        raise SystemExit('--repeats must be >= 1')
    candidates: list[tuple[str, str]] = []
    for raw in args.candidate:
        provider, separator, model = raw.partition('/')
        if not separator or not provider or not model:
            raise SystemExit(f'--candidate must be PROVIDER/MODEL, got: {raw}')
        candidates.append((provider, model))

    if args.doom_loop_threshold is None:
        print(
            'warning: --doom-loop-threshold omitted; doom_loop_event_count '
            'will be null in observations'
        )
    if args.seed_auth_file is not None and not args.seed_auth_file.is_file():
        raise SystemExit(f'--seed-auth-file not found: {args.seed_auth_file}')
    factory = runner_factory or build_runner_factory(
        args.env_pass, args.seed_auth_file
    )
    summary = run_campaign(
        case_root=args.fixture,
        candidates=candidates,
        repeats=args.repeats,
        runner_factory=factory,
        out_dir=args.out_dir,
        timeout_seconds=args.timeout_seconds,
        doom_loop_threshold=args.doom_loop_threshold,
    )
    print(f'observations: {args.out_dir / "observations.jsonl"}')
    print(f'summary: winner={summary["winner"]} (never declared by this tool)')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
