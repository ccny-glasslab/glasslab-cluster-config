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

from app.runtime_replay import OpenCodeCliRunner, RawRunResult, load_case, run_campaign

BUNDLED_CASE = (
    Path(__file__).resolve().parents[1]
    / 'fixtures'
    / 'runtime-replay'
    / 'wine-classification-v1'
)


def default_runner_factory(model_provider: str, model_name: str) -> Any:
    return OpenCodeCliRunner()


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
        '--keep-workspaces',
        action='store_true',
        help='keep per-trial workspaces/HOMEs under out-dir (may contain session data)',
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

    factory = runner_factory or default_runner_factory
    summary = run_campaign(
        case_root=args.fixture,
        candidates=candidates,
        repeats=args.repeats,
        runner_factory=factory,
        out_dir=args.out_dir,
        timeout_seconds=args.timeout_seconds,
    )
    print(f'observations: {args.out_dir / "observations.jsonl"}')
    print(f'summary: winner={summary["winner"]} (never declared by this tool)')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
