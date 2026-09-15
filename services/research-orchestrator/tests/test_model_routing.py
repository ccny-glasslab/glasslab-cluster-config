"""Evidence-driven per-turn-kind model routing (issue #433).

Locks the routing contract end to end, all offline:

- the frozen fixtures cover every TurnKind;
- the recorded evidence covers every TurnKind;
- the committed routing table equals the deterministic derivation from the
  evidence (so the table can never silently drift from its evidence);
- the derivation prefers the higher recorded pass rate and breaks ties
  deterministically;
- Settings and the engine route turns through that table, including a mapping
  that deliberately differs from the legacy hard-coded split.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from app import model_routing
from app.config import Settings
from app.model_benchmark import BenchmarkRun, record_measurements
from app.model_routing import (
    REASONING_ROLE,
    ROLE_ORDER,
    STRUCTURED_ROLE,
    Measurement,
    TurnFixture,
    derive_routes,
    load_evidence,
    load_fixtures,
    load_routing_table,
    parse_envelope,
    score_envelope,
)
from app.schemas import AgentName, RunCreateRequest, TurnKind

STRUCTURED_MODEL = 'mlx-community/Coder-Next-4bit'
REASONING_MODEL = 'mlx-community/Thinking-4bit'


def _measurement(
    turn_kind: TurnKind,
    role: str,
    *,
    pass_rate: float,
    samples: int = 3,
) -> Measurement:
    return Measurement(
        turn_kind=turn_kind,
        role=role,
        model=f'{role}-model',
        samples=samples,
        passed=round(pass_rate * samples),
        pass_rate=pass_rate,
        source='unit-test',
    )


def _write_table(tmp_path: Path, routes: dict[str, str]) -> Path:
    path = tmp_path / f'table-{uuid4().hex}.json'
    path.write_text(
        json.dumps(
            {
                'schema_version': 'glasslab-model-routing-table-v1',
                'derived_from': 'unit-test',
                'generated_at': '2026-09-14',
                'tie_break_order': list(ROLE_ORDER),
                'routes': routes,
            }
        )
    )
    return path


def _fixture(
    turn_kind: TurnKind,
    *,
    required_fields: list[str] | None = None,
) -> TurnFixture:
    return TurnFixture(
        fixture_id=f'{turn_kind.value}-unit',
        turn_kind=turn_kind,
        agent='honeydew',
        context_profile='unit',
        prompt='do the thing',
        required_envelope_kind=turn_kind,
        required_envelope_fields=required_fields or [],
        source='unit-test',
    )


def test_fixtures_cover_every_turn_kind_with_one_each() -> None:
    fixtures = load_fixtures()
    by_kind = fixtures.by_turn_kind()
    assert set(by_kind) == set(TurnKind)
    assert len(fixtures.fixtures) == len(TurnKind)


def test_evidence_covers_every_turn_kind() -> None:
    evidence = load_evidence()
    covered = {m.turn_kind for m in evidence.measurements}
    assert covered == set(TurnKind)
    assert all(m.role in ROLE_ORDER for m in evidence.measurements)
    assert all(0.0 <= m.pass_rate <= 1.0 for m in evidence.measurements)


def test_committed_routing_table_equals_derived_evidence() -> None:
    evidence = load_evidence()
    expected = derive_routes(evidence.measurements)
    assert load_routing_table() == expected


def test_routing_table_covers_every_turn_kind_with_known_roles() -> None:
    table = load_routing_table()
    assert set(table) == set(TurnKind)
    assert set(table.values()) <= set(ROLE_ORDER)


def test_recorded_mapping_routes_verification_to_reasoning() -> None:
    table = load_routing_table()
    assert table[TurnKind.VERIFICATION] == REASONING_ROLE
    assert all(
        role == STRUCTURED_ROLE
        for kind, role in table.items()
        if kind is not TurnKind.VERIFICATION
    )


def test_derivation_prefers_higher_pass_rate() -> None:
    routes = derive_routes(
        [
            _measurement(TurnKind.PROTOCOL_DRAFT, STRUCTURED_ROLE, pass_rate=1.0),
            _measurement(TurnKind.PROTOCOL_DRAFT, REASONING_ROLE, pass_rate=0.5),
        ]
    )
    assert routes[TurnKind.PROTOCOL_DRAFT] == STRUCTURED_ROLE


def test_derivation_breaks_exact_ties_toward_structured() -> None:
    routes = derive_routes(
        [
            _measurement(TurnKind.VERIFICATION, REASONING_ROLE, pass_rate=1.0),
            _measurement(TurnKind.VERIFICATION, STRUCTURED_ROLE, pass_rate=1.0),
        ]
    )
    assert routes[TurnKind.VERIFICATION] == STRUCTURED_ROLE


def test_derivation_ignores_unmeasured_and_unknown_roles() -> None:
    routes = derive_routes(
        [
            _measurement(
                TurnKind.VERIFICATION,
                REASONING_ROLE,
                pass_rate=1.0,
                samples=0,
            ),
            _measurement(TurnKind.VERIFICATION, 'mystery', pass_rate=1.0),
        ]
    )
    assert TurnKind.VERIFICATION not in routes


def test_score_envelope_requires_kind_summary_and_fields() -> None:
    fixture = _fixture(TurnKind.VERIFICATION, required_fields=['verification_verdict'])
    valid, _ = score_envelope(
        fixture,
        {
            'kind': 'verification',
            'summary': 'ok',
            'verification_verdict': {'status': 'consistent'},
        },
    )
    assert valid is True

    wrong_kind, _ = score_envelope(
        fixture, {'kind': 'final_report', 'summary': 'ok'}
    )
    missing_field, _ = score_envelope(
        fixture, {'kind': 'verification', 'summary': 'ok'}
    )
    no_summary, _ = score_envelope(
        fixture,
        {'kind': 'verification', 'verification_verdict': {'status': 'x'}},
    )
    assert wrong_kind is False
    assert missing_field is False
    assert no_summary is False


def test_parse_envelope_recovers_fenced_and_prose_wrapped_json() -> None:
    fenced = '```json\n{"kind": "revision", "summary": "s"}\n```'
    prose = 'Here you go: {"kind": "revision", "summary": "s"} done'
    assert parse_envelope(fenced)['kind'] == 'revision'
    assert parse_envelope(prose)['kind'] == 'revision'
    assert parse_envelope('no json here') == {}


def test_record_measurements_scores_offline() -> None:
    fixtures = load_fixtures()
    run = BenchmarkRun(
        models_by_role={STRUCTURED_ROLE: STRUCTURED_MODEL},
        turns=1,
        source='unit-test',
    )

    def complete(model: str, fixture: TurnFixture, role: str) -> str:
        return json.dumps(
            {
                'kind': fixture.required_envelope_kind.value,
                'summary': 'ok',
                **{field: ['x'] for field in fixture.required_envelope_fields},
            }
        )

    measurements = record_measurements(fixtures, run, complete)
    assert {m.turn_kind for m in measurements} == set(TurnKind)
    assert all(m.pass_rate == 1.0 for m in measurements)


def test_record_measurements_counts_invalid_envelopes() -> None:
    fixtures = load_fixtures()
    run = BenchmarkRun(
        models_by_role={STRUCTURED_ROLE: STRUCTURED_MODEL},
        turns=2,
        source='unit-test',
    )

    def complete(model: str, fixture: TurnFixture, role: str) -> str:
        return 'not an envelope'

    measurements = record_measurements(fixtures, run, complete)
    assert all(m.pass_rate == 0.0 for m in measurements)
    assert all(m.samples == 2 for m in measurements)


def test_settings_route_by_recorded_table_not_hardcoded_default(
    tmp_path: Path,
) -> None:
    # Evidence says protocol_draft runs best on the reasoning model and
    # verification on the structured one, the opposite of the legacy split.
    table = _write_table(
        tmp_path,
        {
            TurnKind.PROTOCOL_DRAFT.value: REASONING_ROLE,
            TurnKind.VERIFICATION.value: STRUCTURED_ROLE,
        },
    )
    settings = Settings(
        model_routing_table_path=str(table),
        honeydew_structured_agent_model=STRUCTURED_MODEL,
        honeydew_reasoning_agent_model=REASONING_MODEL,
    )
    assert settings.honeydew_model_for(TurnKind.PROTOCOL_DRAFT) == (
        REASONING_MODEL,
        settings.honeydew_reasoning_base_url(),
    )
    assert settings.honeydew_model_for(TurnKind.VERIFICATION) == (
        STRUCTURED_MODEL,
        settings.honeydew_structured_base_url(),
    )


def test_settings_fall_back_to_legacy_split_when_table_missing(
    tmp_path: Path,
) -> None:
    settings = Settings(
        model_routing_table_path=str(tmp_path / 'absent.json'),
        honeydew_structured_agent_model=STRUCTURED_MODEL,
        honeydew_reasoning_agent_model=REASONING_MODEL,
    )
    assert settings.model_route(TurnKind.PROTOCOL_DRAFT) is None
    assert settings.honeydew_model_for(TurnKind.PROTOCOL_DRAFT)[0] == (
        STRUCTURED_MODEL
    )
    assert settings.honeydew_model_for(TurnKind.VERIFICATION)[0] == (
        REASONING_MODEL
    )


def test_beaker_routes_reasoning_only_when_evidence_says_so(
    tmp_path: Path,
) -> None:
    table = _write_table(
        tmp_path,
        {TurnKind.EXPERIMENT_ANALYSIS.value: REASONING_ROLE},
    )
    settings = Settings(
        model_routing_table_path=str(table),
        agent_model_beaker=STRUCTURED_MODEL,
        honeydew_reasoning_agent_model=REASONING_MODEL,
    )
    assert settings.beaker_model_for(TurnKind.EXPERIMENT_ANALYSIS)[0] == (
        REASONING_MODEL
    )
    assert settings.beaker_model_for(TurnKind.REVISION)[0] == STRUCTURED_MODEL


def test_engine_routes_honeydew_turn_through_recorded_table(
    orchestrator_bundle,
    tmp_path: Path,
) -> None:
    _, _, _, runtime, engine = orchestrator_bundle
    table = _write_table(
        tmp_path,
        {TurnKind.PROTOCOL_DRAFT.value: REASONING_ROLE},
    )
    engine.settings = engine.settings.model_copy(
        update={
            'model_routing_table_path': str(table),
            'honeydew_structured_agent_model': STRUCTURED_MODEL,
            'honeydew_reasoning_agent_model': REASONING_MODEL,
        }
    )
    engine.create_run(RunCreateRequest(objective='Route by evidence.'))
    assert (AgentName.HONEYDEW, REASONING_MODEL) in runtime.model_overrides


def test_routing_module_paths_are_versioned() -> None:
    for path in (
        model_routing.FIXTURES_PATH,
        model_routing.EVIDENCE_PATH,
        model_routing.ROUTING_TABLE_PATH,
    ):
        assert 'model-routing/v1' in path.as_posix()
        assert path.is_file()
