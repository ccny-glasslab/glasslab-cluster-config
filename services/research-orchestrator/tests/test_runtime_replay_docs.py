"""Guards over committed runtime-replay evidence and documentation wording."""

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
DOCS = REPO_ROOT / 'docs' / 'glasslab-v2'
OBSERVATIONS = (
    DOCS / 'runtime-replay' / 'wine-classification-v1-run98-observations.jsonl'
)
REPORT = DOCS / 'runtime-replay-report.md'

BANNED_PHRASES = (
    '~2 minutes',
    'two minutes',
    '25 min',
    '~25 minute',
    'transport stall',
    'transport reliability problem',
)


def test_committed_observations_are_v2_golden() -> None:
    rows = [
        json.loads(line)
        for line in OBSERVATIONS.read_text().splitlines()
        if line.strip()
    ]
    assert len(rows) == 3
    for row in rows:
        assert row['schema_version'] == 'glasslab-runtime-replay-observation-v2'
    manual = [row for row in rows if row['capture_mode'].startswith('manual')]
    for row in manual:
        # manual captures recorded no session database: usage stays unknown
        assert row['model_request_count'] is None
        assert row['tool_call_count'] is None
        assert row['tool_error_count'] is None
        assert row['invalid_tool_call_count'] is None
        assert row['doom_loop_event_count'] is None
        assert row['doom_loop_threshold'] is None
    qwen = next(r for r in rows if r['candidate'] == 'exo/mlx-community/Qwen3-Coder-Next-4bit')
    ox = next(r for r in rows if r['capture_mode'].startswith('manual') and r['candidate'] == 'opencode-go/ox-alpha-free')
    assert qwen['wall_clock_seconds'] == 1560.0
    assert qwen['timed_out'] is True
    assert qwen['correctness_passed'] is True
    assert qwen['revision_cycles'] is None
    assert ox['wall_clock_seconds'] == 285.0
    assert ox['timed_out'] is False
    assert ox['correctness_passed'] is True
    assert ox['revision_cycles'] == 1
    smoke = rows[-1]
    assert smoke['capture_mode'] == 'harness_smoke_2026-08-24'
    assert smoke['candidate'] == 'opencode-go/ox-alpha-free'
    assert smoke['correctness_passed'] is True
    assert smoke['session_db_layout'] == 'xdg'
    assert smoke['tool_error_count'] == 0


def test_report_states_corrected_timings() -> None:
    text = REPORT.read_text()
    assert '285 s' in text
    assert '4 min 45 s' in text
    assert '1560 s' in text
    assert '~1590 s' in text


def test_banned_phrases_absent_from_committed_evidence_and_docs() -> None:
    haystacks = [REPORT.read_text(), OBSERVATIONS.read_text()]
    readme = REPO_ROOT / 'docs' / 'glasslab-v2' / 'current' / 'README.md'
    service_readme = (
        REPO_ROOT / 'services' / 'research-orchestrator' / 'README.md'
    )
    haystacks.append(readme.read_text())
    haystacks.append(service_readme.read_text())
    for text in haystacks:
        lowered = text.lower()
        for phrase in BANNED_PHRASES:
            assert phrase.lower() not in lowered, phrase
