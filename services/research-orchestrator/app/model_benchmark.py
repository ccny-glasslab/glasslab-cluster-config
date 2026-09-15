"""Offline-scorable per-turn-kind benchmark recorder (issue #433).

Scores candidate models on the frozen turn-kind fixtures and turns the raw
responses into :class:`~app.model_routing.Measurement` records. The model call
is injected as a :data:`CompleteFn`, so the recording logic is fully testable
without a model server: tests pass a fake completion, the CLI passes a live
exo/OpenAI-compatible call or a replay-corpus reader.

Network I/O lives in ``scripts/record_model_routing.py``; this module has no
network or cluster dependency.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from .model_routing import (
    FixtureSet,
    Measurement,
    TurnFixture,
    parse_envelope,
    score_envelope,
)
from .schemas import TurnKind

# (model, fixture, role) -> raw response text.
CompleteFn = Callable[[str, TurnFixture, str], str]


@dataclass(frozen=True)
class BenchmarkRun:
    """One recording session: which models to score, how many turns, source."""

    models_by_role: Mapping[str, str]
    turns: int
    source: str


def record_measurements(
    fixtures: FixtureSet,
    run: BenchmarkRun,
    complete: CompleteFn,
) -> list[Measurement]:
    """Record pass rates for every (fixture, role) pair.

    A fixture is scored once per turn by :func:`score_envelope`; the pass rate
    is the fraction of turns that produced an envelope meeting the fixture's
    contract. Fixtures sharing a turn kind are aggregated.
    """
    totals: dict[tuple[TurnKind, str], list[int]] = {}
    for fixture in fixtures.fixtures:
        for role, model in run.models_by_role.items():
            key = (fixture.turn_kind, role)
            bucket = totals.setdefault(key, [0, 0])
            for _ in range(run.turns):
                response = complete(model, fixture, role)
                passed, _ = score_envelope(
                    fixture, parse_envelope(response)
                )
                bucket[0] += int(passed)
                bucket[1] += 1
    measurements: list[Measurement] = []
    for (turn_kind, role), (passed, samples) in totals.items():
        model = run.models_by_role[role]
        measurements.append(
            Measurement(
                turn_kind=turn_kind,
                role=role,
                model=model,
                samples=samples,
                passed=passed,
                pass_rate=(passed / samples) if samples else 0.0,
                source=run.source,
            )
        )
    return measurements
