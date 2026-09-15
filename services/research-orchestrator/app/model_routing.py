"""Evidence-driven per-turn-kind model routing (issue #433).

The split-serving topology has two co-resident models: a structured/coding
model on `.17` and a reasoning model on `.18`. Which one serves a turn must be
a recorded decision, not a hand-coded per-agent constant.

This module owns the recorded domain:

- the frozen per-turn-kind fixtures (`fixtures.json`),
- the recorded pass rates (`evidence.json`), and
- the derived routing table (`routing_table.json`) that maps every
  :class:`TurnKind` to the role that scored best.

Routing is deterministic: :func:`derive_routes` picks, per turn kind, the
measured role with the highest ``(pass_rate, samples)`` rank; ties fall back to
the documented :data:`ROLE_ORDER` (structured first). No model server or
cluster is contacted here, so the routing table and its tests run offline.

The recording harness in ``scripts/record_model_routing.py`` regenerates the
evidence and the routing table; see ``fixtures/model-routing/v1/README.md``.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
import re

from pydantic import BaseModel, ConfigDict, Field

from .schemas import TurnKind

_SERVICE_ROOT = Path(__file__).resolve().parents[1]
_FIXTURE_ROOT = _SERVICE_ROOT / 'fixtures' / 'model-routing' / 'v1'
FIXTURES_PATH = _FIXTURE_ROOT / 'fixtures.json'
EVIDENCE_PATH = _FIXTURE_ROOT / 'evidence.json'
ROUTING_TABLE_PATH = _FIXTURE_ROOT / 'routing_table.json'

# The two roles are the configured structured/reasoning model slots; the
# routing decision never hard-codes a model id.
STRUCTURED_ROLE = 'structured'
REASONING_ROLE = 'reasoning'
ROLE_ORDER: tuple[str, ...] = (STRUCTURED_ROLE, REASONING_ROLE)


class TurnFixture(BaseModel):
    """One frozen, stable input for a turn kind."""

    model_config = ConfigDict(extra='forbid')

    fixture_id: str = Field(min_length=1)
    turn_kind: TurnKind
    agent: str = Field(min_length=1)
    context_profile: str = Field(min_length=1)
    prompt: str = Field(min_length=1)
    required_envelope_kind: TurnKind
    required_envelope_fields: list[str] = Field(default_factory=list)
    source: str = Field(min_length=1)


class FixtureSet(BaseModel):
    model_config = ConfigDict(extra='forbid')

    schema_version: str
    description: str = ''
    fixtures: list[TurnFixture] = Field(min_length=1)

    def by_turn_kind(self) -> dict[TurnKind, TurnFixture]:
        return {fixture.turn_kind: fixture for fixture in self.fixtures}


class Measurement(BaseModel):
    """A recorded pass rate for one (turn kind, model role) pair."""

    model_config = ConfigDict(extra='forbid')

    turn_kind: TurnKind
    role: str
    model: str = Field(min_length=1)
    samples: int = Field(ge=0)
    passed: int = Field(ge=0)
    pass_rate: float = Field(ge=0, le=1)
    source: str = Field(min_length=1)


class EvidenceSet(BaseModel):
    model_config = ConfigDict(extra='forbid')

    schema_version: str
    recorded_at: str
    harness: str
    fixtures_path: str
    notes: str = ''
    measurements: list[Measurement] = Field(min_length=1)


class RoutingTable(BaseModel):
    model_config = ConfigDict(extra='forbid')

    schema_version: str
    derived_from: str
    generated_at: str
    tie_break_order: list[str]
    routes: dict[TurnKind, str]


def derive_routes(measurements: list[Measurement]) -> dict[TurnKind, str]:
    """Map every measured turn kind to its best-scoring role.

    Rank is ``(pass_rate, samples, -ROLE_ORDER.index(role))`` so a higher pass
    rate wins, then more samples, then the earlier role in :data:`ROLE_ORDER`
    (structured) on an exact tie. Roles outside :data:`ROLE_ORDER` are ignored.
    """
    best_rank: dict[TurnKind, tuple[float, int, int]] = {}
    winner: dict[TurnKind, str] = {}
    for measurement in measurements:
        if measurement.samples <= 0 or measurement.role not in ROLE_ORDER:
            continue
        rank = (
            measurement.pass_rate,
            measurement.samples,
            -ROLE_ORDER.index(measurement.role),
        )
        if measurement.turn_kind not in best_rank or rank > best_rank[measurement.turn_kind]:
            best_rank[measurement.turn_kind] = rank
            winner[measurement.turn_kind] = measurement.role
    return winner


def build_routing_table(
    measurements: list[Measurement],
    *,
    derived_from: str,
    generated_at: str,
) -> RoutingTable:
    return RoutingTable(
        schema_version='glasslab-model-routing-table-v1',
        derived_from=derived_from,
        generated_at=generated_at,
        tie_break_order=list(ROLE_ORDER),
        routes=derive_routes(measurements),
    )


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text())


@lru_cache(maxsize=8)
def load_fixtures(path: str | Path = FIXTURES_PATH) -> FixtureSet:
    return FixtureSet.model_validate(_load_json(Path(path)))


@lru_cache(maxsize=8)
def load_evidence(path: str | Path = EVIDENCE_PATH) -> EvidenceSet:
    return EvidenceSet.model_validate(_load_json(Path(path)))


@lru_cache(maxsize=8)
def load_routing_table(path: str | Path = ROUTING_TABLE_PATH) -> dict[TurnKind, str]:
    """Return the recorded routing table, or ``{}`` if it is absent/invalid.

    A missing or malformed table never raises: callers fall back to the legacy
    structured/reasoning split, so a bad config path cannot strand a run.
    """
    try:
        table = RoutingTable.model_validate(_load_json(Path(path)))
    except (OSError, ValueError):
        return {}
    return dict(table.routes)


def route_for_turn(
    turn_kind: TurnKind,
    *,
    table_path: str | Path = ROUTING_TABLE_PATH,
) -> str | None:
    """The recorded role for ``turn_kind``, or ``None`` when unrecorded."""
    role = load_routing_table(table_path).get(turn_kind)
    return role if role in ROLE_ORDER else None


def parse_envelope(text: str) -> dict:
    """Extract the outermost JSON object carrying a ``kind`` key.

    Models wrap the ``AgentTurnResult`` envelope in prose or markdown fences;
    this recovers it without a schema import so the recorder and its tests stay
    dependency-light.
    """
    stripped = re.sub(r'^```(?:json)?|```$', '', text.strip(), flags=re.M)
    start = stripped.find('{')
    if start == -1:
        return {}
    depth = 0
    in_string = False
    escape = False
    for index in range(start, len(stripped)):
        char = stripped[index]
        if in_string:
            if escape:
                escape = False
            elif char == '\\':
                escape = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char == '{':
            depth += 1
        elif char == '}':
            depth -= 1
            if depth == 0:
                candidate = stripped[start : index + 1]
                try:
                    parsed = json.loads(candidate)
                except json.JSONDecodeError:
                    return {}
                return parsed if isinstance(parsed, dict) else {}
    return {}


def score_envelope(fixture: TurnFixture, envelope: dict) -> tuple[bool, str]:
    """Score one model response envelope against a fixture's contract.

    Valid means the response parsed to an object whose ``kind`` equals the
    fixture's required kind, whose ``summary`` is non-empty, and which carries
    every required variant field.
    """
    if not envelope:
        return False, 'no AgentTurnResult envelope parsed'
    if envelope.get('kind') != fixture.required_envelope_kind.value:
        return False, f"kind {envelope.get('kind')!r} != {fixture.required_envelope_kind.value!r}"
    if not isinstance(envelope.get('summary'), str) or not envelope['summary']:
        return False, 'summary missing'
    for field in fixture.required_envelope_fields:
        if not envelope.get(field):
            return False, f'required field {field!r} missing'
    return True, ''
