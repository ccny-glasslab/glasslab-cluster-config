#!/usr/bin/env python3
"""Record per-turn-kind model evidence and regenerate the routing table (#433).

Scores the frozen fixtures in ``fixtures/model-routing/v1/fixtures.json``
against candidate models on an OpenAI-compatible endpoint, writes the recorded
pass rates to ``evidence.json``, and derives ``routing_table.json`` from them.
Both outputs are versioned and reviewed like any other code.

Live recording (from the service root, with network to the exo endpoints):

    PYTHONPATH=. python3 scripts/record_model_routing.py \
      --base-url http://192.168.1.17:52417/v1 \
      --models structured=mlx-community/Qwen3-Coder-Next-4bit,reasoning=mlx-community/Qwen3-Next-80B-A3B-Thinking-4bit \
      --turns 3

Offline replay (no network; reads recorded responses
``<replay-dir>/<fixture_id>__<role>__<index>.txt``):

    PYTHONPATH=. python3 scripts/record_model_routing.py --replay /path/to/corpus

The routing derivation itself is offline and deterministic; see
``app/model_routing.py`` and ``tests/test_model_routing.py``.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import urllib.request

from app import model_routing
from app.model_benchmark import BenchmarkRun, CompleteFn, record_measurements

STRUCTURED_MODEL = 'mlx-community/Qwen3-Coder-Next-4bit'
REASONING_MODEL = 'mlx-community/Qwen3-Next-80B-A3B-Thinking-4bit'


def _parse_models(raw: str) -> dict[str, str]:
    models: dict[str, str] = {}
    for entry in raw.split(','):
        entry = entry.strip()
        if not entry:
            continue
        if '=' not in entry:
            raise SystemExit(f'--models entry must be role=model: {entry!r}')
        role, model = entry.split('=', 1)
        role, model = role.strip(), model.strip()
        if role not in model_routing.ROLE_ORDER:
            raise SystemExit(
                f'unknown role {role!r}; expected one of '
                f'{", ".join(model_routing.ROLE_ORDER)}'
            )
        if not model:
            raise SystemExit(f'empty model for role {role!r}')
        models[role] = model
    if not models:
        raise SystemExit('--models must name at least one role=model pair')
    return models


def _live_complete(base_url: str, *, max_tokens: int, timeout: float) -> CompleteFn:
    def complete(model: str, fixture, role: str) -> str:
        body = json.dumps(
            {
                'model': model,
                'messages': [{'role': 'user', 'content': fixture.prompt}],
                'max_tokens': max_tokens,
                'stream': False,
            }
        ).encode()
        request = urllib.request.Request(
            base_url.rstrip('/') + '/chat/completions',
            data=body,
            headers={'Content-Type': 'application/json'},
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
        choices = payload.get('choices') or []
        if not choices:
            return ''
        return choices[0].get('message', {}).get('content') or ''

    return complete


def _replay_complete(directory: Path) -> CompleteFn:
    counters: dict[tuple[str, str], int] = {}

    def complete(model: str, fixture, role: str) -> str:
        key = (fixture.fixture_id, role)
        index = counters.get(key, 0)
        counters[key] = index + 1
        path = directory / f'{fixture.fixture_id}__{role}__{index}.txt'
        if not path.is_file():
            return ''
        return path.read_text()

    return complete


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixtures', default=str(model_routing.FIXTURES_PATH))
    parser.add_argument('--evidence-out', default=str(model_routing.EVIDENCE_PATH))
    parser.add_argument(
        '--routing-table-out', default=str(model_routing.ROUTING_TABLE_PATH)
    )
    parser.add_argument('--base-url', default='http://192.168.1.17:52417/v1')
    parser.add_argument(
        '--models',
        default=f'structured={STRUCTURED_MODEL},reasoning={REASONING_MODEL}',
    )
    parser.add_argument('--turns', type=int, default=3)
    parser.add_argument('--max-tokens', type=int, default=2048)
    parser.add_argument('--timeout', type=float, default=1800.0)
    parser.add_argument(
        '--replay',
        default=None,
        help='Read recorded responses from this directory instead of a live endpoint.',
    )
    parser.add_argument(
        '--source',
        default='record_model_routing.py live recording',
        help='Provenance string recorded on every measurement row.',
    )
    parser.add_argument('--recorded-at', default=None)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    fixtures_path = Path(args.fixtures)
    if not fixtures_path.is_file():
        raise SystemExit(f'fixtures not found: {fixtures_path}')
    fixtures = model_routing.load_fixtures(fixtures_path)
    models_by_role = _parse_models(args.models)

    if args.replay:
        complete = _replay_complete(Path(args.replay))
    else:
        complete = _live_complete(
            args.base_url, max_tokens=args.max_tokens, timeout=args.timeout
        )

    run = BenchmarkRun(
        models_by_role=models_by_role, turns=args.turns, source=args.source
    )
    measurements = record_measurements(fixtures, run, complete)

    recorded_at = args.recorded_at or datetime.now(timezone.utc).strftime(
        '%Y-%m-%d'
    )
    evidence = model_routing.EvidenceSet(
        schema_version='glasslab-model-routing-evidence-v1',
        recorded_at=recorded_at,
        harness='scripts/record_model_routing.py',
        fixtures_path='fixtures/model-routing/v1/fixtures.json',
        notes='Regenerated by scripts/record_model_routing.py.',
        measurements=measurements,
    )
    evidence_path = Path(args.evidence_out)
    evidence_path.write_text(
        json.dumps(evidence.model_dump(mode='json'), indent=2) + '\n'
    )

    table = model_routing.build_routing_table(
        measurements,
        derived_from=f'fixtures/model-routing/v1/{evidence_path.name}',
        generated_at=recorded_at,
    )
    table_path = Path(args.routing_table_out)
    table_path.write_text(
        json.dumps(table.model_dump(mode='json'), indent=2) + '\n'
    )

    print(f'wrote {evidence_path} ({len(measurements)} measurements)')
    print(f'wrote {table_path} ({len(table.routes)} routes)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
