"""Behavioral tests for the intake agent.

The service is loaded from its source files under a synthetic package name
rather than imported as `app`, so the tests do not depend on how the deployed
image lays out its code. Loading order matters: main.py imports `.models`, so
models must be registered in sys.modules first. Several assertions (venue and
paper counts, track ids, exact warning wording) are pinned to the checked-in
seed manifest, so this suite also guards
seeds/glasslab_paper_harvester_seed_manifest.yaml against unreviewed edits.
"""

import sys
import types
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

SERVICE_ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = SERVICE_ROOT / 'app'
PACKAGE_NAME = 'intake_agent_app'


def load_package_module(module_name: str, path: Path):
    # Executes the file under the synthetic package name so relative imports
    # (`from .models import ...`) resolve; registering in sys.modules before
    # exec_module lets a later `from .models import ...` reuse this exact
    # module object instead of re-executing the file.
    spec = spec_from_file_location(module_name, path)
    assert spec is not None
    assert spec.loader is not None
    module = module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


package = types.ModuleType(PACKAGE_NAME)
package.__path__ = [str(APP_ROOT)]
sys.modules[PACKAGE_NAME] = package

# models must load before main: main.py does `from .models import ...` and the
# relative import resolves through the synthetic package registered above.
models_module = load_package_module(f'{PACKAGE_NAME}.models', APP_ROOT / 'models.py')
main_module = load_package_module(f'{PACKAGE_NAME}.main', APP_ROOT / 'main.py')

app = main_module.app
build_normalized_draft = main_module.build_normalized_draft
build_approval_warnings = main_module.build_approval_warnings
build_harvester_plan = main_module.build_harvester_plan
build_problem_harvester_plan = main_module.build_problem_harvester_plan
NormalizeIntakeRequest = models_module.NormalizeIntakeRequest

INTERNAL_TOKEN_HEADER = 'X-Glasslab-Internal-Token'
INTERNAL_TOKEN_ENV = 'GLASSLAB_AGENT_INTERNAL_TOKEN'
INTERNAL_TOKEN = 'test-internal-token'


def internal_auth_headers() -> dict[str, str]:
    return {INTERNAL_TOKEN_HEADER: INTERNAL_TOKEN}


@pytest.fixture(autouse=True)
def configure_internal_token(monkeypatch):
    monkeypatch.setenv(INTERNAL_TOKEN_ENV, INTERNAL_TOKEN)


def build_request() -> NormalizeIntakeRequest:
    # example.org is deliberately absent from the seed manifest's venue
    # allowlist, so every test using this request also exercises the
    # out-of-policy source warning.
    return NormalizeIntakeRequest(
        request_id='intake-1',
        intake={
            'raw_request': 'Read this paper and turn it into a bounded benchmark request on the Titanic dataset.',
            'source_refs': ['https://example.org/paper'],
            'notes': ['Focus on the reported baseline and evaluation setup.'],
            'submitted_by': 'glasslab-operator',
        },
    )


def test_healthz() -> None:
    client = TestClient(app)
    response = client.get('/healthz')
    assert response.status_code == 200
    payload = response.json()
    assert payload['status'] == 'ok'
    assert payload['model_backend']['model'] == 'mlx-community/Qwen3-Coder-Next-4bit'
    assert payload['model_backend']['provider'] == 'openai-compatible'
    assert payload['approved_sources']['manifest_name'] == 'glasslab_paper_harvester_seed_manifest'
    assert payload['approved_sources']['venue_count'] == 9


def test_approved_sources_endpoint() -> None:
    client = TestClient(app)
    response = client.get('/approved-sources', headers=internal_auth_headers())
    assert response.status_code == 200
    payload = response.json()
    assert payload['manifest_version'] == 1
    assert 'jmlr.org' in payload['approved_hosts']
    assert 'arxiv.org' in payload['approved_hosts']


def test_paper_harvester_tracks_endpoint() -> None:
    client = TestClient(app)
    response = client.get('/paper-harvester/tracks', headers=internal_auth_headers())
    assert response.status_code == 200
    payload = response.json()
    assert any(track['track_id'] == 'tabular_baselines' for track in payload)
    assert any(track['track_id'] == 'autonomous_science' for track in payload)


def test_build_harvester_plan_prefers_bounded_seed_papers() -> None:
    plan = build_harvester_plan(
        models_module.PaperHarvesterPlanRequest(
            request_id='harvest-1',
            track_ids=['tabular_baselines'],
            priorities=['P0'],
            max_papers=2,
        )
    )
    assert plan.selected_tracks[0].track_id == 'tabular_baselines'
    assert plan.selected_papers
    assert all('tabular_baselines' in paper.tracks for paper in plan.selected_papers)
    assert len(plan.selected_papers) == 2


def test_paper_harvester_plan_warns_on_unknown_track() -> None:
    client = TestClient(app)
    response = client.post(
        '/paper-harvester/plan',
        json={
            'request_id': 'harvest-2',
            'track_ids': ['does-not-exist'],
            'max_papers': 3,
        },
        headers=internal_auth_headers(),
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload['selected_tracks'] == []
    assert payload['warnings'] == [
        'unknown track ids ignored: does-not-exist',
        'no seed papers matched the requested track/priority filters',
    ]


def test_problem_harvester_plan_matches_agent_evaluation_problem() -> None:
    plan = build_problem_harvester_plan(
        models_module.ProblemHarvesterPlanRequest(
            request_id='problem-1',
            problem_statement='We need a bounded benchmark for research agents doing machine learning engineering work.',
            max_papers=3,
        )
    )
    assert plan.selected_tracks
    assert any(track.track_id == 'agent_evaluation' for track in plan.selected_tracks)
    assert plan.selected_papers
    assert plan.selected_papers[0].paper_id in {'mle_bench_arxiv_2024', 'mlgym_arxiv_2025'}
    assert plan.coverage_summary.coverage_mode == 'strong'
    assert 'agent_evaluation' in plan.coverage_summary.matched_track_ids
    assert plan.coverage_summary.problem_token_count > 0


def test_problem_harvester_plan_surfaces_weak_coverage_diagnostics(monkeypatch) -> None:
    # Patching the loader functions (not the cached seed manifest) keeps the
    # real seed data out of this test; the fake track's tokens are deliberately
    # disjoint from the problem statement to force zero overlap and the
    # fallback coverage path.
    monkeypatch.setattr(
        main_module,
        'load_tracks',
        lambda: [
            models_module.TrackDefinition(
                track_id='unrelated_track',
                description='zzz qqq rrr',
                default_priority='P1',
                queries=['xxx yyy zzz'],
            )
        ],
    )
    monkeypatch.setattr(
        main_module,
        'load_seed_papers',
        lambda: [
            models_module.SeedPaperSummary(
                paper_id='tabular_seed_1',
                title='Tabular Baseline Seed Paper',
                year=2024,
                venue='arXiv',
                venue_id='arxiv',
                priority='P1',
                tracks=['unrelated_track'],
                bounded_job_fit=3,
                replication_complexity=2,
                official_page='https://example.org/tabular-seed',
                pdf_url=None,
                why_seed='Seed corpus placeholder',
                first_jobs=['run the baseline as-is'],
                tags=['tabular'],
            )
        ],
    )

    plan = build_problem_harvester_plan(
        models_module.ProblemHarvesterPlanRequest(
            request_id='problem-weak-1',
            problem_statement='We need papers on underwater robotics for coral mapping and lunar agriculture.',
            max_papers=2,
        )
    )
    assert plan.selected_tracks == []
    assert plan.selected_papers
    assert plan.coverage_summary.coverage_mode == 'fallback'
    assert plan.coverage_summary.matched_track_ids == []
    assert plan.coverage_summary.selected_track_scores == {}
    assert plan.coverage_summary.selected_paper_scores['tabular_seed_1'] == 0
    assert any(
        'weakly overlaps the approved seed manifest' in warning
        or 'no track-level seed match' in warning
        for warning in plan.warnings
    )


def test_problem_harvester_plan_endpoint() -> None:
    client = TestClient(app)
    response = client.post(
        '/paper-harvester/plan-from-problem',
        json={
            'request_id': 'problem-2',
            'problem_statement': 'Find bounded reproducibility papers and benchmark-suite work we can run on the cluster.',
            'max_papers': 2,
        },
        headers=internal_auth_headers(),
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload['selected_tracks']
    assert len(payload['selected_papers']) <= 2
    assert payload['approved_sources']['paper_count'] == 12
    assert payload['coverage_summary']['coverage_mode'] in {'strong', 'thin', 'fallback'}
    assert 'problem_token_count' in payload['coverage_summary']


def test_build_normalized_draft() -> None:
    draft = build_normalized_draft(build_request())
    assert draft.source_type == 'paper-link'
    assert draft.workflow_family_candidates == ['literature-to-experiment', 'generic-tabular-benchmark']
    assert draft.submitted_by == 'glasslab-operator'
    assert draft.normalized_summary.startswith('Read this paper and turn it into a bounded benchmark request')


def test_build_approval_warnings_for_unapproved_host() -> None:
    warnings = build_approval_warnings(build_request())
    assert warnings == [
        'current implementation is deterministic scaffold logic; live model integration is not enabled yet',
        'source refs include hosts outside the current approved seed manifest: example.org',
    ]


def test_normalize_intake_endpoint() -> None:
    client = TestClient(app)
    response = client.post(
        '/normalize-intake',
        json=build_request().model_dump(),
        headers=internal_auth_headers(),
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload['request_id'] == 'intake-1'
    assert payload['draft']['source_type'] == 'paper-link'
    assert payload['draft']['workflow_family_candidates'] == [
        'literature-to-experiment',
        'generic-tabular-benchmark',
    ]
    assert payload['approved_sources']['paper_count'] == 12
    assert payload['warnings'] == [
        'current implementation is deterministic scaffold logic; live model integration is not enabled yet',
        'source refs include hosts outside the current approved seed manifest: example.org',
    ]


PROTECTED_REQUESTS = [
    ('get', '/approved-sources', None),
    ('get', '/paper-harvester/tracks', None),
    ('get', '/paper-harvester/papers', None),
    ('post', '/paper-harvester/plan', {'request_id': 'auth-plan', 'track_ids': [], 'max_papers': 1}),
    (
        'post',
        '/paper-harvester/plan-from-problem',
        {
            'request_id': 'auth-problem',
            'problem_statement': 'Find bounded benchmark papers.',
            'max_papers': 1,
        },
    ),
    ('post', '/normalize-intake', build_request().model_dump()),
]

PROTECTED_REQUEST_IDS = [entry[1] for entry in PROTECTED_REQUESTS]


@pytest.mark.parametrize(('method', 'path', 'body'), PROTECTED_REQUESTS, ids=PROTECTED_REQUEST_IDS)
def test_protected_route_rejects_missing_internal_token(method, path, body) -> None:
    client = TestClient(app)
    response = client.request(method, path, json=body)
    assert response.status_code == 401


@pytest.mark.parametrize(('method', 'path', 'body'), PROTECTED_REQUESTS, ids=PROTECTED_REQUEST_IDS)
def test_protected_route_rejects_wrong_internal_token(method, path, body) -> None:
    client = TestClient(app)
    response = client.request(
        method,
        path,
        json=body,
        headers={INTERNAL_TOKEN_HEADER: 'wrong-token'},
    )
    assert response.status_code == 401


@pytest.mark.parametrize(('method', 'path', 'body'), PROTECTED_REQUESTS, ids=PROTECTED_REQUEST_IDS)
def test_protected_route_fails_closed_when_token_unconfigured(monkeypatch, method, path, body) -> None:
    monkeypatch.delenv(INTERNAL_TOKEN_ENV)
    client = TestClient(app)
    response = client.request(method, path, json=body, headers=internal_auth_headers())
    assert response.status_code == 503


def test_healthz_stays_anonymous() -> None:
    client = TestClient(app)
    assert client.get('/healthz').status_code == 200
