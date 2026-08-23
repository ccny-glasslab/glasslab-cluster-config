"""Integration tests for the workflow-api FastAPI application.

Each test builds a fresh ``TestClient`` backed by an in-memory store and
the real workflow registry.  Import-time module-cache clearing ensures
tests do not accidentally share ``app.*`` module state across test
modules or between parametrized runs with different settings.
"""

import sys
import json
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path

from fastapi.testclient import TestClient as FastAPITestClient

# Prevent stale ``app.*`` module state from leaking between test modules.
# conftest.py sets up the import paths; each test file must also clear the
# cache so runs with different Settings or monkeypatches get fresh modules.
for module_name in list(sys.modules):
    if module_name == 'app' or module_name.startswith('app.'):
        del sys.modules[module_name]

from app.config import Settings
from app.auth import CallerPolicy
import app.autoresearch as autoresearch_module
import app.main as main_module
import app.source_documents as source_documents
from app.job_submission import LiveStatusUnavailableError, NullJobSubmitter
from app.schemas import AutoresearchDecisionRecord, AutoresearchIterationRecord, EvaluatorContract
from app.stage_interpretation import build_interpretation_record_from_agent_draft, validate_interpretation_agent_draft
from app.main import create_app
from app.persistence import InMemoryRunStore
from app.registry import WorkflowRegistry

REPO_ROOT = Path(__file__).resolve().parents[3]


class TestClient(FastAPITestClient):
    """Exercise existing route tests as an explicitly authorized caller."""

    def __init__(self, app, **kwargs) -> None:
        app.state.settings.caller_policies = (
            CallerPolicy(
                name='test-suite',
                token='test-suite-token',
                allowed_operations=frozenset(
                    f'{method} {route.path_format}'
                    for route in app.routes
                    if hasattr(route, 'path_format') and hasattr(route, 'methods')
                    for method in route.methods & {'GET', 'POST', 'PUT', 'PATCH', 'DELETE'}
                ),
            ),
        )
        headers = dict(kwargs.pop('headers', {}))
        headers.setdefault('X-Glasslab-Caller', 'test-suite')
        headers.setdefault('X-Glasslab-Workflow-Token', 'test-suite-token')
        super().__init__(app, headers=headers, **kwargs)


def build_client(artifacts_mount_path: Path | None = None, *, submitter=None) -> TestClient:
    # Tests that need on-disk artifacts pass a tmp_path; tests that only
    # exercise API routes and in-memory state skip it so the default
    # artifacts_mount_path is not set.
    settings = Settings(
        registry_dir=str(REPO_ROOT / 'services' / 'workflow-registry' / 'definitions'),
        **(
            {'artifacts_mount_path': str(artifacts_mount_path)}
            if artifacts_mount_path is not None
            else {}
        ),
    )
    registry = WorkflowRegistry(settings.registry_dir)
    store = InMemoryRunStore()
    return TestClient(create_app(settings=settings, registry=registry, store=store, submitter=submitter))


def test_healthz_and_workflow_families() -> None:
    client = build_client()

    health = client.get('/healthz')
    assert health.status_code == 200
    assert health.json()['workflow_count'] == 10
    assert health.json()['store_backend'] == 'memory'

    families = client.get('/workflow-families')
    assert families.status_code == 200
    payload = families.json()
    assert {entry['workflow_id'] for entry in payload} == {
        'gpu-experiment',
        'benchmark-workspace-cpu-v1',
        'benchmark-workspace-gpu-v1',
        'generic-tabular-benchmark',
        'literature-to-experiment',
        'metric-search-v0',
        'replication-lite',
        'research-workspace-cpu-v1',
        'workspace-cpu-ml-v1',
        'workspace-gpu-ml-v1',
    }
    by_id = {entry['workflow_id']: entry for entry in payload}
    assert by_id['gpu-experiment']['execution_status'] == 'ready'
    assert by_id['gpu-experiment']['submission_backend'] == 'kubernetes'
    assert by_id['gpu-experiment']['resource_profile'] == 'gpu-small'
    assert by_id['metric-search-v0']['execution_status'] == 'ready'
    assert by_id['metric-search-v0']['submission_backend'] == 'kubernetes'
    assert by_id['generic-tabular-benchmark']['execution_status'] == 'ready'
    assert by_id['generic-tabular-benchmark']['submission_backend'] == 'kubernetes'
    assert by_id['replication-lite']['execution_status'] == 'declared_only'
    assert by_id['replication-lite']['submission_backend'] == 'unimplemented'


def test_openapi_marks_literature_session_routes_deprecated() -> None:
    client = build_client()

    schema = client.get('/openapi.json')
    assert schema.status_code == 200
    paths = schema.json()['paths']

    assert paths['/investigations']['post'].get('deprecated') is not True
    assert paths['/experiments/runs']['post'].get('deprecated') is not True
    assert paths['/research-sessions/start-literature-search']['post']['deprecated'] is True
    assert paths['/paper-pipelines/fresh-paper']['post']['deprecated'] is True
    assert paths['/paper-intake-queues/{queue_id}/stage-next-intake']['post']['deprecated'] is True
    assert paths['/research-sessions/{session_id}/literature-digest']['get']['deprecated'] is True


def test_generic_experiment_run_result_ingest_and_compare() -> None:
    client = build_client()

    create = client.post(
        '/experiments/runs',
        json={
            'objective': 'Run a bounded metric-search smoke job.',
            'experiment_type': 'gpu-training-job',
            'workload_id': 'metric-search-v0',
            'entrypoint': [
                'python3',
                'scripts/run_experiment.py',
                '--config',
                'configs/search_spaces/art_metric_baseline.yaml',
                '--output-dir',
                '/mnt/artifacts/metric-search-smoke',
            ],
            'config_payload': {'search_space_id': 'art-metric-baseline'},
            'dataset_bindings': {'train_uri': 's3://datasets/art/train.parquet'},
            'budget': {'max_epochs': 1, 'max_wallclock_minutes': 5},
            'submitted_by': 'test-suite',
        },
    )
    assert create.status_code == 201
    run_a = create.json()
    assert run_a['workflow_id'] == 'metric-search-v0'
    assert run_a['manifest']['experiment_type'] == 'gpu-training-job'
    assert run_a['manifest']['workload_id'] == 'metric-search-v0'
    assert run_a['manifest']['entrypoint'][0] == 'python3'

    create_b = client.post(
        '/experiments/runs',
        json={
            'objective': 'Run a second bounded metric-search smoke job.',
            'experiment_type': 'gpu-training-job',
            'workload_id': 'metric-search-v0',
            'entrypoint': [
                'python3',
                'scripts/run_experiment.py',
                '--config',
                'configs/search_spaces/art_metric_proxy_v0.yaml',
                '--output-dir',
                '/mnt/artifacts/metric-search-smoke-b',
            ],
            'config_payload': {'search_space_id': 'art-metric-proxy-v0'},
            'dataset_bindings': {'train_uri': 's3://datasets/art/train.parquet'},
            'budget': {'max_epochs': 1, 'max_wallclock_minutes': 5},
            'submitted_by': 'test-suite',
        },
    )
    assert create_b.status_code == 201
    run_b = create_b.json()

    incomplete_ingest = client.post(
        f"/experiments/runs/{run_a['run_id']}/results",
        json={
            'terminal_status': 'succeeded',
            'metrics': {'composite_score': 0.61},
            'artifact_refs': {'metrics.json': 's3://artifacts/run-a/metrics.json'},
        },
    )
    assert incomplete_ingest.status_code == 409
    assert incomplete_ingest.json()['detail']['message'] == 'successful result is missing required artifacts'

    ingest_a = client.post(
        f"/experiments/runs/{run_a['run_id']}/results",
        json={
            'terminal_status': 'succeeded',
            'metrics': {'composite_score': 0.61, 'retrieval_recall_at_10': 0.72},
            'artifact_refs': {
                name: f's3://artifacts/run-a/{name}'
                for name in [
                    'run_manifest.json',
                    'config.json',
                    'metrics.json',
                    'artifacts_index.json',
                    'report.md',
                    'status.json',
                    'logs/',
                ]
            },
            'runtime': {'node_name': 'node02'},
        },
    )
    assert ingest_a.status_code == 200
    assert ingest_a.json()['status']['status'] == 'succeeded'
    assert ingest_a.json()['reported_metrics']['composite_score'] == 0.61

    ingest_b = client.post(
        f"/experiments/runs/{run_b['run_id']}/results",
        json={
            'terminal_status': 'succeeded',
            'metrics': {'composite_score': 0.74, 'retrieval_recall_at_10': 0.81},
            'artifact_refs': {
                name: f's3://artifacts/run-b/{name}'
                for name in [
                    'run_manifest.json',
                    'config.json',
                    'metrics.json',
                    'artifacts_index.json',
                    'report.md',
                    'status.json',
                    'logs/',
                ]
            },
            'runtime': {'node_name': 'node04'},
        },
    )
    assert ingest_b.status_code == 200

    compare = client.post(
        '/experiments/compare',
        json={
            'run_ids': [run_a['run_id'], run_b['run_id']],
            'metric_name': 'composite_score',
            'higher_is_better': True,
            'workload_id': 'metric-search-v0',
        },
    )
    assert compare.status_code == 201
    payload = compare.json()
    assert payload['workload_id'] == 'metric-search-v0'
    assert payload['summary_metrics']['metric_name'] == 'composite_score'
    assert payload['summary_metrics']['best_run_id'] == run_b['run_id']


def test_research_workspace_cannot_bypass_investigation_approval() -> None:
    client = build_client()

    response = client.post(
        '/experiments/runs',
        json={
            'objective': 'Attempt to launch an unapproved research workspace.',
            'experiment_type': 'research-workspace-job',
            'workload_id': 'research-workspace-cpu-v1',
            'budget': {'max_wallclock_minutes': 5},
        },
    )

    assert response.status_code == 409
    assert (
        response.json()['detail']
        == 'research workspace runs require an approved investigation plan execution'
    )


def test_confirmatory_investigation_freezes_workspace_plan_launches_run_and_records_claim(
    tmp_path: Path,
) -> None:
    client = build_client(tmp_path)

    create = client.post(
        '/investigations',
        json={
            'title': 'Adult Income investigation',
            'research_question': 'Can a frozen, leakage-controlled Adult Income workspace outperform the majority baseline?',
            'research_mode': 'confirmatory',
            'hypotheses': [
                'A nonlinear Adult Income model improves held-out ROC-AUC over the linear baseline.'
            ],
            'priorities': ['reproducibility', 'bounded execution'],
            'submitted_by': 'test-researcher',
        },
    )
    assert create.status_code == 201
    investigation = create.json()
    investigation_id = investigation['investigation_id']
    hypothesis_id = investigation['hypotheses'][0]['hypothesis_id']
    assert investigation['status'] == 'planning'
    assert 'session_id' not in investigation

    plan = client.post(
        f'/investigations/{investigation_id}/plans',
        json={
            'title': 'Frozen Adult Income CPU workspace',
            'rationale': 'Use immutable task, source, and dataset bundles under a bounded run.',
            'hypothesis_ids': [hypothesis_id],
            'executions': [
                {
                    'execution_id': 'adult-train',
                    'objective': 'Execute the frozen Adult Income analysis and emit a verifiable report bundle.',
                    'experiment_type': 'research-workspace-job',
                    'workload_id': 'research-workspace-cpu-v1',
                    'data_access_scope': 'train',
                    'workspace': {
                        'task_bundle': {
                            'uri': 's3://datasets/benchmarks/adult-income-v1/task.zip',
                            'sha256': '1' * 64,
                            'media_type': 'application/zip',
                        },
                        'source_bundle': {
                            'uri': 's3://artifacts/submissions/adult-income-v1/submission.zip',
                            'sha256': '2' * 64,
                            'media_type': 'application/zip',
                        },
                        'working_directory': 'submission',
                        'command': ['python3', 'run.py', '--output-dir', '/outputs'],
                        'output_directory': '/outputs',
                        'network_policy': 'none',
                    },
                    'dataset_bindings': [
                        {
                            'name': 'adult_train',
                            'asset': {
                                'uri': 's3://datasets/uci-adult/adult.data',
                                'sha256': '3' * 64,
                                'media_type': 'text/csv',
                            },
                            'role': 'train',
                            'contains_labels': True,
                            'access_scopes': ['train', 'validate'],
                        },
                        {
                            'name': 'adult_test',
                            'asset': {
                                'uri': 's3://datasets/uci-adult/adult.test',
                                'sha256': '4' * 64,
                                'media_type': 'text/csv',
                            },
                            'role': 'test',
                            'contains_labels': True,
                            'access_scopes': ['evaluate'],
                        },
                    ],
                    'config_payload': {'evaluation_mode': 'benchmark'},
                    'budget': {
                        'budget_mode': 'wallclock',
                        'max_wallclock_minutes': 20,
                    },
                    'artifact_contract': {
                        'required': [
                            'run_manifest.json',
                            'config.json',
                            'metrics.json',
                            'artifacts_index.json',
                            'report.md',
                            'status.json',
                            'logs/',
                            'source.zip',
                        ],
                        'optional': ['plots/'],
                    },
                    'evaluator_contract': {
                        'evaluator_type': 'rubric-gated-v1',
                        'primary_metric': {
                            'name': 'rubric_score',
                            'direction': 'maximize',
                            'minimum_effect': 0,
                        },
                        'guardrails': [
                            {
                                'name': 'integrity_pass',
                                'direction': 'maximize',
                                'minimum': 1,
                                'required': True,
                            }
                        ],
                    },
                },
                {
                    'execution_id': 'adult-evaluate',
                    'depends_on': ['adult-train'],
                    'objective': 'Evaluate the frozen Adult Income submission only after training completes.',
                    'experiment_type': 'research-workspace-job',
                    'workload_id': 'research-workspace-cpu-v1',
                    'data_access_scope': 'evaluate',
                    'workspace': {
                        'task_bundle': {
                            'uri': 's3://datasets/benchmarks/adult-income-v1/task.zip',
                            'sha256': '1' * 64,
                        },
                        'source_bundle': {
                            'uri': 's3://artifacts/evaluators/adult-income-v1/evaluator.zip',
                            'sha256': '5' * 64,
                        },
                        'command': ['python3', 'evaluate.py'],
                    },
                    'dataset_bindings': [
                        {
                            'name': 'adult_test',
                            'asset': {
                                'uri': 's3://datasets/uci-adult/adult.test',
                                'sha256': '4' * 64,
                            },
                            'role': 'test',
                            'contains_labels': True,
                            'access_scopes': ['evaluate'],
                        }
                    ],
                    'budget': {
                        'budget_mode': 'wallclock',
                        'max_wallclock_minutes': 10,
                    },
                    'artifact_contract': {
                        'required': [
                            'run_manifest.json',
                            'config.json',
                            'metrics.json',
                            'artifacts_index.json',
                            'report.md',
                            'status.json',
                            'logs/',
                            'source.zip',
                        ]
                    },
                    'evaluator_contract': {
                        'evaluator_type': 'rubric-gated-v1',
                        'primary_metric': {
                            'name': 'rubric_score',
                            'direction': 'maximize',
                        },
                    },
                },
            ],
            'submitted_by': 'test-researcher',
        },
    )
    assert plan.status_code == 201
    plan_payload = plan.json()
    assert plan_payload['revision'] == 1
    assert plan_payload['executions'][0]['workspace']['source_bundle']['sha256'] == '2' * 64

    approve = client.post(
        f'/investigations/{investigation_id}/plan-approvals',
        json={
            'plan_id': plan_payload['plan_id'],
            'approved_by': 'test-researcher',
            'note': 'Freeze task, code, data, environment contract, budget, and evaluator before execution.',
        },
    )
    assert approve.status_code == 200
    approved = approve.json()
    approval_id = approved['active_plan_approval_id']
    assert approved['status'] == 'approved'
    assert len(approved['plan_approvals']) == 1
    assert len(approved['plan_approvals'][0]['plan_sha256']) == 64
    assert approved['plan_approvals'][0]['hypothesis_ids'] == [hypothesis_id]
    assert (
        approved['plan_approvals'][0]['plan_snapshot']['plan']['plan_id']
        == plan_payload['plan_id']
    )

    approve_again = client.post(
        f'/investigations/{investigation_id}/plan-approvals',
        json={'plan_id': plan_payload['plan_id']},
    )
    assert approve_again.status_code == 200
    assert len(approve_again.json()['plan_approvals']) == 1

    frozen_hypothesis = client.post(
        f'/investigations/{investigation_id}/hypotheses',
        json={'statement': 'A post-hoc confirmatory hypothesis should not be accepted.'},
    )
    assert frozen_hypothesis.status_code == 409
    assert frozen_hypothesis.json()['detail'] == 'confirmatory hypotheses are frozen after plan approval'

    blocked_evaluation = client.post(
        f'/investigations/{investigation_id}/runs',
        json={'approval_id': approval_id, 'execution_id': 'adult-evaluate'},
    )
    assert blocked_evaluation.status_code == 409
    assert blocked_evaluation.json()['detail']['dependencies'] == ['adult-train']

    launch = client.post(
        f'/investigations/{investigation_id}/runs',
        json={'approval_id': approval_id, 'execution_id': 'adult-train'},
    )
    assert launch.status_code == 201
    launch_payload = launch.json()
    run_id = launch_payload['run']['run_id']
    assert launch_payload['run']['investigation_id'] == investigation_id
    assert launch_payload['run']['source_plan_id'] == plan_payload['plan_id']
    assert launch_payload['run']['source_approval_id'] == approval_id
    assert launch_payload['run']['source_execution_id'] == 'adult-train'
    assert launch_payload['run']['plan_sha256'] == approved['plan_approvals'][0]['plan_sha256']
    assert launch_payload['execution']['execution_id'] == 'adult-train'
    assert launch_payload['run']['manifest']['dataset_bindings'] == {
        'adult_train': 's3://datasets/uci-adult/adult.data'
    }
    assert [
        contract['name']
        for contract in launch_payload['run']['manifest']['config_payload']['dataset_contracts']
    ] == ['adult_train']
    assert launch_payload['run']['session_id'] is None
    assert launch_payload['investigation']['run_ids'] == [run_id]

    premature_claim = client.post(
        f'/investigations/{investigation_id}/claims',
        json={
            'statement': 'The nonlinear model is supported as the stronger Adult Income baseline.',
            'assessment': 'supported',
            'hypothesis_ids': [hypothesis_id],
            'evidence': [{'run_id': run_id, 'artifact_name': 'metrics.json'}],
        },
    )
    assert premature_claim.status_code == 409
    assert premature_claim.json()['detail'] == f'run is not terminal: {run_id}'

    required_artifacts = [
        'run_manifest.json',
        'config.json',
        'metrics.json',
        'artifacts_index.json',
        'report.md',
        'status.json',
        'logs/',
        'source.zip',
    ]
    run_dir = tmp_path / run_id
    (run_dir / 'logs').mkdir(parents=True)
    artifact_contents = {
        'run_manifest.json': '{}',
        'config.json': '{}',
        'metrics.json': '{"rubric_score":88,"integrity_pass":1}',
        'artifacts_index.json': '{}',
        'report.md': '# Adult Income report\n',
        'status.json': '{"status":"succeeded"}',
        'source.zip': 'frozen source bundle',
    }
    for name, content in artifact_contents.items():
        (run_dir / name).write_text(content)
    (run_dir / 'logs' / 'runner.log').write_text('workspace complete\n')
    ingest = client.post(f'/runs/{run_id}/artifacts/ingest')
    assert ingest.status_code == 200
    assert ingest.json()['artifact_bundle_verified'] is True

    evaluate = client.post(
        f'/investigations/{investigation_id}/runs',
        json={'approval_id': approval_id, 'execution_id': 'adult-evaluate'},
    )
    assert evaluate.status_code == 201
    evaluate_payload = evaluate.json()
    evaluate_run_id = evaluate_payload['run']['run_id']
    assert evaluate_payload['run']['source_execution_id'] == 'adult-evaluate'
    assert evaluate_payload['run']['manifest']['dataset_bindings'] == {
        'adult_test': 's3://datasets/uci-adult/adult.test'
    }
    assert evaluate_payload['investigation']['run_ids'] == [run_id, evaluate_run_id]

    claim = client.post(
        f'/investigations/{investigation_id}/claims',
        json={
            'statement': 'The frozen run supports the nonlinear model as the stronger Adult Income baseline.',
            'assessment': 'supported',
            'hypothesis_ids': [hypothesis_id],
            'evidence': [{'run_id': run_id, 'artifact_name': 'metrics.json'}],
            'submitted_by': 'test-researcher',
        },
    )
    assert claim.status_code == 201
    claim_payload = claim.json()
    assert claim_payload['evidence'] == [
        {
            'run_id': run_id,
            'artifact_name': 'metrics.json',
            'artifact_ref': f'artifacts/{run_id}/metrics.json',
            'artifact_sha256': sha256(
                artifact_contents['metrics.json'].encode()
            ).hexdigest(),
        }
    ]

    (run_dir / 'metrics.json').write_text('{"rubric_score":99,"integrity_pass":1}')
    drifted_claim = client.post(
        f'/investigations/{investigation_id}/claims',
        json={
            'statement': 'A changed artifact must not support another scientific claim.',
            'assessment': 'supported',
            'hypothesis_ids': [hypothesis_id],
            'evidence': [{'run_id': run_id, 'artifact_name': 'metrics.json'}],
        },
    )
    assert drifted_claim.status_code == 409
    assert 'no longer matches its ingested content digest' in drifted_claim.json()['detail']

    context = client.get(f'/investigations/{investigation_id}/context')
    assert context.status_code == 200
    context_payload = context.json()
    assert context_payload['investigation']['status'] == 'evaluating'
    assert context_payload['investigation']['claims'][0]['claim_id'] == claim_payload['claim_id']
    assert context_payload['current_plan']['plan_id'] == plan_payload['plan_id']
    assert context_payload['approved_plan']['plan_id'] == plan_payload['plan_id']
    assert context_payload['runs'][0]['status']['status'] == 'succeeded'
    assert context_payload['runs'][1]['source_execution_id'] == 'adult-evaluate'


def test_investigation_rejects_blank_hypothesis_after_normalization() -> None:
    client = build_client()

    question_only = client.post(
        '/investigations',
        json={
            'research_question': 'Which testable hypotheses should be considered for this bounded question?',
            'research_mode': 'exploratory',
        },
    )
    assert question_only.status_code == 201
    assert question_only.json()['hypotheses'] == []
    approve_without_hypothesis = client.post(
        f"/investigations/{question_only.json()['investigation_id']}/plan-approvals",
        json={'plan_id': 'missing-plan'},
    )
    assert approve_without_hypothesis.status_code == 409
    assert (
        approve_without_hypothesis.json()['detail']
        == 'investigation needs at least one hypothesis before plan approval'
    )

    create = client.post(
        '/investigations',
        json={
            'research_question': 'Does request validation reject a hypothesis that only contains spaces?',
            'research_mode': 'exploratory',
            'hypotheses': ['        '],
        },
    )
    assert create.status_code == 422


def test_exploratory_hypothesis_change_invalidates_plan_approval() -> None:
    client = build_client()

    create = client.post(
        '/investigations',
        json={
            'research_question': 'Which bounded analysis plan is most promising for an Adult Income follow-up experiment?',
            'research_mode': 'exploratory',
            'hypotheses': [
                'Tree-based models may outperform the linear Adult Income baseline.'
            ],
        },
    )
    assert create.status_code == 201
    investigation = create.json()
    investigation_id = investigation['investigation_id']
    hypothesis_id = investigation['hypotheses'][0]['hypothesis_id']
    plan = client.post(
        f'/investigations/{investigation_id}/plans',
        json={
            'title': 'Exploratory frozen workspace',
            'rationale': 'Test one immutable analysis workspace before proposing a revision.',
            'hypothesis_ids': [hypothesis_id],
            'executions': [{
                'execution_id': 'explore',
                'objective': 'Execute one bounded exploratory workspace and preserve its evidence bundle.',
                'experiment_type': 'research-workspace-job',
                'workload_id': 'research-workspace-cpu-v1',
                'data_access_scope': 'solve',
                'workspace': {
                    'task_bundle': {
                        'uri': 's3://datasets/benchmarks/adult-income-v1/task.zip',
                        'sha256': 'a' * 64,
                    },
                    'source_bundle': {
                        'uri': 's3://artifacts/submissions/adult-income-v1/source.zip',
                        'sha256': 'b' * 64,
                    },
                    'command': ['python3', 'run.py'],
                },
                'budget': {
                    'budget_mode': 'wallclock',
                    'max_wallclock_minutes': 10,
                },
                'artifact_contract': {
                    'required': [
                        'run_manifest.json',
                        'config.json',
                        'metrics.json',
                        'artifacts_index.json',
                        'report.md',
                        'status.json',
                        'logs/',
                        'source.zip',
                    ]
                },
                'evaluator_contract': {
                    'evaluator_type': 'rubric-gated-v1',
                    'primary_metric': {
                        'name': 'rubric_score',
                        'direction': 'maximize',
                    },
                },
            }],
        },
    )
    assert plan.status_code == 201

    approve = client.post(
        f'/investigations/{investigation_id}/plan-approvals',
        json={'plan_id': plan.json()['plan_id']},
    )
    assert approve.status_code == 200
    assert approve.json()['active_plan_approval_id']

    add_hypothesis = client.post(
        f'/investigations/{investigation_id}/hypotheses',
        json={
            'statement': 'Calibration quality may matter more than raw holdout accuracy for the next study.'
        },
    )
    assert add_hypothesis.status_code == 201
    updated = add_hypothesis.json()
    assert updated['status'] == 'planning'
    assert updated['active_plan_approval_id'] is None
    assert len(updated['plan_approvals']) == 1

    blocked_launch = client.post(
        f'/investigations/{investigation_id}/runs',
        json={
            'approval_id': approve.json()['active_plan_approval_id'],
            'execution_id': 'explore',
        },
    )
    assert blocked_launch.status_code == 409
    assert blocked_launch.json()['detail'] == 'investigation has no active plan approval'


def test_healthz_redacts_postgres_password() -> None:
    settings = Settings(
        registry_dir=str(REPO_ROOT / 'services' / 'workflow-registry' / 'definitions'),
        store_backend='postgres',
        store_postgres_dsn='postgresql://glasslab:super-secret@glasslab-postgres.glasslab-v2.svc.cluster.local:5432/glasslab',
    )
    registry = WorkflowRegistry(settings.registry_dir)
    store = InMemoryRunStore()
    client = TestClient(create_app(settings=settings, registry=registry, store=store))

    health = client.get('/healthz')
    assert health.status_code == 200
    assert health.json()['store_target'] == 'postgresql://glasslab:***@glasslab-postgres.glasslab-v2.svc.cluster.local:5432/glasslab'


def test_create_and_fetch_latest_intake() -> None:
    client = build_client()

    create = client.post(
        '/intakes',
        json={
            'raw_request': 'Take this paper note set and turn it into a bounded validation experiment on a tabular dataset.',
            'source_refs': ['arxiv:2401.12345', 'https://example.org/paper-notes'],
            'notes': ['Focus on the reported baseline and evaluation method.'],
        },
    )

    assert create.status_code == 201
    payload = create.json()
    intake_id = payload['intake_id']
    assert payload['status'] == 'ready_for_design'
    assert payload['source_type'] == 'paper-link'
    assert 'literature-to-experiment' in payload['workflow_family_candidates']
    assert 'generic-tabular-benchmark' in payload['workflow_family_candidates']

    latest = client.get('/intakes/latest')
    assert latest.status_code == 200
    assert latest.json()['intake_id'] == intake_id

    fetched = client.get(f'/intakes/{intake_id}')
    assert fetched.status_code == 200
    assert fetched.json()['normalized_summary'].startswith('Take this paper note set')


def test_create_session_scoped_intake_updates_latest_session_intake() -> None:
    client = build_client()

    session = client.post(
        '/research-sessions',
        json={'goal_statement': 'Bind a manually created intake to the current research session.'},
    )
    assert session.status_code == 201
    session_id = session.json()['session_id']

    create = client.post(
        f'/research-sessions/{session_id}/intakes',
        json={
            'raw_request': 'Benchmark a bounded set of approved Titanic baselines and compare one small methodology variant.',
            'source_refs': ['https://example.org/titanic-method-note'],
            'notes': ['Session-scoped intake smoke fixture.'],
        },
    )

    assert create.status_code == 201
    payload = create.json()
    assert payload['session_id'] == session_id

    latest = client.get(f'/research-sessions/{session_id}/intake')
    assert latest.status_code == 200
    assert latest.json()['intake_id'] == payload['intake_id']


def test_stage_next_paper_intake_supports_session_scoped_route() -> None:
    client = build_client()

    session = client.post(
        '/research-sessions',
        json={'goal_statement': 'Stage a manual paper candidate through the pinned session route.'},
    )
    assert session.status_code == 201
    session_id = session.json()['session_id']

    manual = client.post(
        f'/research-sessions/{session_id}/paper-intake-queue/manual-paper',
        json={
            'title': 'Manual metric learning paper',
            'official_page': 'https://example.org/metric-learning-paper',
            'notes': ['Pinned-session next-paper regression test.'],
            'tags': ['manual'],
        },
    )
    assert manual.status_code == 201

    staged = client.post(f'/research-sessions/{session_id}/paper-intake-queues/stage-next-intake')
    assert staged.status_code == 201
    staged_payload = staged.json()
    assert staged_payload['session_id'] == session_id
    assert staged_payload['status'] == 'ready_for_design'
    assert staged_payload['source_type'] == 'paper-link'
    assert 'literature-derived experiment' not in staged_payload['raw_request']
    assert not any(note.startswith('Selected tracks:') for note in staged_payload['notes'])
    assert 'Manually added by the operator.' not in staged_payload['notes']


def test_session_source_document_ingest_bootstraps_intake_for_run(monkeypatch) -> None:
    client = build_client()

    monkeypatch.setattr(
        source_documents,
        'fetch_source_document_bytes',
        lambda source_url: (
            b'<html><title>DreamSim</title><body>DreamSim uses contrastive loss with timm and torch.</body></html>',
            'text/html',
        ),
    )
    monkeypatch.setattr(
        source_documents,
        'persist_source_document_bytes',
        lambda **kwargs: 'file:///tmp/source.html',
    )
    monkeypatch.setattr(main_module, 'ingest_source_document', source_documents.ingest_source_document)

    session = client.post(
        '/research-sessions',
        json={'goal_statement': 'Develop an artist similarity metric from an attached source.'},
    )
    assert session.status_code == 201
    session_id = session.json()['session_id']

    ingest = client.post(
        f'/research-sessions/{session_id}/source-documents/ingest',
        json={'source_url': 'https://example.org/dreamsim.html', 'submitted_by': 'test-suite'},
    )
    assert ingest.status_code == 201
    document_payload = ingest.json()
    assert document_payload['session_id'] == session_id
    assert document_payload['status'] == 'fetched'

    design = client.post(f'/research-sessions/{session_id}/skills/design')
    assert design.status_code == 201

    intake = client.get(f'/research-sessions/{session_id}/intake')
    assert intake.status_code == 200
    intake_payload = intake.json()
    assert intake_payload['session_id'] == session_id
    assert document_payload['document_id'] in intake_payload['document_refs']
    assert 'https://example.org/dreamsim.html' in intake_payload['source_refs']
    assert any(note.startswith('Attached source:') for note in intake_payload['notes'])


def test_json_store_persists_intake_across_restart(tmp_path) -> None:
    state_path = tmp_path / 'run-store.json'
    settings = Settings(
        registry_dir=str(REPO_ROOT / 'services' / 'workflow-registry' / 'definitions'),
        store_backend='json',
        store_json_path=str(state_path),
    )
    registry = WorkflowRegistry(settings.registry_dir)

    first_client = TestClient(create_app(settings=settings, registry=registry))
    create = first_client.post(
        '/intakes',
        json={
            'raw_request': 'Turn this literature note into a bounded experiment.',
            'source_refs': ['https://example.org/paper-notes'],
            'notes': ['Persist this intake.'],
        },
    )

    assert create.status_code == 201
    intake_id = create.json()['intake_id']
    assert state_path.exists()

    second_client = TestClient(create_app(settings=settings, registry=registry))
    latest = second_client.get('/intakes/latest')
    assert latest.status_code == 200
    assert latest.json()['intake_id'] == intake_id


def test_session_intake_endpoint_accepts_note_dataset_and_source(monkeypatch) -> None:
    client = build_client()

    session = client.post(
        '/research-sessions',
        json={'goal_statement': 'Keep a single bounded session and add structured context.'},
    )
    assert session.status_code == 201
    session_id = session.json()['session_id']

    note = client.post(
        f'/research-sessions/{session_id}/intake',
        json={'note': 'keep timm backbones only'},
    )
    assert note.status_code == 200
    assert note.json()['record_type'] == 'note'
    assert note.json()['recorded_value'] == 'keep timm backbones only'

    dataset = client.post(
        f'/research-sessions/{session_id}/intake',
        json={'dataset_uri': 's3://datasets/paintings/v1'},
    )
    assert dataset.status_code == 200
    assert dataset.json()['record_type'] == 'dataset'
    assert dataset.json()['dataset']['uri'] == 's3://datasets/paintings/v1'

    monkeypatch.setattr(
        source_documents,
        'fetch_source_document_bytes',
        lambda source_url: (b'<html><title>Paper</title><body>bounded source</body></html>', 'text/html'),
    )
    monkeypatch.setattr(
        source_documents,
        'persist_source_document_bytes',
        lambda **kwargs: 'file:///tmp/source.html',
    )
    monkeypatch.setattr(main_module, 'ingest_source_document', source_documents.ingest_source_document)

    source = client.post(
        f'/research-sessions/{session_id}/intake',
        json={'source_url': 'https://example.org/paper.html'},
    )
    assert source.status_code == 200
    assert source.json()['record_type'] == 'source_document'
    assert source.json()['source_document']['source_url'] == 'https://example.org/paper.html'


def test_prepare_current_plan_and_current_preflight_aliases() -> None:
    client = build_client()

    session = client.post(
        '/research-sessions',
        json={'goal_statement': 'Benchmark bounded Titanic baselines in one current plan.'},
    )
    assert session.status_code == 201
    session_id = session.json()['session_id']

    intake = client.post(
        f'/research-sessions/{session_id}/intakes',
        json={
            'raw_request': 'Benchmark approved Titanic baselines and compare one small bounded variant.',
            'source_refs': ['https://example.org/titanic-note'],
            'notes': ['Current-plan alias regression test.'],
        },
    )
    assert intake.status_code == 201

    plan = client.post(f'/research-sessions/{session_id}/transitions/prepare-current-plan')
    assert plan.status_code == 201
    assert plan.json()['design_id']

    check = client.get(f'/research-sessions/{session_id}/preflight/current-plan')
    assert check.status_code == 200
    assert 'ready' in check.json()


def test_current_decision_endpoint_persists_operation_and_session_log() -> None:
    client = build_client()

    session = client.post(
        '/research-sessions',
        json={'goal_statement': 'Record a bounded human decision for the current session.'},
    )
    assert session.status_code == 201
    session_id = session.json()['session_id']

    decide = client.post(
        f'/research-sessions/{session_id}/decisions/current',
        json={'decision': 'keep', 'note': 'use the stronger baseline next'},
    )
    assert decide.status_code == 200
    payload = decide.json()
    assert payload['operation']['operation_type'] == 'session-decision'
    assert "keep: use the stronger baseline next" in payload['session']['decision_log']


def test_import_and_query_technique_catalog() -> None:
    client = build_client()

    imported = client.post(
        '/technique-catalog/import',
        json={
            'cards': [
                {
                    'name': 'PyTorch Vision Transformer',
                    'aliases': ['vit', 'vision transformer'],
                    'problem_types': ['multiclass_classification'],
                    'algorithm_family': 'transformers',
                    'specific_algorithms': ['vit_b16'],
                    'validation_strategies': ['holdout', 'k_fold_cv'],
                    'primary_metrics': ['accuracy', 'f1_score'],
                    'python_packages': ['torch', 'timm'],
                    'gpu_required': True,
                    'resource_profile': 'gpu-small',
                    'workflow_ids': ['gpu-experiment'],
                    'common_failure_modes': ['overfitting_without_stratified_split'],
                    'source_refs': ['notebooklm://vision-transformer-card'],
                }
            ],
            'import_source': 'notebooklm-manual-export',
        },
    )
    assert imported.status_code == 201
    payload = imported.json()
    assert len(payload) == 1
    technique_id = payload[0]['technique_id']
    assert payload[0]['workflow_ids'] == ['gpu-experiment']

    listed = client.get('/technique-catalog')
    assert listed.status_code == 200
    assert listed.json()[0]['technique_id'] == technique_id

    queried = client.get('/technique-catalog', params={'query': 'transformer'})
    assert queried.status_code == 200
    assert queried.json()[0]['technique_id'] == technique_id

    fetched = client.get(f'/technique-catalog/{technique_id}')
    assert fetched.status_code == 200
    assert fetched.json()['python_packages'] == ['torch', 'timm']


def test_technique_catalog_import_upserts_by_name() -> None:
    client = build_client()

    first = client.post(
        '/technique-catalog/import',
        json={
            'cards': [
                {
                    'name': 'DreamSim Transformer Similarity',
                    'python_packages': ['torch'],
                }
            ]
        },
    )
    assert first.status_code == 201
    first_payload = first.json()[0]

    second = client.post(
        '/technique-catalog/import',
        json={
            'cards': [
                {
                    'name': 'DreamSim Transformer Similarity',
                    'python_packages': ['torch', 'timm'],
                    'default_dataset_uri': 's3://datasets/dreamsim/train.csv',
                }
            ]
        },
    )
    assert second.status_code == 201
    second_payload = second.json()[0]

    assert second_payload['technique_id'] == first_payload['technique_id']
    listed = client.get('/technique-catalog', params={'query': 'DreamSim Transformer Similarity'})
    assert listed.status_code == 200
    listed_payload = listed.json()
    assert len(listed_payload) == 1
    assert listed_payload[0]['python_packages'] == ['torch', 'timm']
    assert listed_payload[0]['default_dataset_uri'] == 's3://datasets/dreamsim/train.csv'


def test_technique_catalog_search_prefers_newest_most_complete_duplicate() -> None:
    client = build_client()

    client.post(
        '/technique-catalog/import',
        json={'cards': [{'name': 'DreamSim Transformer Similarity', 'aliases': ['dreamsim'], 'python_packages': ['torch']}]},
    )
    client.post(
        '/technique-catalog/import',
        json={
            'cards': [
                {
                    'name': 'DreamSim Transformer Similarity',
                    'aliases': ['dreamsim', 'visual similarity metric'],
                    'python_packages': ['torch', 'timm'],
                    'workflow_ids': ['gpu-experiment'],
                    'default_dataset_uri': 's3://datasets/dreamsim/train.csv',
                }
            ]
        },
    )

    results = client.get('/technique-catalog', params={'query': 'dreamsim'})
    assert results.status_code == 200
    payload = results.json()
    assert len(payload) == 1
    assert payload[0]['default_dataset_uri'] == 's3://datasets/dreamsim/train.csv'
    assert payload[0]['workflow_ids'] == ['gpu-experiment']


def test_interpretation_is_enriched_from_technique_catalog() -> None:
    client = build_client()

    imported = client.post(
        '/technique-catalog/import',
        json={
            'cards': [
                {
                    'name': 'DreamSim Transformer Similarity',
                    'aliases': ['dreamsim', 'visual similarity metric'],
                    'problem_types': ['multiclass_classification'],
                    'algorithm_family': 'transformers',
                    'specific_algorithms': ['vision_transformer'],
                    'loss_functions': ['contrastive_loss'],
                    'validation_strategies': ['stratified_holdout'],
                    'primary_metrics': ['accuracy', 'roc_auc'],
                    'python_packages': ['torch', 'timm'],
                    'gpu_required': True,
                    'resource_profile': 'gpu-small',
                    'workflow_ids': ['gpu-experiment'],
                    'common_failure_modes': ['overfitting_without_artist_aware_split'],
                    'source_refs': ['notebooklm://dreamsim-technique-card'],
                }
            ]
        },
    )
    assert imported.status_code == 201

    intake = client.post(
        '/intakes',
        json={
            'raw_request': 'Replicate DreamSim visual similarity metric with a vision transformer using s3://datasets/dreamsim/train.csv.',
            'source_refs': ['https://dreamsim-nights.github.io/'],
            'notes': ['Prefer PyTorch and timm for the implementation.'],
        },
    )
    assert intake.status_code == 201

    interpretation = client.post('/interpretations/from-latest-intake')
    assert interpretation.status_code == 201
    payload = interpretation.json()
    assert 'torch' in payload['technique_knowledge']['python_packages']
    assert 'timm' in payload['technique_knowledge']['python_packages']
    assert payload['technique_knowledge']['catalog_technique_ids']
    assert payload['preferred_workflow_id'] == 'gpu-experiment'
    assert payload['preferred_resource_profile'] == 'gpu-small'
    assert payload['gpu_required'] is True


def test_agent_interpretation_catalog_match_overrides_weaker_workflow_hint(monkeypatch) -> None:
    client = build_client()

    imported = client.post(
        '/technique-catalog/import',
        json={
            'cards': [
                {
                    'name': 'DreamSim Transformer Similarity',
                    'aliases': ['dreamsim', 'visual similarity metric'],
                    'algorithm_family': 'transformers',
                    'specific_algorithms': ['vision_transformer'],
                    'loss_functions': ['contrastive_loss'],
                    'validation_strategies': ['stratified_holdout'],
                    'primary_metrics': ['accuracy', 'roc_auc'],
                    'python_packages': ['torch', 'timm'],
                    'gpu_required': True,
                    'resource_profile': 'gpu-small',
                    'workflow_ids': ['gpu-experiment'],
                }
            ]
        },
    )
    assert imported.status_code == 201

    intake = client.post(
        '/intakes',
        json={
            'raw_request': 'Replicate DreamSim visual similarity metric with PyTorch and timm.',
            'source_refs': ['manual:dreamsim'],
            'technique_tags': ['dreamsim', 'visual_similarity', 'transformers', 'contrastive_loss'],
        },
    )
    assert intake.status_code == 201

    def fake_call_interpretation_agent(intake, settings, registry, store):
        draft = {
            'source_type': intake.source_type,
            'normalized_summary': intake.normalized_summary,
            'extracted_method_summary': 'Agent weakly suggested replication-lite.',
            'literature_state_summary': intake.normalized_summary,
            'candidate_workflow_families': ['replication-lite', 'literature-to-experiment'],
            'dataset_hints': [],
            'evaluation_targets': ['reported metrics'],
            'extracted_claims': [intake.normalized_summary],
            'research_gaps': ['dataset unresolved'],
            'bounded_experiment_ideas': ['keep bounded'],
            'recommended_method_family': 'lightweight replication',
            'recommended_datasets': [],
            'recommended_metrics': ['reported metrics'],
            'recommended_baselines': [],
            'recommended_architectures': [],
            'recommended_python_packages': [],
            'preferred_workflow_id': 'replication-lite',
            'preferred_resource_profile': 'cpu-medium',
            'gpu_required': False,
            'mutation_axes': ['metric emphasis'],
            'unresolved_questions': ['Which concrete dataset should the backend use?'],
        }
        return build_interpretation_record_from_agent_draft(
            intake,
            draft,
            store=store,
            interpretation_source='agent-fallback',
            interpretation_backend={'provider': 'ollama', 'model': 'qwen3:14b'},
            interpretation_warnings=['used fallback interpretation backend'],
        )

    monkeypatch.setattr(main_module, 'call_interpretation_agent', fake_call_interpretation_agent)

    interpretation = client.post('/interpretations/from-latest-intake')
    assert interpretation.status_code == 201
    payload = interpretation.json()
    assert payload['preferred_workflow_id'] == 'gpu-experiment'
    assert payload['preferred_resource_profile'] == 'gpu-small'
    assert payload['gpu_required'] is True
    assert 'gpu-experiment' in payload['candidate_workflow_families']


def test_intake_preserves_explicit_technique_tags() -> None:
    client = build_client()

    intake = client.post(
        '/intakes',
        json={
            'raw_request': 'Evaluate similarity-learning methods for same-artist versus different-artist comparison.',
            'source_refs': ['https://example.org/artist-problem'],
            'technique_tags': ['dreamsim', 'metric_learning', 'artist_aware_split'],
            'notes': ['Keep this bounded and GPU-aware.'],
        },
    )
    assert intake.status_code == 201
    payload = intake.json()
    assert payload['technique_tags'] == ['dreamsim', 'metric_learning', 'artist_aware_split']


def test_session_interpretation_bootstraps_intake_from_goal_and_tags() -> None:
    client = build_client()

    imported = client.post(
        '/technique-catalog/import',
        json={
            'cards': [
                {
                    'name': 'DreamSim Transformer Similarity',
                    'aliases': ['dreamsim', 'visual similarity metric'],
                    'algorithm_family': 'transformers',
                    'specific_algorithms': ['vision_transformer'],
                    'loss_functions': ['contrastive_loss'],
                    'validation_strategies': ['stratified_holdout'],
                    'primary_metrics': ['roc_auc'],
                    'python_packages': ['torch', 'timm'],
                    'gpu_required': True,
                    'resource_profile': 'gpu-small',
                    'workflow_ids': ['gpu-experiment'],
                    'default_dataset_uri': 's3://datasets/dreamsim/train.csv',
                    'default_evaluation_target': 'embedding retrieval auc',
                }
            ]
        },
    )
    assert imported.status_code == 201

    session = client.post(
        '/research-sessions',
        json={'goal_statement': 'Replicate DreamSim visual similarity metric with PyTorch and timm.'},
    )
    assert session.status_code == 201
    session_id = session.json()['session_id']

    interpretation = client.post(f'/research-sessions/{session_id}/skills/interpretation')
    assert interpretation.status_code == 201
    payload = interpretation.json()
    assert payload['preferred_workflow_id'] == 'gpu-experiment'
    assert payload['method_spec']['execution_inputs']['dataset_uri'] == 's3://datasets/dreamsim/train.csv'

    intake = client.get(f'/research-sessions/{session_id}/intake')
    assert intake.status_code == 200
    assert 'dreamsim' in intake.json()['technique_tags']


def test_session_design_bootstraps_interpretation_from_goal_backed_intake() -> None:
    client = build_client()

    imported = client.post(
        '/technique-catalog/import',
        json={
            'cards': [
                {
                    'name': 'DreamSim Transformer Similarity',
                    'aliases': ['dreamsim', 'visual similarity metric'],
                    'algorithm_family': 'transformers',
                    'specific_algorithms': ['vision_transformer'],
                    'loss_functions': ['contrastive_loss'],
                    'validation_strategies': ['stratified_holdout'],
                    'primary_metrics': ['roc_auc'],
                    'python_packages': ['torch', 'timm'],
                    'gpu_required': True,
                    'resource_profile': 'gpu-small',
                    'workflow_ids': ['gpu-experiment'],
                    'default_dataset_uri': 's3://datasets/dreamsim/train.csv',
                    'default_evaluation_target': 'embedding retrieval auc',
                }
            ]
        },
    )
    assert imported.status_code == 201

    session = client.post(
        '/research-sessions',
        json={'goal_statement': 'Replicate DreamSim visual similarity metric with PyTorch and timm.'},
    )
    assert session.status_code == 201
    session_id = session.json()['session_id']

    design = client.post(f'/research-sessions/{session_id}/skills/design')
    assert design.status_code == 201
    payload = design.json()
    assert payload['workflow_id'] == 'gpu-experiment'
    assert payload['status'] == 'ready_for_run'
    assert payload['method_spec']['run_readiness'] == 'ready'

    interpretation = client.get(f'/research-sessions/{session_id}/interpretation')
    assert interpretation.status_code == 200
    assert interpretation.json()['preferred_workflow_id'] == 'gpu-experiment'


def test_autoresearch_drafts_technique_catalog_variant(tmp_path) -> None:
    settings = Settings(
        registry_dir=str(REPO_ROOT / 'services' / 'workflow-registry' / 'definitions'),
        artifacts_mount_path=str(tmp_path),
    )
    registry = WorkflowRegistry(settings.registry_dir)
    store = InMemoryRunStore()
    client = TestClient(create_app(settings=settings, registry=registry, store=store))

    imported = client.post(
        '/technique-catalog/import',
        json={
            'cards': [
                {
                    'name': 'DreamSim Transformer Similarity',
                    'aliases': ['dreamsim', 'visual similarity metric'],
                    'algorithm_family': 'transformers',
                    'specific_algorithms': ['vision_transformer'],
                    'loss_functions': ['contrastive_loss'],
                    'validation_strategies': ['stratified_holdout'],
                    'primary_metrics': ['roc_auc'],
                    'python_packages': ['torch', 'timm'],
                    'gpu_required': True,
                    'resource_profile': 'gpu-small',
                    'workflow_ids': ['gpu-experiment'],
                }
            ]
        },
    )
    assert imported.status_code == 201

    session = client.post(
        '/research-sessions',
        json={'goal_statement': 'Validate technique-catalog-driven DreamSim methodology variants.'},
    )
    session_id = session.json()['session_id']

    intake = client.post(
        f'/research-sessions/{session_id}/intakes',
        json={
            'raw_request': 'Replicate DreamSim visual similarity metric using s3://datasets/dreamsim/train.csv.',
            'source_refs': ['https://dreamsim-nights.github.io/'],
            'technique_tags': ['dreamsim', 'vision_transformer', 'metric_learning'],
            'notes': ['Prefer PyTorch and timm.'],
        },
    )
    assert intake.status_code == 201

    interpretation = client.post('/interpretations/from-latest-intake')
    assert interpretation.status_code == 201
    design = client.post('/design-drafts/from-latest-intake')
    assert design.status_code == 201

    store.save_research_session(
        store.get_research_session(session_id).model_copy(update={'latest_design_id': design.json()['design_id']})
    )

    campaign = client.post(f'/research-sessions/{session_id}/transitions/start-autoresearch-campaign')
    assert campaign.status_code == 201
    drafted = client.post(f'/research-sessions/{session_id}/transitions/draft-methodologies')
    assert drafted.status_code == 201
    drafts = drafted.json()['methodology_drafts']
    assert any('technique-catalog variant' in ' '.join(draft['notes']) for draft in drafts)
    assert any('torch' in draft['method_spec']['required_python_packages'] for draft in drafts if draft['method_spec'] is not None)


def test_gpu_autoresearch_launch_iteration_uses_allowed_runner_model(tmp_path) -> None:
    settings = Settings(
        registry_dir=str(REPO_ROOT / 'services' / 'workflow-registry' / 'definitions'),
        artifacts_mount_path=str(tmp_path),
    )
    registry = WorkflowRegistry(settings.registry_dir)
    store = InMemoryRunStore()
    client = TestClient(create_app(settings=settings, registry=registry, store=store))

    imported = client.post(
        '/technique-catalog/import',
        json={
            'cards': [
                {
                    'name': 'DreamSim Transformer Similarity',
                    'aliases': ['dreamsim', 'visual similarity metric'],
                    'algorithm_family': 'transformers',
                    'specific_algorithms': ['vision_transformer'],
                    'loss_functions': ['contrastive_loss'],
                    'validation_strategies': ['stratified_holdout'],
                    'primary_metrics': ['roc_auc'],
                    'python_packages': ['torch', 'timm'],
                    'gpu_required': True,
                    'resource_profile': 'gpu-small',
                    'workflow_ids': ['gpu-experiment'],
                    'default_dataset_uri': 's3://datasets/dreamsim/train.csv',
                    'default_evaluation_target': 'embedding retrieval auc',
                }
            ]
        },
    )
    assert imported.status_code == 201

    session = client.post(
        '/research-sessions',
        json={'goal_statement': 'Replicate DreamSim visual similarity metric with PyTorch and timm.'},
    )
    session_id = session.json()['session_id']

    design = client.post(f'/research-sessions/{session_id}/skills/design')
    assert design.status_code == 201

    campaign = client.post(f'/research-sessions/{session_id}/transitions/start-autoresearch-campaign')
    assert campaign.status_code == 201
    drafted = client.post(f'/research-sessions/{session_id}/transitions/draft-methodologies')
    assert drafted.status_code == 201

    launched = client.post(f'/research-sessions/{session_id}/transitions/launch-autoresearch-iteration')
    assert launched.status_code == 201
    payload = launched.json()
    assert payload['run']['workflow_id'] == 'gpu-experiment'
    assert payload['run']['manifest']['requested_models'] == ['pytorch-template-v1']
    assert payload['run']['manifest']['inputs']['evaluation_target'] == 'embedding retrieval auc'
    assert payload['run']['manifest']['inputs']['validation_strategy'] == 'stratified_holdout'


def test_gpu_technique_card_can_fill_executable_contract() -> None:
    client = build_client()

    imported = client.post(
        '/technique-catalog/import',
        json={
            'cards': [
                {
                    'name': 'DreamSim Transformer Similarity',
                    'aliases': ['dreamsim', 'visual similarity metric'],
                    'algorithm_family': 'transformers',
                    'specific_algorithms': ['vision_transformer'],
                    'loss_functions': ['contrastive_loss'],
                    'validation_strategies': ['stratified_holdout'],
                    'primary_metrics': ['roc_auc'],
                    'python_packages': ['torch', 'timm'],
                    'gpu_required': True,
                    'resource_profile': 'gpu-small',
                    'workflow_ids': ['gpu-experiment'],
                    'dataset_hints': ['dreamsim'],
                    'default_dataset_uri': 's3://datasets/dreamsim/train.csv',
                    'default_evaluation_target': 'embedding retrieval auc',
                    'default_training_notes': 'Train a bounded DreamSim-style embedding model on the approved dataset.',
                    'default_execution_inputs': {
                        'pair_strategy': 'artist_positive_negative_pairs',
                        'evaluation_protocol': 'same_artist_verification',
                        'label_field': 'artist_id',
                    },
                }
            ]
        },
    )
    assert imported.status_code == 201

    intake = client.post(
        '/intakes',
        json={
            'raw_request': 'Replicate DreamSim visual similarity metric with PyTorch and timm.',
            'source_refs': ['manual:dreamsim'],
            'technique_tags': ['dreamsim', 'visual_similarity', 'transformers', 'contrastive_loss'],
        },
    )
    assert intake.status_code == 201

    interpretation = client.post('/interpretations/from-latest-intake')
    assert interpretation.status_code == 201
    interpretation_payload = interpretation.json()
    assert interpretation_payload['preferred_workflow_id'] == 'gpu-experiment'
    assert interpretation_payload['method_spec']['execution_inputs']['dataset_uri'] == 's3://datasets/dreamsim/train.csv'
    assert interpretation_payload['method_spec']['execution_inputs']['evaluation_target'] == 'embedding retrieval auc'
    assert interpretation_payload['method_spec']['execution_inputs']['training_notes'].startswith('Train a bounded DreamSim-style embedding model')
    assert interpretation_payload['method_spec']['execution_inputs']['pair_strategy'] == 'artist_positive_negative_pairs'
    assert interpretation_payload['method_spec']['execution_inputs']['evaluation_protocol'] == 'same_artist_verification'
    assert interpretation_payload['method_spec']['execution_inputs']['label_field'] == 'artist_id'

    design = client.post('/design-drafts/from-latest-intake')
    assert design.status_code == 201
    design_payload = design.json()
    assert design_payload['workflow_id'] == 'gpu-experiment'
    assert design_payload['status'] == 'ready_for_run'
    assert design_payload['method_spec']['run_readiness'] == 'ready'
    assert design_payload['declared_inputs']['dataset_uri'] == 's3://datasets/dreamsim/train.csv'
    assert design_payload['declared_inputs']['evaluation_target'] == 'embedding retrieval auc'
    assert design_payload['declared_inputs']['pair_strategy'] == 'artist_positive_negative_pairs'
    assert design_payload['declared_inputs']['evaluation_protocol'] == 'same_artist_verification'

    run = client.post('/runs/from-latest-design-draft')
    assert run.status_code == 201
    run_payload = run.json()
    assert run_payload['workflow_id'] == 'gpu-experiment'
    assert run_payload['manifest']['inputs']['pair_strategy'] == 'artist_positive_negative_pairs'
    assert run_payload['manifest']['inputs']['evaluation_protocol'] == 'same_artist_verification'
    assert run_payload['manifest']['inputs']['label_field'] == 'artist_id'


def test_validate_interpretation_agent_draft_does_not_stringify_none_optionals() -> None:
    registry = WorkflowRegistry(str(REPO_ROOT / 'services' / 'workflow-registry' / 'definitions'))
    intake = build_client().post(
        '/intakes',
        json={
            'raw_request': 'Compare image metric learning methods.',
            'source_refs': ['manual:artist-similarity-v1'],
        },
    ).json()

    normalized = validate_interpretation_agent_draft(
        {
            'source_type': 'manual-note',
            'normalized_summary': 'Compare image metric learning methods.',
            'extracted_method_summary': 'Metric-learning framing for image similarity.',
            'literature_state_summary': 'No linked paper yet.',
            'candidate_workflow_families': ['gpu-experiment'],
            'recommended_method_family': None,
            'preferred_workflow_id': None,
            'preferred_resource_profile': None,
        },
        intake=intake,
        registry=registry,
    )

    assert normalized['recommended_method_family'] is None
    assert normalized['preferred_workflow_id'] is None
    assert normalized['preferred_resource_profile'] is None


def test_create_app_rejects_implicit_memory_store_when_disallowed() -> None:
    try:
        Settings(
            registry_dir=str(REPO_ROOT / 'services' / 'workflow-registry' / 'definitions'),
            store_backend='memory',
            allow_inmemory_store=False,
        )
    except Exception as exc:
        assert 'allow_inmemory_store=false' in str(exc)
    else:
        raise AssertionError('expected Settings validation to reject implicit in-memory store')


def test_create_intake_uses_agent_when_enabled(monkeypatch) -> None:
    settings = Settings(
        registry_dir=str(REPO_ROOT / 'services' / 'workflow-registry' / 'definitions'),
        intake_agent_enabled=True,
    )
    registry = WorkflowRegistry(settings.registry_dir)
    store = InMemoryRunStore()
    client = TestClient(create_app(settings=settings, registry=registry, store=store))

    def fake_call_intake_agent(request, settings, registry):
        return main_module.build_intake_record_from_agent_draft(
            {
                'source_type': 'paper-link',
                'source_refs': ['https://example.org/agent-paper'],
                'raw_request': request.raw_request,
                'normalized_summary': 'agent-normalized intake summary',
                'workflow_family_candidates': ['literature-to-experiment'],
                'notes': ['Agent normalized note'],
                'submitted_by': 'agent-operator',
            }
        )

    monkeypatch.setattr(main_module, 'call_intake_agent', fake_call_intake_agent)

    create = client.post(
        '/intakes',
        json={
            'raw_request': 'Take this paper note set and turn it into a bounded validation experiment on a tabular dataset.',
            'source_refs': ['https://example.org/paper-notes'],
            'notes': ['Focus on the reported baseline and evaluation method.'],
        },
    )

    assert create.status_code == 201
    payload = create.json()
    assert payload['normalized_summary'] == 'agent-normalized intake summary'
    assert payload['workflow_family_candidates'] == ['literature-to-experiment']
    assert payload['submitted_by'] == 'agent-operator'


def test_create_intake_falls_back_when_agent_returns_none(monkeypatch) -> None:
    settings = Settings(
        registry_dir=str(REPO_ROOT / 'services' / 'workflow-registry' / 'definitions'),
        intake_agent_enabled=True,
    )
    registry = WorkflowRegistry(settings.registry_dir)
    store = InMemoryRunStore()
    client = TestClient(create_app(settings=settings, registry=registry, store=store))

    monkeypatch.setattr(main_module, 'call_intake_agent', lambda request, settings, registry: None)

    create = client.post(
        '/intakes',
        json={
            'raw_request': 'Take this paper note set and turn it into a bounded validation experiment on a tabular dataset.',
            'source_refs': ['https://example.org/paper-notes'],
            'notes': ['Focus on the reported baseline and evaluation method.'],
        },
    )

    assert create.status_code == 201
    payload = create.json()
    assert payload['normalized_summary'].startswith('Take this paper note set')
    assert 'generic-tabular-benchmark' in payload['workflow_family_candidates']


def test_create_intake_uses_ranker_when_scores_are_strong(monkeypatch) -> None:
    settings = Settings(
        registry_dir=str(REPO_ROOT / 'services' / 'workflow-registry' / 'definitions'),
        ranker_enabled=True,
        ranker_min_top_score=0.75,
        ranker_min_score_gap=0.10,
    )
    registry = WorkflowRegistry(settings.registry_dir)
    store = InMemoryRunStore()
    client = TestClient(create_app(settings=settings, registry=registry, store=store))

    class FakeResponse:
        def __init__(self, payload: dict) -> None:
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            import json
            return json.dumps(self.payload).encode('utf-8')

    def fake_urlopen(request_obj, timeout):
        return FakeResponse(
            {
                'request_id': 'ignored',
                'ranked_candidates': [
                    {
                        'workflow_id': 'generic-tabular-benchmark',
                        'score': 0.92,
                        'reason': 'strong tabular benchmark signal',
                    },
                    {
                        'workflow_id': 'literature-to-experiment',
                        'score': 0.61,
                        'reason': 'paper-oriented fallback',
                    },
                ],
                'ranking_basis': 'test fixture',
            }
        )

    monkeypatch.setattr(main_module.urllib_request, 'urlopen', fake_urlopen)

    create = client.post(
        '/intakes',
        json={
            'raw_request': 'Take this paper note set and turn it into a bounded validation experiment on a tabular dataset.',
            'source_refs': ['https://example.org/paper-notes'],
            'notes': ['Focus on the reported baseline and evaluation method.'],
        },
    )

    assert create.status_code == 201
    payload = create.json()
    assert payload['workflow_family_candidates'] == [
        'generic-tabular-benchmark',
        'literature-to-experiment',
    ]


def test_create_intake_ignores_ranker_when_scores_are_ambiguous(monkeypatch) -> None:
    settings = Settings(
        registry_dir=str(REPO_ROOT / 'services' / 'workflow-registry' / 'definitions'),
        ranker_enabled=True,
        ranker_min_top_score=0.75,
        ranker_min_score_gap=0.10,
    )
    registry = WorkflowRegistry(settings.registry_dir)
    store = InMemoryRunStore()
    client = TestClient(create_app(settings=settings, registry=registry, store=store))

    class FakeResponse:
        def __init__(self, payload: dict) -> None:
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            import json
            return json.dumps(self.payload).encode('utf-8')

    def fake_urlopen(request_obj, timeout):
        import json
        from urllib.error import URLError
        body = json.loads(request_obj.data.decode('utf-8'))
        if 'problem_statement' not in body:
            raise URLError('agent unavailable in test fixture')
        return FakeResponse(
            {
                'request_id': 'ignored',
                'ranked_candidates': [
                    {
                        'workflow_id': 'generic-tabular-benchmark',
                        'score': 0.78,
                        'reason': 'slightly preferred',
                    },
                    {
                        'workflow_id': 'literature-to-experiment',
                        'score': 0.74,
                        'reason': 'close alternative',
                    },
                ],
                'ranking_basis': 'test fixture',
            }
        )

    monkeypatch.setattr(main_module.urllib_request, 'urlopen', fake_urlopen)

    create = client.post(
        '/intakes',
        json={
            'raw_request': 'Take this paper note set and turn it into a bounded validation experiment on a tabular dataset.',
            'source_refs': ['https://example.org/paper-notes'],
            'notes': ['Focus on the reported baseline and evaluation method.'],
        },
    )

    assert create.status_code == 201
    payload = create.json()
    assert payload['workflow_family_candidates'] == [
        'literature-to-experiment',
        'generic-tabular-benchmark',
    ]


def test_get_latest_intake_missing() -> None:
    client = build_client()

    latest = client.get('/intakes/latest')
    assert latest.status_code == 404
    assert latest.json()['detail'] == 'intake not found'


def test_create_and_fetch_interpretation_from_latest_intake() -> None:
    client = build_client()

    create_intake = client.post(
        '/intakes',
        json={
            'raw_request': 'Read this paper intake and determine whether the approved Titanic benchmark path is a good fit.',
            'source_refs': ['https://example.org/titanic-paper'],
            'notes': [
                'The paper compares a baseline on Titanic.',
                'Focus on the reported metrics and dataset assumptions.',
            ],
        },
    )
    assert create_intake.status_code == 201
    intake_id = create_intake.json()['intake_id']

    create_interpretation = client.post('/interpretations/from-latest-intake')
    assert create_interpretation.status_code == 201
    payload = create_interpretation.json()
    interpretation_id = payload['interpretation_id']
    assert payload['intake_id'] == intake_id
    assert 'generic-tabular-benchmark' in payload['candidate_workflow_families']
    assert 'titanic' in payload['dataset_hints']
    assert payload['literature_state_summary'].startswith('Current bounded literature view:')
    assert payload['bounded_experiment_ideas']
    assert payload['status'] in {'ready_for_assessment', 'needs_review'}

    latest = client.get('/interpretations/latest')
    assert latest.status_code == 200
    assert latest.json()['interpretation_id'] == interpretation_id

    fetched = client.get(f'/interpretations/{interpretation_id}')
    assert fetched.status_code == 200
    assert fetched.json()['extracted_claims'][0].startswith('The paper compares')


def test_create_interpretation_uses_agent_when_enabled(monkeypatch) -> None:
    settings = Settings(
        registry_dir=str(REPO_ROOT / 'services' / 'workflow-registry' / 'definitions'),
        interpretation_agent_enabled=True,
    )
    registry = WorkflowRegistry(settings.registry_dir)
    store = InMemoryRunStore()
    client = TestClient(create_app(settings=settings, registry=registry, store=store))

    create_intake = client.post(
        '/intakes',
        json={
            'raw_request': 'Read this paper intake and determine whether the approved Titanic benchmark path is a good fit.',
            'source_refs': ['https://example.org/titanic-paper'],
            'notes': ['The paper compares a baseline on Titanic.'],
        },
    )
    assert create_intake.status_code == 201

    def fake_call_interpretation_agent(intake, settings, registry, store):
        return main_module.build_interpretation_record_from_agent_draft(
            intake,
            {
                'source_type': intake.source_type,
                'normalized_summary': 'agent-normalized summary',
                'extracted_method_summary': 'agent-produced method summary',
                'literature_state_summary': 'agent literature-state summary',
                'candidate_workflow_families': ['generic-tabular-benchmark'],
                'dataset_hints': ['titanic'],
                'evaluation_targets': ['baseline comparison'],
                'extracted_claims': ['Agent extracted claim'],
                'research_gaps': ['Agent gap'],
                'bounded_experiment_ideas': ['Agent idea'],
                'unresolved_questions': [],
            },
        )

    monkeypatch.setattr(main_module, 'call_interpretation_agent', fake_call_interpretation_agent)

    create_interpretation = client.post('/interpretations/from-latest-intake')
    assert create_interpretation.status_code == 201
    payload = create_interpretation.json()
    assert payload['normalized_summary'] == 'agent-normalized summary'
    assert payload['extracted_method_summary'] == 'agent-produced method summary'
    assert payload['literature_state_summary'] == 'agent literature-state summary'
    assert payload['candidate_workflow_families'] == ['generic-tabular-benchmark']
    assert payload['research_gaps'] == ['Agent gap']
    assert payload['bounded_experiment_ideas'] == ['Agent idea']
    assert payload['status'] == 'ready_for_assessment'
    assert payload['interpretation_source'] == 'agent-primary'
    assert payload['interpretation_backend'] is None
    assert payload['interpretation_warnings'] == []


def test_create_interpretation_falls_back_when_agent_returns_none(monkeypatch) -> None:
    settings = Settings(
        registry_dir=str(REPO_ROOT / 'services' / 'workflow-registry' / 'definitions'),
        interpretation_agent_enabled=True,
    )
    registry = WorkflowRegistry(settings.registry_dir)
    store = InMemoryRunStore()
    client = TestClient(create_app(settings=settings, registry=registry, store=store))

    create_intake = client.post(
        '/intakes',
        json={
            'raw_request': 'Read this paper intake and determine whether the approved Titanic benchmark path is a good fit.',
            'source_refs': ['https://example.org/titanic-paper'],
            'notes': ['The paper compares a baseline on Titanic.'],
        },
    )
    assert create_intake.status_code == 201

    monkeypatch.setattr(main_module, 'call_interpretation_agent', lambda intake, settings, registry, store: None)

    create_interpretation = client.post('/interpretations/from-latest-intake')
    assert create_interpretation.status_code == 201
    payload = create_interpretation.json()
    assert 'generic-tabular-benchmark' in payload['candidate_workflow_families']
    assert payload['normalized_summary'].startswith('Read this paper intake')
    assert payload['interpretation_source'] == 'deterministic'
    assert payload['interpretation_backend'] is None
    assert payload['interpretation_warnings'] == []


def test_create_interpretation_records_agent_backend_metadata(monkeypatch) -> None:
    settings = Settings(
        registry_dir=str(REPO_ROOT / 'services' / 'workflow-registry' / 'definitions'),
        interpretation_agent_enabled=True,
    )
    registry = WorkflowRegistry(settings.registry_dir)
    store = InMemoryRunStore()
    client = TestClient(create_app(settings=settings, registry=registry, store=store))

    create_intake = client.post(
        '/intakes',
        json={
            'raw_request': 'Read this paper intake and determine whether the approved Titanic benchmark path is a good fit.',
            'source_refs': ['https://example.org/titanic-paper'],
            'notes': ['The paper compares a baseline on Titanic.'],
        },
    )
    assert create_intake.status_code == 201

    def fake_call_interpretation_agent(intake, settings, registry, store):
        return main_module.build_interpretation_record_from_agent_draft(
            intake,
            {
                'source_type': intake.source_type,
                'normalized_summary': 'agent-normalized summary',
                'extracted_method_summary': 'agent-produced method summary',
                'literature_state_summary': 'agent literature-state summary',
                'candidate_workflow_families': ['generic-tabular-benchmark'],
                'dataset_hints': ['titanic'],
                'evaluation_targets': ['baseline comparison'],
                'extracted_claims': ['Agent extracted claim'],
                'research_gaps': ['Agent gap'],
                'bounded_experiment_ideas': ['Agent idea'],
                'unresolved_questions': [],
            },
            interpretation_source='agent-fallback',
            interpretation_backend={
                'provider': 'ollama',
                'base_url': 'http://192.168.1.12:11434',
                'model': 'qwen3:14b',
                'timeout_seconds': 30.0,
            },
            interpretation_warnings=['used fallback interpretation backend'],
        )

    monkeypatch.setattr(main_module, 'call_interpretation_agent', fake_call_interpretation_agent)

    create_interpretation = client.post('/interpretations/from-latest-intake')
    assert create_interpretation.status_code == 201
    payload = create_interpretation.json()
    assert payload['interpretation_source'] == 'agent-fallback'
    assert payload['interpretation_backend']['base_url'] == 'http://192.168.1.12:11434'
    assert payload['interpretation_backend']['model'] == 'qwen3:14b'
    assert payload['interpretation_warnings'] == ['used fallback interpretation backend']


def test_create_interpretation_uses_stored_source_document_context(monkeypatch) -> None:
    client = build_client()

    class FakeResponse:
        def __init__(self, payload: dict) -> None:
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            import json
            return json.dumps(self.payload).encode('utf-8')

    monkeypatch.setattr(
        main_module.urllib_request,
        'urlopen',
        lambda request_obj, timeout: FakeResponse(
            {
                'request_id': 'paper-queue-docctx',
                'selected_tracks': [{'track_id': 'agent_evaluation', 'track': 'agent_evaluation'}],
                'selected_queries': [{'queries': ['site:arxiv.org machine learning agents benchmark Kaggle']}],
                'selected_papers': [
                    {
                        'paper_id': 'mle_bench_arxiv_2024',
                        'title': 'MLE-bench: Evaluating Machine Learning Agents on Machine Learning Engineering',
                        'year': 2024,
                        'venue': 'arXiv',
                        'priority': 'P1',
                        'tracks': ['agent_evaluation'],
                        'bounded_job_fit': 4,
                        'replication_complexity': 4,
                        'official_page': 'https://arxiv.org/abs/2410.07095',
                        'pdf_url': None,
                        'why_seed': 'benchmark for measuring whether agents can do bounded ML engineering work on real tasks',
                        'first_jobs': ['adapt a reduced internal benchmark using 3-5 public competitions or equivalent tasks'],
                        'tags': ['agents'],
                    }
                ],
                'warnings': [],
            }
        ),
    )

    monkeypatch.setattr(
        main_module,
        'ingest_source_document',
        lambda source_url, submitted_by, settings, store, session_id=None, expected_title=None: main_module.SourceDocumentRecord(
            document_id='doc-ctx-1',
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            status='fetched',
            source_url=source_url,
            submitted_by=submitted_by,
            storage_uri='file:///tmp/source-documents/doc-ctx-1/source.html',
            content_type='text/html',
            size_bytes=128,
            sha256='def456',
            title='MLE-bench: Evaluating Machine Learning Agents on Machine Learning Engineering',
            text_excerpt='This paper evaluates research agents on machine learning engineering tasks using Kaggle-style benchmarks and reports accuracy improvements over a baseline.',
            abstract_excerpt='Machine learning agents are evaluated on Kaggle-style engineering tasks with accuracy-based scoring.',
            method_hints=['benchmark'],
            dataset_hints=['kaggle'],
            metric_hints=['accuracy'],
            validation_status='matched',
            validation_notes=['matched title terms: benchmark'],
            session_id=session_id,
        ),
    )

    queue = client.post(
        '/paper-intake-queues/from-research-problem',
        json={
            'problem_statement': 'Find literature about research agents doing machine learning engineering work.',
            'max_candidate_papers': 1,
        },
    )
    assert queue.status_code == 201
    queue_id = queue.json()['queue_id']

    staged_intake = client.post(f'/paper-intake-queues/{queue_id}/stage-next-intake')
    assert staged_intake.status_code == 201

    interpretation = client.post('/interpretations/from-latest-intake')
    assert interpretation.status_code == 201
    payload = interpretation.json()
    assert 'Used stored source-document context.' in payload['extracted_method_summary']
    assert payload['literature_state_summary'].startswith('Current bounded literature view:')
    joined_claims = ' '.join(payload['extracted_claims']).lower()
    assert 'machine learning engineering' in joined_claims
    assert 'kaggle' in payload['dataset_hints']
    assert 'accuracy' in payload['evaluation_targets']
    assert 'kaggle-style engineering tasks' in payload['literature_state_summary'].lower()
    assert payload['preferred_workflow_id'] == 'literature-to-experiment'
    assert payload['preferred_resource_profile'] == 'cpu-medium'
    assert payload['gpu_required'] is False
    joined_gaps = ' '.join(payload['research_gaps']).lower()
    assert 'concrete dataset' not in joined_gaps
    assert payload['bounded_experiment_ideas']


def test_create_interpretation_requires_intake() -> None:
    client = build_client()

    create_interpretation = client.post('/interpretations/from-latest-intake')
    assert create_interpretation.status_code == 404
    assert create_interpretation.json()['detail'] == 'intake not found'


def test_create_and_fetch_replicability_assessment_from_latest_interpretation() -> None:
    client = build_client()

    create_intake = client.post(
        '/intakes',
        json={
            'raw_request': 'Read this paper intake and determine whether the approved Titanic benchmark path is a good fit.',
            'source_refs': ['https://example.org/titanic-paper'],
            'notes': [
                'The paper compares a baseline on Titanic.',
                'Focus on the reported metrics and dataset assumptions.',
            ],
        },
    )
    assert create_intake.status_code == 201

    create_interpretation = client.post('/interpretations/from-latest-intake')
    assert create_interpretation.status_code == 201
    interpretation_id = create_interpretation.json()['interpretation_id']

    create_assessment = client.post('/replicability-assessments/from-latest-interpretation')
    assert create_assessment.status_code == 201
    payload = create_assessment.json()
    assessment_id = payload['assessment_id']
    assert payload['interpretation_id'] == interpretation_id
    assert payload['recommendation'] == 'proceed'
    assert payload['recommended_workflow_id'] == 'generic-tabular-benchmark'
    assert payload['status'] == 'ready_for_design'
    assert payload['assessment_notes']

    latest = client.get('/replicability-assessments/latest')
    assert latest.status_code == 200
    assert latest.json()['assessment_id'] == assessment_id

    fetched = client.get(f'/replicability-assessments/{assessment_id}')
    assert fetched.status_code == 200
    assert fetched.json()['approval_tier'] == 'tier-2-approved-execution'


def test_create_replicability_assessment_uses_agent_when_enabled(monkeypatch) -> None:
    settings = Settings(
        registry_dir=str(REPO_ROOT / 'services' / 'workflow-registry' / 'definitions'),
        assessment_agent_enabled=True,
    )
    registry = WorkflowRegistry(settings.registry_dir)
    store = InMemoryRunStore()
    client = TestClient(create_app(settings=settings, registry=registry, store=store))

    create_intake = client.post(
        '/intakes',
        json={
            'raw_request': 'Read this paper intake and determine whether the approved Titanic benchmark path is a good fit.',
            'source_refs': ['https://example.org/titanic-paper'],
            'notes': ['The paper compares a baseline on Titanic.'],
        },
    )
    assert create_intake.status_code == 201
    create_interpretation = client.post('/interpretations/from-latest-intake')
    assert create_interpretation.status_code == 201

    def fake_call_assessment_agent(interpretation, settings, registry):
        return main_module.build_replicability_assessment_from_agent_draft(
            interpretation,
            {
                'status': 'ready_for_design',
                'recommendation': 'proceed',
                'recommended_workflow_id': 'generic-tabular-benchmark',
                'candidate_workflow_families': ['generic-tabular-benchmark'],
                'unresolved_fields': [],
                'blocking_reasons': [],
                'approval_tier': 'tier-2-approved-execution',
                'assessment_notes': ['Agent assessment note'],
            },
        )

    monkeypatch.setattr(main_module, 'call_assessment_agent', fake_call_assessment_agent)

    create_assessment = client.post('/replicability-assessments/from-latest-interpretation')
    assert create_assessment.status_code == 201
    payload = create_assessment.json()
    assert payload['status'] == 'ready_for_design'
    assert payload['recommendation'] == 'proceed'
    assert payload['assessment_notes'] == ['Agent assessment note']


def test_create_replicability_assessment_falls_back_when_agent_returns_none(monkeypatch) -> None:
    settings = Settings(
        registry_dir=str(REPO_ROOT / 'services' / 'workflow-registry' / 'definitions'),
        assessment_agent_enabled=True,
    )
    registry = WorkflowRegistry(settings.registry_dir)
    store = InMemoryRunStore()
    client = TestClient(create_app(settings=settings, registry=registry, store=store))

    create_intake = client.post(
        '/intakes',
        json={
            'raw_request': 'Read this paper intake and determine whether the approved Titanic benchmark path is a good fit.',
            'source_refs': ['https://example.org/titanic-paper'],
            'notes': ['The paper compares a baseline on Titanic.'],
        },
    )
    assert create_intake.status_code == 201
    create_interpretation = client.post('/interpretations/from-latest-intake')
    assert create_interpretation.status_code == 201

    monkeypatch.setattr(main_module, 'call_assessment_agent', lambda interpretation, settings, registry: None)

    create_assessment = client.post('/replicability-assessments/from-latest-interpretation')
    assert create_assessment.status_code == 201
    payload = create_assessment.json()
    assert payload['recommended_workflow_id'] == 'generic-tabular-benchmark'
    assert payload['status'] == 'ready_for_design'
    assert payload['assessment_notes']


def test_create_replicability_assessment_requires_interpretation() -> None:
    client = build_client()

    create_assessment = client.post('/replicability-assessments/from-latest-interpretation')
    assert create_assessment.status_code == 404
    assert create_assessment.json()['detail'] == 'interpretation not found'


def test_create_and_fetch_design_draft_from_latest_titanic_intake() -> None:
    client = build_client()

    create_intake = client.post(
        '/intakes',
        json={
            'raw_request': 'Benchmark the approved models on the Titanic dataset and validate the baseline results.',
            'notes': ['Use the standard Titanic train/test splits.'],
        },
    )
    assert create_intake.status_code == 201
    intake_id = create_intake.json()['intake_id']
    create_interpretation = client.post('/interpretations/from-latest-intake')
    assert create_interpretation.status_code == 201
    interpretation_payload = create_interpretation.json()

    create_design = client.post('/design-drafts/from-latest-intake')
    assert create_design.status_code == 201
    payload = create_design.json()
    design_id = payload['design_id']
    assert payload['intake_id'] == intake_id
    assert payload['workflow_id'] == 'generic-tabular-benchmark'
    assert payload['status'] == 'ready_for_run'
    assert payload['declared_inputs']['dataset_name'] == 'titanic'
    assert payload['declared_inputs']['validation_strategy'] == 'holdout'
    assert payload['declared_inputs']['validation_split'] == '0.2'
    assert payload['unresolved_inputs'] == []
    joined_notes = ' '.join(payload['design_notes'])
    assert 'Literature state:' in joined_notes
    assert interpretation_payload['literature_state_summary'] in joined_notes
    assert 'Bounded experiment ideas:' in joined_notes

    latest = client.get('/design-drafts/latest')
    assert latest.status_code == 200
    assert latest.json()['design_id'] == design_id

    fetched = client.get(f'/design-drafts/{design_id}')
    assert fetched.status_code == 200
    assert fetched.json()['candidate_models'] == ['logistic_regression', 'random_forest']


def test_create_design_draft_from_latest_intake_uses_agent_when_enabled(monkeypatch) -> None:
    settings = Settings(
        registry_dir=str(REPO_ROOT / 'services' / 'workflow-registry' / 'definitions'),
        design_agent_enabled=True,
    )
    registry = WorkflowRegistry(settings.registry_dir)
    store = InMemoryRunStore()
    client = TestClient(create_app(settings=settings, registry=registry, store=store))

    create_intake = client.post(
        '/intakes',
        json={
            'raw_request': 'Benchmark the approved models on the Titanic dataset and validate the baseline results.',
            'notes': ['Use the standard Titanic train/test splits.'],
        },
    )
    assert create_intake.status_code == 201
    assert client.post('/interpretations/from-latest-intake').status_code == 201

    def fake_call_design_agent(intake, workflow, submitted_by, settings, source_assessment_id=None):
        return main_module.build_design_draft_from_agent_draft(
            intake,
            workflow,
            submitted_by=submitted_by,
            source_assessment_id=source_assessment_id,
            validated_draft={
                'workflow_id': workflow.workflow_id,
                'workflow_family': workflow.workflow_family,
                'objective': 'agent-produced design objective',
                'declared_inputs': {
                    'dataset_name': 'titanic',
                    'train_uri': 's3://datasets/titanic/train.csv',
                    'test_uri': 's3://datasets/titanic/test.csv',
                    'target_column': 'Survived',
                },
                'unresolved_inputs': [],
                'candidate_models': ['random_forest'],
                'resource_profile': workflow.resource_profile.profile_name,
                'expected_artifacts': workflow.expected_artifacts.model_dump(mode='json'),
                'approval_tier': workflow.approval_tier,
                'design_notes': ['Agent design note'],
            },
        )

    monkeypatch.setattr(main_module, 'call_design_agent', fake_call_design_agent)

    create_design = client.post('/design-drafts/from-latest-intake')
    assert create_design.status_code == 201
    payload = create_design.json()
    assert payload['objective'] == 'agent-produced design objective'
    assert payload['candidate_models'] == ['random_forest']
    assert payload['design_notes'] == ['Agent design note']


def test_create_design_draft_from_latest_intake_falls_back_when_agent_returns_none(monkeypatch) -> None:
    settings = Settings(
        registry_dir=str(REPO_ROOT / 'services' / 'workflow-registry' / 'definitions'),
        design_agent_enabled=True,
    )
    registry = WorkflowRegistry(settings.registry_dir)
    store = InMemoryRunStore()
    client = TestClient(create_app(settings=settings, registry=registry, store=store))

    create_intake = client.post(
        '/intakes',
        json={
            'raw_request': 'Benchmark the approved models on the Titanic dataset and validate the baseline results.',
            'notes': ['Use the standard Titanic train/test splits.'],
        },
    )
    assert create_intake.status_code == 201
    assert client.post('/interpretations/from-latest-intake').status_code == 201

    monkeypatch.setattr(main_module, 'call_design_agent', lambda *args, **kwargs: None)

    create_design = client.post('/design-drafts/from-latest-intake')
    assert create_design.status_code == 201
    payload = create_design.json()
    assert payload['declared_inputs']['dataset_name'] == 'titanic'
    assert payload['candidate_models'] == ['logistic_regression', 'random_forest']


def test_create_design_draft_from_latest_assessment() -> None:
    client = build_client()

    create_intake = client.post(
        '/intakes',
        json={
            'raw_request': 'Read this paper intake and determine whether the approved Titanic benchmark path is a good fit.',
            'source_refs': ['https://example.org/titanic-paper'],
            'notes': [
                'The paper compares a baseline on Titanic.',
                'Focus on the reported metrics and dataset assumptions.',
            ],
        },
    )
    assert create_intake.status_code == 201
    intake_id = create_intake.json()['intake_id']

    create_interpretation = client.post('/interpretations/from-latest-intake')
    assert create_interpretation.status_code == 201

    create_assessment = client.post('/replicability-assessments/from-latest-interpretation')
    assert create_assessment.status_code == 201
    assessment_id = create_assessment.json()['assessment_id']

    create_design = client.post('/design-drafts/from-latest-assessment')
    assert create_design.status_code == 201
    payload = create_design.json()
    assert payload['intake_id'] == intake_id
    assert payload['source_assessment_id'] == assessment_id
    assert payload['workflow_id'] == 'generic-tabular-benchmark'
    assert payload['status'] == 'ready_for_run'
    assert 'Literature state:' in ' '.join(payload['design_notes'])


def test_create_design_draft_from_latest_assessment_uses_agent_when_enabled(monkeypatch) -> None:
    settings = Settings(
        registry_dir=str(REPO_ROOT / 'services' / 'workflow-registry' / 'definitions'),
        design_agent_enabled=True,
    )
    registry = WorkflowRegistry(settings.registry_dir)
    store = InMemoryRunStore()
    client = TestClient(create_app(settings=settings, registry=registry, store=store))

    create_intake = client.post(
        '/intakes',
        json={
            'raw_request': 'Read this paper intake and determine whether the approved Titanic benchmark path is a good fit.',
            'source_refs': ['https://example.org/titanic-paper'],
            'notes': ['The paper compares a baseline on Titanic.'],
        },
    )
    assert create_intake.status_code == 201
    assert client.post('/interpretations/from-latest-intake').status_code == 201
    create_assessment = client.post('/replicability-assessments/from-latest-interpretation')
    assert create_assessment.status_code == 201
    assessment_id = create_assessment.json()['assessment_id']

    def fake_call_design_agent(intake, workflow, submitted_by, settings, source_assessment_id=None):
        return main_module.build_design_draft_from_agent_draft(
            intake,
            workflow,
            submitted_by=submitted_by,
            source_assessment_id=source_assessment_id,
            validated_draft={
                'workflow_id': workflow.workflow_id,
                'workflow_family': workflow.workflow_family,
                'objective': 'agent-produced assessment design objective',
                'declared_inputs': {
                    'dataset_name': 'titanic',
                    'train_uri': 's3://datasets/titanic/train.csv',
                    'test_uri': 's3://datasets/titanic/test.csv',
                    'target_column': 'Survived',
                },
                'unresolved_inputs': [],
                'candidate_models': ['logistic_regression'],
                'resource_profile': workflow.resource_profile.profile_name,
                'expected_artifacts': workflow.expected_artifacts.model_dump(mode='json'),
                'approval_tier': workflow.approval_tier,
                'design_notes': ['Agent assessment-linked design note'],
            },
        )

    monkeypatch.setattr(main_module, 'call_design_agent', fake_call_design_agent)

    create_design = client.post('/design-drafts/from-latest-assessment')
    assert create_design.status_code == 201
    payload = create_design.json()
    assert payload['source_assessment_id'] == assessment_id
    assert payload['objective'] == 'agent-produced assessment design objective'
    assert payload['candidate_models'] == ['logistic_regression']


def test_create_design_draft_from_latest_assessment_requires_assessment() -> None:
    client = build_client()

    create_design = client.post('/design-drafts/from-latest-assessment')
    assert create_design.status_code == 404
    assert create_design.json()['detail'] == 'replicability assessment not found'


def test_review_latest_design_draft_resolves_literature_inputs() -> None:
    client = build_client()

    intake = client.post(
        '/intakes',
        json={
            'raw_request': 'Turn this paper into a bounded experiment design based on the linked notes.',
            'source_refs': ['https://example.org/paper-notes'],
            'notes': ['Focus on the method section and reported metrics.'],
        },
    )
    assert intake.status_code == 201

    design = client.post('/design-drafts/from-latest-intake')
    assert design.status_code == 201
    payload = design.json()
    assert payload['workflow_id'] == 'literature-to-experiment'
    assert payload['status'] == 'needs_review'
    assert payload['unresolved_inputs'] == ['dataset_uri']

    reviewed = client.post(
        '/design-drafts/latest/review',
        json={
            'resolved_inputs': {'dataset_uri': 's3://datasets/paper-derived/train.csv'},
            'review_notes': ['Dataset location was approved during backend review.'],
        },
    )
    assert reviewed.status_code == 200
    reviewed_payload = reviewed.json()
    assert reviewed_payload['status'] == 'ready_for_run'
    assert reviewed_payload['declared_inputs']['dataset_uri'] == 's3://datasets/paper-derived/train.csv'
    assert reviewed_payload['unresolved_inputs'] == []
    assert 'Dataset location was approved during backend review.' in reviewed_payload['design_notes']


def test_review_existing_replication_design_stays_blocked_by_approval_tier() -> None:
    client = build_client()

    intake = client.post(
        '/intakes',
        json={
            'raw_request': 'Replicate this published result from the linked repository and reported evaluation target.',
            'source_refs': ['https://example.org/paper', 'https://github.com/example/repo'],
            'notes': ['Re-run the published baseline exactly once with the approved environment.'],
        },
    )
    assert intake.status_code == 201

    design = client.post('/design-drafts/from-latest-intake')
    assert design.status_code == 201
    payload = design.json()
    assert payload['workflow_id'] == 'replication-lite'
    assert payload['status'] == 'needs_review'

    reviewed = client.post(
        f"/design-drafts/{payload['design_id']}/review",
        json={
            'resolved_inputs': {
                'repository_url': 'https://github.com/example/repo',
                'dataset_uri': 's3://datasets/replication-lite/input.csv',
                'evaluation_target': 'accuracy',
            },
            'review_notes': ['Resolved execution inputs, but keep approval-tier gate in place.'],
        },
    )
    assert reviewed.status_code == 200
    reviewed_payload = reviewed.json()
    assert reviewed_payload['status'] == 'needs_review'
    assert reviewed_payload['unresolved_inputs'] == []
    assert any('Approval tier tier-3-human-approval' in note for note in reviewed_payload['design_notes'])


def test_create_design_draft_prefers_benchmark_for_titanic_paper_intake() -> None:
    client = build_client()

    create_intake = client.post(
        '/intakes',
        json={
            'raw_request': 'Start a bounded paper-to-validation intake for the approved Titanic benchmark path.',
            'source_refs': ['https://www.kaggle.com/competitions/titanic'],
            'source_type': 'paper-link',
            'notes': [
                'Use the approved Titanic dataset path for the first intake-to-design validation.',
                'Keep the workflow family inside the approved Glasslab registry.',
            ],
        },
    )
    assert create_intake.status_code == 201

    create_design = client.post('/design-drafts/from-latest-intake')
    assert create_design.status_code == 201
    payload = create_design.json()
    assert payload['workflow_id'] == 'generic-tabular-benchmark'
    assert payload['status'] == 'ready_for_run'
    assert payload['unresolved_inputs'] == []


def test_create_design_draft_requires_intake() -> None:
    client = build_client()

    create_design = client.post('/design-drafts/from-latest-intake')
    assert create_design.status_code == 404
    assert create_design.json()['detail'] == 'intake not found'


def test_digest_schedule_lifecycle() -> None:
    client = build_client()

    created = client.post(
        '/digest-schedules',
        json={
            'cron_expr': '0 6 * * *',
            'digest_kind': 'daily-run-summary',
            'scope_filter': {'workflow_id': 'generic-tabular-benchmark'},
        },
    )
    assert created.status_code == 201
    payload = created.json()
    schedule_id = payload['schedule_id']
    assert payload['operation_type'] == 'digest'
    assert payload['approval_tier'] == 'tier-1-read-only'
    assert payload['status'] == 'active'

    listed = client.get('/digest-schedules')
    assert listed.status_code == 200
    assert [item['schedule_id'] for item in listed.json()] == [schedule_id]

    disabled = client.post(f'/digest-schedules/{schedule_id}/disable')
    assert disabled.status_code == 200
    assert disabled.json()['status'] == 'disabled'


def test_run_due_digest_schedules_creates_execution_record() -> None:
    client = build_client()

    create_run = client.post(
        '/runs',
        json={
            'workflow_id': 'generic-tabular-benchmark',
            'objective': 'Create a run for digest schedule execution.',
            'inputs': {
                'dataset_name': 'titanic',
                'train_uri': 's3://datasets/titanic/train.csv',
                'test_uri': 's3://datasets/titanic/test.csv',
                'target_column': 'Survived',
            },
            'models': ['logistic_regression'],
            'resource_profile': 'cpu-small',
        },
    )
    assert create_run.status_code == 201

    now = datetime.now(timezone.utc)
    cron_expr = f'{now.minute} {now.hour} {now.day} {now.month} {(now.weekday() + 1) % 7}'
    created = client.post(
        '/digest-schedules',
        json={
            'cron_expr': cron_expr,
            'digest_kind': 'daily-run-summary',
            'scope_filter': {'workflow_id': 'generic-tabular-benchmark'},
        },
    )
    assert created.status_code == 201
    schedule_id = created.json()['schedule_id']

    executed = client.post('/digest-schedules/run-due')
    assert executed.status_code == 200
    payload = executed.json()
    assert len(payload) == 1
    assert payload[0]['schedule_id'] == schedule_id
    assert payload[0]['result_status'] == 'ok'
    assert payload[0]['digest_payload']['matching_run_count'] == 1
    assert payload[0]['digest_payload']['workflow_ids'] == ['generic-tabular-benchmark']

    listed = client.get('/scheduled-executions')
    assert listed.status_code == 200
    executions = listed.json()
    assert len(executions) == 1
    assert executions[0]['schedule_id'] == schedule_id


def test_create_approved_rerun_schedule_from_latest_run() -> None:
    client = build_client()

    create_run = client.post(
        '/runs',
        json={
            'workflow_id': 'generic-tabular-benchmark',
            'objective': 'Create a reviewed benchmark run suitable for scheduled reruns.',
            'inputs': {
                'dataset_name': 'titanic',
                'train_uri': 's3://datasets/titanic/train.csv',
                'test_uri': 's3://datasets/titanic/test.csv',
                'target_column': 'Survived',
            },
            'models': ['logistic_regression', 'random_forest'],
            'resource_profile': 'cpu-small',
        },
    )
    assert create_run.status_code == 201
    run_id = create_run.json()['run_id']

    store = client.app.state.store
    record = store.get_run(run_id)
    succeeded_record = record.model_copy(update={'status': record.status.model_copy(update={'status': 'succeeded'})})
    store.save_run(succeeded_record)

    created = client.post(
        '/approved-rerun-schedules/from-latest-run',
        json={'cron_expr': '30 2 * * 1'},
    )
    assert created.status_code == 201
    payload = created.json()
    schedule_id = payload['schedule_id']
    assert payload['operation_type'] == 'approved-rerun'
    assert payload['workflow_id'] == 'generic-tabular-benchmark'
    assert payload['source_run_id'] == run_id
    assert payload['allowed_dataset_uri'] == 's3://datasets/titanic/train.csv'
    assert payload['allowed_model_ids'] == ['logistic_regression', 'random_forest']

    listed = client.get('/approved-rerun-schedules')
    assert listed.status_code == 200
    assert [item['schedule_id'] for item in listed.json()] == [schedule_id]

    disabled = client.post(f'/approved-rerun-schedules/{schedule_id}/disable')
    assert disabled.status_code == 200
    assert disabled.json()['status'] == 'disabled'


def test_run_due_approved_rerun_schedules_creates_autonomous_run() -> None:
    client = build_client()

    create_run = client.post(
        '/runs',
        json={
            'workflow_id': 'generic-tabular-benchmark',
            'objective': 'Create a reviewed benchmark run suitable for scheduled reruns.',
            'inputs': {
                'dataset_name': 'titanic',
                'train_uri': 's3://datasets/titanic/train.csv',
                'test_uri': 's3://datasets/titanic/test.csv',
                'target_column': 'Survived',
            },
            'models': ['logistic_regression', 'random_forest'],
            'resource_profile': 'cpu-small',
        },
    )
    assert create_run.status_code == 201
    source_run_id = create_run.json()['run_id']

    store = client.app.state.store
    record = store.get_run(source_run_id)
    succeeded_record = record.model_copy(update={'status': record.status.model_copy(update={'status': 'succeeded'})})
    store.save_run(succeeded_record)

    now = datetime.now(timezone.utc)
    cron_expr = f'{now.minute} {now.hour} {now.day} {now.month} {(now.weekday() + 1) % 7}'
    created = client.post(
        '/approved-rerun-schedules/from-latest-run',
        json={'cron_expr': cron_expr},
    )
    assert created.status_code == 201
    schedule_id = created.json()['schedule_id']

    executed = client.post('/approved-rerun-schedules/run-due')
    assert executed.status_code == 200
    payload = executed.json()
    assert len(payload) == 1
    assert payload[0]['schedule_id'] == schedule_id
    assert payload[0]['result_status'] == 'ok'
    produced_run_id = payload[0]['produced_run_ids'][0]

    rerun_record = store.get_run(produced_run_id)
    assert rerun_record is not None
    assert rerun_record.run_purpose == 'approved-rerun'
    assert rerun_record.run_priority == 'autonomous'
    assert rerun_record.manifest.run_priority == 'autonomous'


def test_run_due_approved_rerun_schedules_resolves_source_status_from_disk(tmp_path) -> None:
    settings = Settings(
        registry_dir=str(REPO_ROOT / 'services' / 'workflow-registry' / 'definitions'),
        artifacts_mount_path=str(tmp_path),
    )
    registry = WorkflowRegistry(settings.registry_dir)
    store = InMemoryRunStore()
    client = TestClient(create_app(settings=settings, registry=registry, store=store))

    create_run = client.post(
        '/runs',
        json={
            'workflow_id': 'generic-tabular-benchmark',
            'objective': 'Create a reviewed benchmark run suitable for scheduled reruns.',
            'inputs': {
                'dataset_name': 'titanic',
                'train_uri': 's3://datasets/titanic/train.csv',
                'test_uri': 's3://datasets/titanic/test.csv',
                'target_column': 'Survived',
            },
            'models': ['logistic_regression', 'random_forest'],
            'resource_profile': 'cpu-small',
        },
    )
    assert create_run.status_code == 201
    source_run_id = create_run.json()['run_id']

    run_dir = tmp_path / source_run_id
    run_dir.mkdir(parents=True)
    (run_dir / 'status.json').write_text(
        '{"run_id":"%s","status":"succeeded","updated_at":"2026-03-25T16:37:32Z","detail":"done"}'
        % source_run_id
    )

    now = datetime.now(timezone.utc)
    cron_expr = f'{now.minute} {now.hour} {now.day} {now.month} {(now.weekday() + 1) % 7}'
    created = client.post(
        '/approved-rerun-schedules/from-latest-run',
        json={'cron_expr': cron_expr},
    )
    assert created.status_code == 201
    schedule_id = created.json()['schedule_id']

    executed = client.post('/approved-rerun-schedules/run-due')
    assert executed.status_code == 200
    payload = executed.json()
    assert len(payload) == 1
    assert payload[0]['schedule_id'] == schedule_id
    assert payload[0]['result_status'] == 'ok'
    produced_run_id = payload[0]['produced_run_ids'][0]

    rerun_record = store.get_run(produced_run_id)
    assert rerun_record is not None
    assert rerun_record.run_purpose == 'approved-rerun'
    assert rerun_record.run_priority == 'autonomous'


def test_approved_rerun_schedule_requires_succeeded_tier_two_run() -> None:
    client = build_client()

    created = client.post(
        '/runs',
        json={
            'workflow_id': 'literature-to-experiment',
            'objective': 'Create a run and then force it into a failed state for schedule rejection testing.',
            'inputs': {
                'paper_id': 'https://example.org/paper-notes',
                'source_notes': 'Focus on the reviewed method and evaluation section.',
                'dataset_uri': 's3://datasets/paper-derived/train.csv',
            },
            'models': ['deterministic-template'],
            'resource_profile': 'cpu-medium',
        },
    )
    assert created.status_code == 201
    run_id = created.json()['run_id']

    store = client.app.state.store
    record = store.get_run(run_id)
    failed_record = record.model_copy(update={'status': record.status.model_copy(update={'status': 'failed'})})
    store.save_run(failed_record)

    rerun = client.post(
        '/approved-rerun-schedules/from-latest-run',
        json={'cron_expr': '15 4 * * *'},
    )
    assert rerun.status_code == 409
    assert rerun.json()['detail'] == 'current run must have status succeeded before creating an approved rerun schedule'


def test_create_run_from_latest_ready_design_draft() -> None:
    client = build_client()

    intake = client.post(
        '/intakes',
        json={
            'raw_request': 'Benchmark the approved models on Titanic and create a validation run.',
            'notes': ['Use the standard Titanic train/test splits.'],
        },
    )
    assert intake.status_code == 201

    design = client.post('/design-drafts/from-latest-intake')
    assert design.status_code == 201
    design_payload = design.json()

    run = client.post('/runs/from-latest-design-draft')
    assert run.status_code == 201
    payload = run.json()
    assert payload['source_design_id'] == design_payload['design_id']
    assert payload['source_intake_id'] == design_payload['intake_id']
    assert payload['run_purpose'] == 'validation'
    assert payload['manifest']['inputs']['dataset_name'] == 'titanic'


def test_apply_latest_session_design_skill_is_not_shadowed_by_session_route() -> None:
    client = build_client()

    session = client.post(
        '/research-sessions',
        json={
            'goal_statement': 'Benchmark approved Titanic models and create a validation run.',
            'priorities': ['titanic', 'benchmark'],
        },
    )
    assert session.status_code == 201

    intake = client.post(
        '/research-sessions/latest/intakes',
        json={
            'raw_request': 'Benchmark the approved models on Titanic and create a validation run.',
            'notes': ['Use the standard Titanic train/test splits.'],
        },
    )
    assert intake.status_code == 201

    design = client.post('/research-sessions/latest/skills/design')
    assert design.status_code == 201
    payload = design.json()
    assert payload['workflow_id'] == 'generic-tabular-benchmark'
    assert payload['intake_id'] == intake.json()['intake_id']


def test_get_latest_session_execution_preflight_uses_latest_session_design(monkeypatch) -> None:
    client = build_client()

    class FakeResponse:
        def __init__(self, payload: dict) -> None:
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            import json
            return json.dumps(self.payload).encode('utf-8')

    monkeypatch.setattr(
        main_module.urllib_request,
        'urlopen',
        lambda request_obj, timeout: FakeResponse(
            {
                'request_id': 'session-exec-queue-1',
                'selected_tracks': [{'track_id': 'agent_evaluation', 'track': 'agent_evaluation'}],
                'selected_queries': [{'queries': ['bounded literature harvest']}],
                'selected_papers': [
                    {
                        'paper_id': 'mle_bench_arxiv_2024',
                        'title': 'MLE-bench: Evaluating Machine Learning Agents on Machine Learning Engineering',
                        'year': 2024,
                        'venue': 'arXiv',
                        'priority': 'P1',
                        'tracks': ['agent_evaluation'],
                        'bounded_job_fit': 4,
                        'replication_complexity': 4,
                        'official_page': 'https://arxiv.org/abs/2410.07095',
                        'pdf_url': None,
                        'why_seed': 'benchmark for bounded ML engineering work',
                        'first_jobs': ['reduce the benchmark into a smaller internal harness'],
                        'tags': ['agents'],
                    }
                ],
                'warnings': [],
            }
        ),
    )
    monkeypatch.setattr(
        main_module,
        'ingest_source_document',
        lambda source_url, submitted_by, settings, store, session_id=None, expected_title=None: main_module.SourceDocumentRecord(
            document_id='session-exec-doc-1',
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            status='fetched',
            source_url=source_url,
            submitted_by=submitted_by,
            storage_uri='file:///tmp/source-documents/session-exec-doc-1/source.html',
            content_type='text/html',
            size_bytes=42,
            sha256='mno345',
            title='source.html',
            text_excerpt='Session execution route document excerpt.',
            session_id=session_id,
        ),
    )

    session = client.post(
        '/research-sessions',
        json={
            'goal_statement': 'Benchmark the approved models on Titanic and create a validation run.',
        },
    )
    assert session.status_code == 201
    session_id = session.json()['session_id']

    problem = client.post(f'/research-sessions/{session_id}/skills/research-problem')
    assert problem.status_code == 201
    queue = client.post(f'/research-sessions/{session_id}/skills/literature-harvest')
    assert queue.status_code == 201
    intake = client.post(f'/research-sessions/{session_id}/skills/paper-intake')
    assert intake.status_code == 201
    interpretation = client.post(f'/research-sessions/{session_id}/skills/interpretation')
    assert interpretation.status_code == 201
    assessment = client.post(f'/research-sessions/{session_id}/skills/assessment')
    assert assessment.status_code == 201
    design = client.post(f'/research-sessions/{session_id}/skills/design')
    assert design.status_code == 201

    response = client.get('/research-sessions/latest/execution-preflight')
    assert response.status_code == 200
    payload = response.json()
    assert payload['workflow_id'] == design.json()['workflow_id']
    assert payload['resource_profile'] == design.json()['resource_profile']
    assert payload['ready'] is True


def test_get_latest_session_execution_preflight_requires_session_design() -> None:
    client = build_client()

    response = client.get('/research-sessions/latest/execution-preflight')
    assert response.status_code == 404
    assert response.json()['detail'] == 'no research session has been created yet'


def test_create_run_from_latest_session_design(monkeypatch) -> None:
    client = build_client()

    class FakeResponse:
        def __init__(self, payload: dict) -> None:
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            import json
            return json.dumps(self.payload).encode('utf-8')

    monkeypatch.setattr(
        main_module.urllib_request,
        'urlopen',
        lambda request_obj, timeout: FakeResponse(
            {
                'request_id': 'session-exec-queue-2',
                'selected_tracks': [{'track_id': 'agent_evaluation', 'track': 'agent_evaluation'}],
                'selected_queries': [{'queries': ['bounded literature harvest']}],
                'selected_papers': [
                    {
                        'paper_id': 'mle_bench_arxiv_2024',
                        'title': 'MLE-bench: Evaluating Machine Learning Agents on Machine Learning Engineering',
                        'year': 2024,
                        'venue': 'arXiv',
                        'priority': 'P1',
                        'tracks': ['agent_evaluation'],
                        'bounded_job_fit': 4,
                        'replication_complexity': 4,
                        'official_page': 'https://arxiv.org/abs/2410.07095',
                        'pdf_url': None,
                        'why_seed': 'benchmark for bounded ML engineering work',
                        'first_jobs': ['reduce the benchmark into a smaller internal harness'],
                        'tags': ['agents'],
                    }
                ],
                'warnings': [],
            }
        ),
    )
    monkeypatch.setattr(
        main_module,
        'ingest_source_document',
        lambda source_url, submitted_by, settings, store, session_id=None, expected_title=None: main_module.SourceDocumentRecord(
            document_id='session-exec-doc-2',
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            status='fetched',
            source_url=source_url,
            submitted_by=submitted_by,
            storage_uri='file:///tmp/source-documents/session-exec-doc-2/source.html',
            content_type='text/html',
            size_bytes=42,
            sha256='pqr678',
            title='source.html',
            text_excerpt='Session execution route document excerpt.',
            session_id=session_id,
        ),
    )

    session = client.post(
        '/research-sessions',
        json={
            'goal_statement': 'Benchmark the approved models on Titanic and create a validation run.',
        },
    )
    assert session.status_code == 201
    session_id = session.json()['session_id']

    problem = client.post(f'/research-sessions/{session_id}/skills/research-problem')
    assert problem.status_code == 201
    queue = client.post(f'/research-sessions/{session_id}/skills/literature-harvest')
    assert queue.status_code == 201
    intake = client.post(f'/research-sessions/{session_id}/skills/paper-intake')
    assert intake.status_code == 201
    interpretation = client.post(f'/research-sessions/{session_id}/skills/interpretation')
    assert interpretation.status_code == 201
    assessment = client.post(f'/research-sessions/{session_id}/skills/assessment')
    assert assessment.status_code == 201
    design = client.post(f'/research-sessions/{session_id}/skills/design')
    assert design.status_code == 201
    design_payload = design.json()

    run = client.post('/research-sessions/latest/runs/from-design')
    assert run.status_code == 201
    payload = run.json()
    assert payload['source_design_id'] == design_payload['design_id']
    assert payload['source_intake_id'] == design_payload['intake_id']
    assert payload['session_id'] == design_payload['session_id']
    assert payload['run_purpose'] == 'validation'
    assert payload['manifest']['inputs']['dataset_name'] == 'titanic'


def test_transition_run_happy_path_creates_design_and_run(monkeypatch) -> None:
    client = build_client()

    class FakeResponse:
        def __init__(self, payload: dict) -> None:
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            import json
            return json.dumps(self.payload).encode('utf-8')

    monkeypatch.setattr(
        main_module.urllib_request,
        'urlopen',
        lambda request_obj, timeout: FakeResponse(
            {
                'request_id': 'session-run-happy-path-1',
                'selected_tracks': [{'track_id': 'agent_evaluation', 'track': 'agent_evaluation'}],
                'selected_queries': [{'queries': ['bounded literature harvest']}],
                'selected_papers': [
                    {
                        'paper_id': 'mle_bench_arxiv_2024',
                        'title': 'MLE-bench: Evaluating Machine Learning Agents on Machine Learning Engineering',
                        'year': 2024,
                        'venue': 'arXiv',
                        'priority': 'P1',
                        'tracks': ['agent_evaluation'],
                        'bounded_job_fit': 4,
                        'replication_complexity': 4,
                        'official_page': 'https://arxiv.org/abs/2410.07095',
                        'pdf_url': None,
                        'why_seed': 'benchmark for bounded ML engineering work',
                        'first_jobs': ['reduce the benchmark into a smaller internal harness'],
                        'tags': ['agents'],
                    }
                ],
                'warnings': [],
            }
        ),
    )
    monkeypatch.setattr(
        main_module,
        'ingest_source_document',
        lambda source_url, submitted_by, settings, store, session_id=None, expected_title=None: main_module.SourceDocumentRecord(
            document_id='session-run-happy-doc',
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            status='fetched',
            source_url=source_url,
            submitted_by=submitted_by,
            storage_uri='file:///tmp/source-documents/session-run-happy-doc/source.html',
            content_type='text/html',
            size_bytes=42,
            sha256='runhappy123',
            title='source.html',
            text_excerpt='Session execution route document excerpt.',
            session_id=session_id,
        ),
    )

    session = client.post(
        '/research-sessions',
        json={'goal_statement': 'Benchmark the approved models on Titanic and create a validation run.'},
    )
    assert session.status_code == 201
    session_id = session.json()['session_id']

    problem = client.post(f'/research-sessions/{session_id}/skills/research-problem')
    assert problem.status_code == 201
    queue = client.post(f'/research-sessions/{session_id}/skills/literature-harvest')
    assert queue.status_code == 201
    intake = client.post(f'/research-sessions/{session_id}/skills/paper-intake')
    assert intake.status_code == 201

    run = client.post(f'/research-sessions/{session_id}/transitions/run-happy-path')
    assert run.status_code == 201
    payload = run.json()
    assert payload['session']['session_id'] == session_id
    assert payload['design']['session_id'] == session_id
    assert payload['run']['session_id'] == session_id
    assert payload['run']['source_design_id'] == payload['design']['design_id']
    assert payload['run']['source_intake_id'] == payload['design']['intake_id']
    assert payload['run']['run_purpose'] == 'validation'


def test_create_run_from_latest_design_draft_blocks_non_ready_design() -> None:
    client = build_client()

    intake = client.post(
        '/intakes',
        json={
            'raw_request': 'Turn this paper into a bounded experiment design based on the linked notes.',
            'source_refs': ['https://example.org/paper-notes'],
            'notes': ['Focus on the method section and reported metrics.'],
        },
    )
    assert intake.status_code == 201

    design = client.post('/design-drafts/from-latest-intake')
    assert design.status_code == 201
    assert design.json()['status'] == 'needs_review'

    run = client.post('/runs/from-latest-design-draft')
    assert run.status_code == 409
    assert run.json()['detail'] == 'design method_spec is not ready_for_run: dataset_uri is unresolved'


def test_create_run_from_reviewed_literature_design_draft() -> None:
    client = build_client()

    intake = client.post(
        '/intakes',
        json={
            'raw_request': 'Turn this paper into a bounded experiment design based on the linked notes.',
            'source_refs': ['https://example.org/paper-notes'],
            'notes': ['Focus on the method section and reported metrics.'],
        },
    )
    assert intake.status_code == 201

    design = client.post('/design-drafts/from-latest-intake')
    assert design.status_code == 201
    assert design.json()['status'] == 'needs_review'

    review = client.post(
        '/design-drafts/latest/review',
        json={
            'resolved_inputs': {'dataset_uri': 's3://datasets/paper-derived/train.csv'},
            'review_notes': ['Dataset location was approved during backend review.'],
        },
    )
    assert review.status_code == 200
    assert review.json()['status'] == 'ready_for_run'
    assert review.json()['workflow_id'] == 'literature-to-experiment'

    run = client.post('/runs/from-latest-design-draft')
    assert run.status_code == 201
    payload = run.json()
    assert payload['workflow_id'] == 'literature-to-experiment'
    assert payload['source_design_id'] == review.json()['design_id']
    assert payload['manifest']['inputs']['dataset_uri'] == 's3://datasets/paper-derived/train.csv'


def test_interpretation_emits_bounded_method_spec() -> None:
    client = build_client()

    intake = client.post(
        '/intakes',
        json={
            'raw_request': 'Vision-transformer image forgery detection with PyTorch and timm on s3://datasets/forgery/train.csv.',
            'source_refs': ['https://example.org/forgery-paper'],
            'notes': ['Prefer GPU execution and explicit package checks.'],
        },
    )
    assert intake.status_code == 201

    interpretation = client.post('/interpretations/from-latest-intake')
    assert interpretation.status_code == 201
    payload = interpretation.json()
    assert payload['method_spec']['workflow_id'] == 'gpu-experiment'
    assert payload['method_spec']['execution_inputs']['dataset_uri'] == 's3://datasets/forgery/train.csv'
    assert payload['method_spec']['run_readiness'] == 'ready'
    assert 'torch' in payload['technique_knowledge']['python_packages']


def test_design_from_interpretation_can_be_ready_for_run() -> None:
    client = build_client()

    intake = client.post(
        '/intakes',
        json={
            'raw_request': 'Turn this paper into a bounded experiment design based on the linked notes and s3://datasets/paper-derived/train.csv.',
            'source_refs': ['https://example.org/paper-notes'],
            'notes': ['Focus on the method section and reported metrics.'],
        },
    )
    assert intake.status_code == 201

    interpretation = client.post('/interpretations/from-latest-intake')
    assert interpretation.status_code == 201

    design = client.post('/design-drafts/from-latest-intake')
    assert design.status_code == 201
    design_payload = design.json()
    reviewed = client.post(
        f"/design-drafts/{design_payload['design_id']}/review",
        json={
            'resolved_inputs': {
                'dataset_uri': 's3://datasets/dreamsim/train.csv',
                'model_family': 'lightweight replication',
                'training_notes': 'Replicate DreamSim visual similarity metric with PyTorch and timm.',
                'evaluation_target': 'embedding retrieval auc',
                'validation_strategy': 'stratified_holdout',
                'validation_split': '0.2',
            },
            'review_notes': ['Resolved GPU execution inputs for bounded autoresearch decision test.'],
        },
    )
    assert reviewed.status_code == 200
    design_payload = reviewed.json()
    assert design_payload['status'] == 'ready_for_run'
    assert design_payload['method_spec']['run_readiness'] == 'ready'
    assert design_payload['workflow_id'] == 'generic-tabular-benchmark'
    assert design_payload['declared_inputs']['train_uri'] == 's3://datasets/paper-derived/train.csv'

    run = client.post('/runs/from-latest-design-draft')
    assert run.status_code == 201
    assert run.json()['manifest']['inputs']['train_uri'] == 's3://datasets/paper-derived/train.csv'


def test_gpu_technique_card_design_can_launch_run() -> None:
    client = build_client()

    imported = client.post(
        '/technique-catalog/import',
        json={
            'cards': [
                {
                    'name': 'DreamSim Transformer Similarity',
                    'aliases': ['dreamsim', 'visual similarity metric'],
                    'algorithm_family': 'transformers',
                    'specific_algorithms': ['vision_transformer'],
                    'loss_functions': ['contrastive_loss'],
                    'validation_strategies': ['stratified_holdout'],
                    'primary_metrics': ['roc_auc'],
                    'python_packages': ['torch', 'timm'],
                    'gpu_required': True,
                    'resource_profile': 'gpu-small',
                    'workflow_ids': ['gpu-experiment'],
                    'default_dataset_uri': 's3://datasets/dreamsim/train.csv',
                    'default_evaluation_target': 'embedding retrieval auc',
                }
            ]
        },
    )
    assert imported.status_code == 201

    session = client.post(
        '/research-sessions',
        json={'goal_statement': 'Replicate DreamSim visual similarity metric with PyTorch and timm.'},
    )
    assert session.status_code == 201
    session_id = session.json()['session_id']

    design = client.post(f'/research-sessions/{session_id}/skills/design')
    assert design.status_code == 201
    payload = design.json()
    assert payload['workflow_id'] == 'gpu-experiment'
    assert payload['status'] == 'ready_for_run'
    assert payload['declared_inputs']['validation_split'] == '0.2'

    run = client.post(f'/research-sessions/{session_id}/runs/from-design')
    assert run.status_code == 201
    run_payload = run.json()
    assert run_payload['workflow_id'] == 'gpu-experiment'
    assert run_payload['manifest']['inputs']['dataset_uri'] == 's3://datasets/dreamsim/train.csv'
    assert run_payload['manifest']['inputs']['evaluation_target'] == 'embedding retrieval auc'
    assert run_payload['manifest']['inputs']['validation_strategy'] == 'stratified_holdout'
    assert run_payload['manifest']['inputs']['validation_split'] == '0.2'


def test_create_fresh_paper_pipeline_creates_literature_run_without_manual_review() -> None:
    client = build_client()

    response = client.post(
        '/paper-pipelines/fresh-paper',
        json={
            'paper_ref': 'https://example.org/papers/bounded-method.pdf',
            'raw_request': 'Ingest this paper and derive a bounded literature experiment from the linked method notes.',
            'notes': ['The paper discusses a bounded validation path for a literature-derived experiment.'],
            'dataset_uri': 's3://datasets/paper-derived/train.csv',
            'wait_for_terminal_state': False,
        },
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload['assessment']['recommended_workflow_id'] == 'literature-to-experiment'
    assert payload['design']['status'] == 'ready_for_run'
    assert payload['design']['declared_inputs']['dataset_uri'] == 's3://datasets/paper-derived/train.csv'
    assert payload['run']['workflow_id'] == 'literature-to-experiment'
    assert payload['run']['run_purpose'] == 'paper-pipeline'
    assert payload['report_state']['run_status'] == 'accepted'
    assert payload['report_state']['report_available'] is True
    assert 'report.md' in payload['report_state']['artifact_names']
    assert payload['next_action'] == 'await-run-completion'


def test_create_fresh_paper_pipeline_stops_for_replication_review_boundary() -> None:
    client = build_client()

    response = client.post(
        '/paper-pipelines/fresh-paper',
        json={
            'paper_ref': 'https://github.com/example/project-paper',
            'raw_request': 'Replicate this paper from the linked repository and compare against the reported baseline.',
            'notes': ['Focus on the replication path and reported evaluation target.'],
            'wait_for_terminal_state': False,
        },
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload['assessment']['recommended_workflow_id'] == 'replication-lite'
    assert payload['design']['workflow_id'] == 'replication-lite'
    assert payload['design']['status'] == 'needs_review'
    assert payload['run'] is None
    assert payload['next_action'] == 'review-required'
    assert payload['report_state']['run_status'] == 'not-submitted'


def test_create_pipeline_from_research_problem_runs_top_candidate(monkeypatch) -> None:
    client = build_client()

    class FakeResponse:
        def __init__(self, payload: dict) -> None:
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            import json
            return json.dumps(self.payload).encode('utf-8')

    def fake_urlopen(request_obj, timeout):
        import json
        from urllib.error import URLError
        body = json.loads(request_obj.data.decode('utf-8'))
        if 'problem_statement' not in body:
            raise URLError('agent unavailable in test fixture')
        assert body['problem_statement'].startswith('Find a bounded benchmark')
        return FakeResponse(
            {
                'request_id': 'problem-plan-1',
                'selected_tracks': [
                    {
                        'track_id': 'agent_evaluation',
                        'description': 'benchmarks for measuring ML or research-agent capability rather than just model quality',
                        'default_priority': 'P1',
                        'queries': ['site:arxiv.org machine learning agents benchmark Kaggle'],
                    }
                ],
                'selected_queries': [
                    {
                        'track': 'agent_evaluation',
                        'queries': ['site:arxiv.org machine learning agents benchmark Kaggle'],
                    }
                ],
                'selected_papers': [
                    {
                        'paper_id': 'mle_bench_arxiv_2024',
                        'title': 'MLE-bench: Evaluating Machine Learning Agents on Machine Learning Engineering',
                        'year': 2024,
                        'venue': 'arXiv',
                        'priority': 'P1',
                        'tracks': ['agent_evaluation'],
                        'bounded_job_fit': 4,
                        'replication_complexity': 4,
                        'official_page': 'https://arxiv.org/abs/2410.07095',
                        'pdf_url': None,
                        'why_seed': 'benchmark for measuring whether agents can do bounded ML engineering work on real tasks',
                        'first_jobs': ['adapt a reduced internal benchmark using 3-5 public competitions or equivalent tasks'],
                        'tags': ['agents', 'kaggle', 'evaluation', 'ml_engineering'],
                    }
                ],
                'approved_sources': {
                    'manifest_name': 'glasslab_paper_harvester_seed_manifest',
                    'manifest_version': 1,
                    'venue_count': 9,
                    'paper_count': 12,
                    'track_query_count': 6,
                    'approved_hosts': ['arxiv.org'],
                },
                'warnings': [],
            }
        )

    monkeypatch.setattr(main_module.urllib_request, 'urlopen', fake_urlopen)

    response = client.post(
        '/paper-pipelines/from-research-problem',
        json={
            'problem_statement': 'Find a bounded benchmark for research agents doing machine learning engineering work.',
            'max_candidate_papers': 1,
            'wait_for_terminal_state': False,
        },
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload['chosen_paper_id'] == 'mle_bench_arxiv_2024'
    assert payload['selected_tracks'] == ['agent_evaluation']
    assert payload['pipeline']['run'] is None
    assert payload['pipeline']['next_action'] == 'review-required'
    assert payload['next_action'] == 'review-required'
    assert payload['pipeline']['intake']['source_refs'][0] == 'https://arxiv.org/abs/2410.07095'


def test_create_pipeline_from_research_problem_returns_no_candidates(monkeypatch) -> None:
    client = build_client()

    class FakeResponse:
        def __init__(self, payload: dict) -> None:
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            import json
            return json.dumps(self.payload).encode('utf-8')

    monkeypatch.setattr(
        main_module.urllib_request,
        'urlopen',
        lambda request_obj, timeout: FakeResponse(
            {
                'request_id': 'problem-plan-2',
                'selected_tracks': [],
                'selected_queries': [],
                'selected_papers': [],
                'approved_sources': {
                    'manifest_name': 'glasslab_paper_harvester_seed_manifest',
                    'manifest_version': 1,
                    'venue_count': 9,
                    'paper_count': 12,
                    'track_query_count': 6,
                    'approved_hosts': ['arxiv.org'],
                },
                'warnings': ['no approved seed papers matched the research problem strongly enough'],
            }
        ),
    )

    response = client.post(
        '/paper-pipelines/from-research-problem',
        json={'problem_statement': 'Solve some vague thing somehow.'},
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload['pipeline'] is None
    assert payload['next_action'] == 'no-paper-candidates'
    assert payload['warnings'] == ['no approved seed papers matched the research problem strongly enough']


def test_stage_and_get_latest_research_problem() -> None:
    client = build_client()

    create = client.post(
        '/research-problems',
        json={
            'problem_statement': 'Find a bounded benchmark for research agents doing machine learning engineering work.',
            'max_candidate_papers': 2,
            'priorities': ['boundedness', 'artifact quality'],
            'submitted_by': 'operator',
        },
    )

    assert create.status_code == 201
    payload = create.json()
    assert payload['status'] == 'staged'
    assert payload['problem_statement'].startswith('Find a bounded benchmark')
    assert payload['priorities'] == ['boundedness', 'artifact quality']

    latest = client.get('/research-problems/latest')
    assert latest.status_code == 200
    assert latest.json()['problem_id'] == payload['problem_id']


def test_research_session_bootstrap_status_reports_missing_state_and_staged_problem() -> None:
    client = build_client()

    initial = client.get('/research-sessions/bootstrap-status')
    assert initial.status_code == 200
    assert initial.json() == {
        'active_session': None,
        'staged_research_problem': None,
        'recommended_next_action': 'create-session-manually',
        'can_create_session_from_latest_problem': False,
        'can_apply_session_skills': False,
        'detail': 'no active research session or staged research problem exists yet',
    }

    staged = client.post(
        '/research-problems',
        json={
            'problem_statement': 'Find a bounded benchmark for research agents doing machine learning engineering work.',
            'submitted_by': 'operator',
        },
    )
    assert staged.status_code == 201

    after_problem = client.get('/research-sessions/bootstrap-status')
    assert after_problem.status_code == 200
    payload = after_problem.json()
    assert payload['active_session'] is None
    assert payload['staged_research_problem']['problem_id'] == staged.json()['problem_id']
    assert payload['recommended_next_action'] == 'create-session-from-latest-problem'
    assert payload['can_create_session_from_latest_problem'] is True
    assert payload['can_apply_session_skills'] is False


def test_research_session_bootstrap_reports_manual_action_when_empty() -> None:
    client = build_client()

    response = client.post('/research-sessions/bootstrap')
    assert response.status_code == 200
    payload = response.json()
    assert payload == {
        'bootstrap_action': 'create-session-manually',
        'session': None,
        'staged_research_problem': None,
        'detail': 'no active research session or staged research problem exists yet',
    }


def test_research_session_bootstrap_creates_session_from_latest_problem() -> None:
    client = build_client()

    staged = client.post(
        '/research-problems',
        json={
            'problem_statement': 'Find a bounded benchmark for research agents doing machine learning engineering work.',
            'submitted_by': 'operator',
        },
    )
    assert staged.status_code == 201

    response = client.post('/research-sessions/bootstrap')
    assert response.status_code == 200
    payload = response.json()
    assert payload['bootstrap_action'] == 'created-session-from-latest-problem'
    assert payload['session']['goal_statement'].startswith('Find a bounded benchmark')
    assert payload['staged_research_problem']['problem_id'] == staged.json()['problem_id']

    latest_problem = client.get('/research-problems/latest')
    assert latest_problem.status_code == 200
    assert latest_problem.json()['session_id'] == payload['session']['session_id']

    reused = client.post('/research-sessions/bootstrap')
    assert reused.status_code == 200
    reused_payload = reused.json()
    assert reused_payload['bootstrap_action'] == 'reuse-active-session'
    assert reused_payload['session']['session_id'] == payload['session']['session_id']


def test_start_literature_search_creates_session_problem_and_queue(monkeypatch) -> None:
    client = build_client()

    class FakeResponse:
        def __init__(self, payload: dict) -> None:
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            import json
            return json.dumps(self.payload).encode('utf-8')

    monkeypatch.setattr(
        main_module.urllib_request,
        'urlopen',
        lambda request_obj, timeout: FakeResponse(
            {
                'request_id': 'start-literature-search-1',
                'selected_tracks': [{'track_id': 'computer_vision', 'track': 'computer_vision'}],
                'selected_queries': [{'queries': ['computer vision art forgery detection dataset']}],
                'selected_papers': [
                    {
                        'paper_id': 'cv_forgery_2025',
                        'title': 'A bounded computer vision benchmark for forged art detection',
                        'year': 2025,
                        'venue': 'arXiv',
                        'priority': 'P1',
                        'tracks': ['computer_vision'],
                        'bounded_job_fit': 4,
                        'replication_complexity': 3,
                        'official_page': 'https://arxiv.org/abs/2501.00001',
                        'pdf_url': None,
                        'why_seed': 'matches the requested CV research direction',
                        'first_jobs': ['compare baseline losses on a bounded image split'],
                        'tags': ['computer_vision', 'forgery_detection'],
                    }
                ],
                'warnings': [],
            }
        ),
    )

    response = client.post(
        '/research-sessions/start-literature-search',
        json={
            'goal_statement': 'Detect forged art using computer vision methods and open image datasets.',
            'priorities': ['computer vision', 'bounded experiments'],
            'submitted_by': 'operator',
        },
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload['session']['goal_statement'].startswith('Detect forged art')
    assert payload['research_problem']['session_id'] == payload['session']['session_id']
    assert payload['paper_intake_queue']['session_id'] == payload['session']['session_id']
    assert payload['paper_intake_queue']['candidates'][0]['paper_id'] == 'cv_forgery_2025'
    assert payload['operation']['operation_type'] == 'literature-search-start'


def test_start_literature_search_creates_new_session_for_new_goal(monkeypatch) -> None:
    client = build_client()

    class FakeResponse:
        def __init__(self, payload: dict) -> None:
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            import json
            return json.dumps(self.payload).encode('utf-8')

    monkeypatch.setattr(
        main_module.urllib_request,
        'urlopen',
        lambda request_obj, timeout: FakeResponse(
            {
                'request_id': 'start-literature-search-2',
                'selected_tracks': [{'track_id': 'metric_learning', 'track': 'metric_learning'}],
                'selected_queries': [{'queries': ['dreamsim visual similarity metric replication']}],
                'selected_papers': [
                    {
                        'paper_id': 'dreamsim_2024',
                        'title': 'DreamSim: Perceptual Similarity from Human Feedback',
                        'year': 2024,
                        'venue': 'arXiv',
                        'priority': 'P1',
                        'tracks': ['metric_learning'],
                        'bounded_job_fit': 4,
                        'replication_complexity': 3,
                        'official_page': 'https://arxiv.org/abs/2405.00001',
                        'pdf_url': None,
                        'why_seed': 'matches the requested replication topic',
                        'first_jobs': ['replicate the visual similarity benchmark on a bounded split'],
                        'tags': ['vision', 'similarity'],
                    }
                ],
                'warnings': [],
            }
        ),
    )

    first_response = client.post(
        '/research-sessions/start-literature-search',
        json={
            'goal_statement': 'Detect forged art using computer vision methods and open image datasets.',
            'priorities': ['computer vision'],
            'submitted_by': 'operator',
        },
    )
    assert first_response.status_code == 201
    first_session_id = first_response.json()['session']['session_id']

    second_response = client.post(
        '/research-sessions/start-literature-search',
        json={
            'goal_statement': 'Replicate DreamSim visual similarity metric for perceptual comparisons.',
            'priorities': ['replication'],
            'submitted_by': 'operator',
        },
    )

    assert second_response.status_code == 201
    payload = second_response.json()
    assert payload['session']['session_id'] != first_session_id
    assert payload['session']['goal_statement'].startswith('Replicate DreamSim')
    assert payload['research_problem']['session_id'] == payload['session']['session_id']
    assert 'created-session-from-new-goal' in payload['action']


def test_start_literature_search_replaces_stale_queue_for_same_goal(monkeypatch) -> None:
    settings = Settings(registry_dir=str(REPO_ROOT / 'services' / 'workflow-registry' / 'definitions'))
    registry = WorkflowRegistry(settings.registry_dir)
    store = InMemoryRunStore()
    client = TestClient(create_app(settings=settings, registry=registry, store=store))

    responses = iter(
        [
            {
                'request_id': 'start-literature-search-stale-1',
                'selected_tracks': [{'track_id': 'tabular_baselines', 'track': 'tabular_baselines'}],
                'selected_queries': [{'queries': ['xgboost tabular benchmark']}],
                'selected_papers': [
                    {
                        'paper_id': 'xgboost_kdd_2016',
                        'title': 'XGBoost: A Scalable Tree Boosting System',
                        'year': 2016,
                        'venue': 'KDD',
                        'priority': 'P1',
                        'tracks': ['tabular_baselines'],
                        'bounded_job_fit': 4,
                        'replication_complexity': 2,
                        'official_page': 'https://example.org/xgboost',
                        'pdf_url': None,
                        'why_seed': 'stale tabular baseline queue',
                        'first_jobs': ['bounded tabular baseline'],
                        'tags': ['tabular'],
                    }
                ],
                'warnings': [],
            },
            {
                'request_id': 'start-literature-search-stale-2',
                'selected_tracks': [{'track_id': 'metric_learning', 'track': 'metric_learning'}],
                'selected_queries': [{'queries': ['dreamsim visual similarity metric replication']}],
                'selected_papers': [
                    {
                        'paper_id': 'dreamsim_2024',
                        'title': 'DreamSim: Perceptual Similarity from Human Feedback',
                        'year': 2024,
                        'venue': 'arXiv',
                        'priority': 'P1',
                        'tracks': ['metric_learning'],
                        'bounded_job_fit': 4,
                        'replication_complexity': 3,
                        'official_page': 'https://example.org/dreamsim',
                        'pdf_url': None,
                        'why_seed': 'fresh metric-learning queue',
                        'first_jobs': ['bounded similarity replication'],
                        'tags': ['vision', 'similarity'],
                    }
                ],
                'warnings': [],
            },
        ]
    )

    class FakeResponse:
        def __init__(self, payload: dict) -> None:
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            import json
            return json.dumps(self.payload).encode('utf-8')

    monkeypatch.setattr(
        main_module.urllib_request,
        'urlopen',
        lambda request_obj, timeout: FakeResponse(next(responses)),
    )

    first_response = client.post(
        '/research-sessions/start-literature-search',
        json={
            'goal_statement': 'Replicate DreamSim visual similarity metric for perceptual comparisons.',
            'priorities': ['replication'],
            'submitted_by': 'operator',
        },
    )
    assert first_response.status_code == 201
    first_queue_id = first_response.json()['paper_intake_queue']['queue_id']
    assert first_response.json()['paper_intake_queue']['candidates'][0]['paper_id'] == 'xgboost_kdd_2016'
    stale_queue = store.get_paper_intake_queue(first_queue_id)
    assert stale_queue is not None
    store.save_paper_intake_queue(
        stale_queue.model_copy(
            update={
                'problem_statement': 'stale queue from an unrelated tabular benchmark',
                'updated_at': datetime.now(timezone.utc),
            }
        )
    )

    second_response = client.post(
        '/research-sessions/start-literature-search',
        json={
            'goal_statement': 'Replicate DreamSim visual similarity metric for perceptual comparisons.',
            'priorities': ['replication'],
            'submitted_by': 'operator',
        },
    )
    assert second_response.status_code == 201
    payload = second_response.json()
    assert payload['paper_intake_queue']['queue_id'] != first_queue_id
    assert payload['paper_intake_queue']['candidates'][0]['paper_id'] == 'dreamsim_2024'
    assert 'replaced-stale-queue' in payload['action']


def test_external_literature_search_skill_creates_session_queue(monkeypatch) -> None:
    from app.external_literature import ExternalLiteratureResult
    from app.schemas import ResearchProblemPaperCandidate

    client = build_client()

    session = client.post(
        '/research-sessions',
        json={
            'goal_statement': 'Detect forged art using computer vision methods and open image datasets.',
            'priorities': ['computer vision', 'art authentication'],
            'submitted_by': 'operator',
        },
    )
    assert session.status_code == 201
    session_id = session.json()['session_id']

    problem = client.post(f'/research-sessions/{session_id}/skills/research-problem')
    assert problem.status_code == 201

    monkeypatch.setattr(
        main_module,
        'search_external_literature',
        lambda **kwargs: ExternalLiteratureResult(
            selected_tracks=['external_literature', 'openalex', 'arxiv'],
            selected_queries=['forged art computer vision dataset'],
            selected_papers=[
                ResearchProblemPaperCandidate(
                    paper_id='https://openalex.org/W123',
                    title='Vision Transformers for Art Authentication',
                    year=2024,
                    venue='CVPR Workshops',
                    venue_id='https://openalex.org/S123',
                    priority='P1',
                    tracks=['external_literature', 'openalex'],
                    bounded_job_fit=3,
                    replication_complexity=3,
                    official_page='https://openalex.org/W123',
                    pdf_url='https://arxiv.org/pdf/2401.12345.pdf',
                    why_seed='Matched the external literature query through OpenAlex.',
                    first_jobs=['compare the loss function and dataset split against session priorities'],
                    tags=['computer_vision', 'art_authentication'],
                    match_score=4,
                    match_reasons=["matched term 'forged'", "matched term 'vision'"],
                )
            ],
            coverage_summary={
                'mode': 'external_search',
                'provider_counts': {'openalex': 1, 'arxiv': 1, 'crossref': 0},
                'selected_candidate_count': 1,
                'selected_provider_mix': ['openalex', 'arxiv'],
            },
            warnings=[],
        ),
    )

    response = client.post(f'/research-sessions/{session_id}/skills/external-literature-search')
    assert response.status_code == 201
    payload = response.json()
    assert payload['session_id'] == session_id
    assert payload['coverage_summary']['mode'] == 'external_search'
    assert payload['candidates'][0]['title'] == 'Vision Transformers for Art Authentication'
    assert payload['candidates'][0]['pdf_url'] == 'https://arxiv.org/pdf/2401.12345.pdf'

    latest_context = client.get('/research-sessions/latest/context')
    assert latest_context.status_code == 200
    assert latest_context.json()['paper_intake_queue']['queue_id'] == payload['queue_id']


def test_external_literature_reranker_prefers_relevant_and_diverse_candidates() -> None:
    from app.external_literature import _score_candidate, _select_diverse_top_candidates
    from app.schemas import ResearchProblemPaperCandidate

    terms = ['forged', 'art', 'vision', 'dataset']
    candidates = [
        ResearchProblemPaperCandidate(
            paper_id='paper-a',
            title='Vision Transformers for Forged Art Detection',
            year=2025,
            venue='arXiv',
            priority='P1',
            tracks=['external_literature', 'arxiv'],
            bounded_job_fit=3,
            replication_complexity=3,
            official_page='https://arxiv.org/abs/2501.00001',
            pdf_url='https://arxiv.org/pdf/2501.00001.pdf',
            why_seed='Matched the external literature query through arXiv search.',
            first_jobs=['compare methods and dataset choices'],
            tags=['computer_vision', 'art_authentication'],
        ),
        ResearchProblemPaperCandidate(
            paper_id='paper-b',
            title='Vision Transformers for Forged Art Attribution',
            year=2024,
            venue='arXiv',
            priority='P1',
            tracks=['external_literature', 'arxiv'],
            bounded_job_fit=3,
            replication_complexity=3,
            official_page='https://arxiv.org/abs/2501.00002',
            pdf_url='https://arxiv.org/pdf/2501.00002.pdf',
            why_seed='Matched the external literature query through arXiv search.',
            first_jobs=['compare methods and dataset choices'],
            tags=['computer_vision', 'art_authentication'],
        ),
        ResearchProblemPaperCandidate(
            paper_id='paper-c',
            title='Open Dataset Benchmarks for Art Authentication',
            year=2023,
            venue='OpenReview',
            priority='P1',
            tracks=['external_literature', 'openalex'],
            bounded_job_fit=3,
            replication_complexity=3,
            official_page='https://openalex.org/W123',
            pdf_url=None,
            why_seed='Matched the external literature query through OpenAlex.',
            first_jobs=['review open datasets used in prior work'],
            tags=['open_dataset', 'art_authentication'],
        ),
    ]

    scored = [_score_candidate(candidate, terms) for candidate in candidates]
    selected = _select_diverse_top_candidates(
        sorted(scored, key=lambda item: (item.match_score, item.year), reverse=True),
        2,
    )

    assert selected[0].paper_id == 'paper-a'
    assert {candidate.paper_id for candidate in selected} == {'paper-a', 'paper-c'}


def test_dblp_candidates_parse_search_results(monkeypatch) -> None:
    from app.config import Settings
    from app.external_literature import _dblp_candidates

    monkeypatch.setattr(
        'app.external_literature._request_json',
        lambda *args, **kwargs: {
            'result': {
                'hits': {
                    'hit': [
                        {
                            'info': {
                                'title': 'Forgery Detection with Vision Transformers',
                                'year': '2024',
                                'venue': 'ICCV Workshops',
                                'key': 'conf/iccv/Forgery2024',
                                'url': 'https://dblp.org/rec/conf/iccv/Forgery2024',
                                'ee': 'https://example.org/forgery2024.pdf',
                            }
                        }
                    ]
                }
            }
        },
    )

    candidates = _dblp_candidates('forgery detection vision transformers', 5, Settings())

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.title == 'Forgery Detection with Vision Transformers'
    assert candidate.tracks == ['external_literature', 'dblp']
    assert candidate.pdf_url == 'https://example.org/forgery2024.pdf'
    assert candidate.official_page == 'https://dblp.org/rec/conf/iccv/Forgery2024'


def test_add_manual_paper_to_latest_session_queue() -> None:
    client = build_client()

    session = client.post(
        '/research-sessions',
        json={
            'goal_statement': 'Detect forged art using computer vision methods and open image datasets.',
            'submitted_by': 'operator',
        },
    )
    assert session.status_code == 201

    response = client.post(
        '/research-sessions/latest/paper-intake-queue/manual-paper',
        json={
            'title': 'Forgery detection with vision transformers',
            'official_page': 'https://arxiv.org/abs/2401.12345',
            'tags': ['computer_vision', 'manual'],
            'notes': ['check whether the loss differs from the current shortlist'],
            'submitted_by': 'operator',
        },
    )
    assert response.status_code == 201
    payload = response.json()
    assert payload['status'] == 'ready'
    assert payload['coverage_summary']['manual'] is True
    assert payload['candidates'][-1]['title'] == 'Forgery detection with vision transformers'
    assert payload['candidates'][-1]['official_page'] == 'https://arxiv.org/abs/2401.12345'
    assert payload['candidates'][-1]['pdf_url'] == 'https://arxiv.org/pdf/2401.12345.pdf'


def test_create_pipeline_from_latest_research_problem(monkeypatch) -> None:
    client = build_client()

    class FakeResponse:
        def __init__(self, payload: dict) -> None:
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            import json
            return json.dumps(self.payload).encode('utf-8')

    def fake_urlopen(request_obj, timeout):
        return FakeResponse(
            {
                'request_id': 'problem-plan-latest',
                'selected_tracks': [{'track_id': 'agent_evaluation', 'track': 'agent_evaluation'}],
                'selected_queries': [{'queries': ['site:arxiv.org machine learning agents benchmark Kaggle']}],
                'selected_papers': [
                    {
                        'paper_id': 'mle_bench_arxiv_2024',
                        'title': 'MLE-bench: Evaluating Machine Learning Agents on Machine Learning Engineering',
                        'year': 2024,
                        'venue': 'arXiv',
                        'priority': 'P1',
                        'tracks': ['agent_evaluation'],
                        'bounded_job_fit': 4,
                        'replication_complexity': 4,
                        'official_page': 'https://arxiv.org/abs/2410.07095',
                        'pdf_url': None,
                        'why_seed': 'benchmark for measuring whether agents can do bounded ML engineering work on real tasks',
                        'first_jobs': ['adapt a reduced internal benchmark using 3-5 public competitions or equivalent tasks'],
                        'tags': ['agents', 'kaggle', 'evaluation', 'ml_engineering'],
                    }
                ],
                'approved_sources': {
                    'manifest_name': 'glasslab_paper_harvester_seed_manifest',
                    'manifest_version': 1,
                    'venue_count': 9,
                    'paper_count': 12,
                    'track_query_count': 6,
                    'approved_hosts': ['arxiv.org'],
                },
                'warnings': [],
            }
        )

    monkeypatch.setattr(main_module.urllib_request, 'urlopen', fake_urlopen)

    staged = client.post(
        '/research-problems',
        json={
            'problem_statement': 'Find a bounded benchmark for research agents doing machine learning engineering work.',
            'max_candidate_papers': 1,
            'submitted_by': 'operator',
            'wait_for_terminal_state': False,
        },
    )
    assert staged.status_code == 201

    response = client.post('/paper-pipelines/from-latest-research-problem')
    assert response.status_code == 201
    payload = response.json()
    assert payload['problem_statement'].startswith('Find a bounded benchmark')
    assert payload['chosen_paper_id'] == 'mle_bench_arxiv_2024'
    assert payload['pipeline']['run'] is None
    assert payload['pipeline']['next_action'] == 'review-required'
    assert payload['next_action'] == 'review-required'


def test_create_and_fetch_paper_intake_queue(monkeypatch) -> None:
    client = build_client()

    class FakeResponse:
        def __init__(self, payload: dict) -> None:
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            import json
            return json.dumps(self.payload).encode('utf-8')

    monkeypatch.setattr(
        main_module.urllib_request,
        'urlopen',
        lambda request_obj, timeout: FakeResponse(
            {
                'request_id': 'paper-queue-1',
                'selected_tracks': [{'track_id': 'agent_evaluation', 'track': 'agent_evaluation'}],
                'selected_queries': [{'queries': ['site:arxiv.org machine learning agents benchmark Kaggle']}],
                'selected_papers': [
                    {
                        'paper_id': 'mle_bench_arxiv_2024',
                        'title': 'MLE-bench: Evaluating Machine Learning Agents on Machine Learning Engineering',
                        'year': 2024,
                        'venue': 'arXiv',
                        'priority': 'P1',
                        'tracks': ['agent_evaluation'],
                        'bounded_job_fit': 4,
                        'replication_complexity': 4,
                        'official_page': 'https://arxiv.org/abs/2410.07095',
                        'pdf_url': None,
                        'why_seed': 'benchmark for measuring whether agents can do bounded ML engineering work on real tasks',
                        'first_jobs': ['adapt a reduced internal benchmark using 3-5 public competitions or equivalent tasks'],
                        'tags': ['agents'],
                    },
                    {
                        'paper_id': 'second_paper',
                        'title': 'Another bounded paper',
                        'year': 2023,
                        'venue': 'arXiv',
                        'priority': 'P2',
                        'tracks': ['agent_evaluation'],
                        'bounded_job_fit': 3,
                        'replication_complexity': 2,
                        'official_page': 'https://arxiv.org/abs/2301.00001',
                        'pdf_url': None,
                        'why_seed': 'secondary candidate',
                        'first_jobs': ['check reported setup'],
                        'tags': ['agents'],
                    },
                ],
                'warnings': [],
            }
        ),
    )

    create = client.post(
        '/paper-intake-queues/from-research-problem',
        json={
            'problem_statement': 'Find bounded literature we can stage into intake before doing deeper understanding.',
            'max_candidate_papers': 2,
            'submitted_by': 'operator',
        },
    )

    assert create.status_code == 201
    payload = create.json()
    assert payload['status'] == 'ready'
    assert len(payload['candidates']) == 2
    assert payload['candidates'][0]['intake_status'] == 'pending'

    latest = client.get('/paper-intake-queues/latest')
    assert latest.status_code == 200
    assert latest.json()['queue_id'] == payload['queue_id']

    listed = client.get('/paper-intake-queues')
    assert listed.status_code == 200
    assert listed.json()[0]['queue_id'] == payload['queue_id']


def test_stage_next_intake_from_paper_queue(monkeypatch) -> None:
    client = build_client()

    class FakeResponse:
        def __init__(self, payload: dict) -> None:
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            import json
            return json.dumps(self.payload).encode('utf-8')

    monkeypatch.setattr(
        main_module.urllib_request,
        'urlopen',
        lambda request_obj, timeout: FakeResponse(
            {
                'request_id': 'paper-queue-2',
                'selected_tracks': [{'track_id': 'agent_evaluation', 'track': 'agent_evaluation'}],
                'selected_queries': [{'queries': ['site:arxiv.org machine learning agents benchmark Kaggle']}],
                'selected_papers': [
                    {
                        'paper_id': 'mle_bench_arxiv_2024',
                        'title': 'MLE-bench: Evaluating Machine Learning Agents on Machine Learning Engineering',
                        'year': 2024,
                        'venue': 'arXiv',
                        'priority': 'P1',
                        'tracks': ['agent_evaluation'],
                        'bounded_job_fit': 4,
                        'replication_complexity': 4,
                        'official_page': 'https://arxiv.org/abs/2410.07095',
                        'pdf_url': None,
                        'why_seed': 'benchmark for measuring whether agents can do bounded ML engineering work on real tasks',
                        'first_jobs': ['adapt a reduced internal benchmark using 3-5 public competitions or equivalent tasks'],
                        'tags': ['agents'],
                    }
                ],
                'warnings': [],
            }
        ),
    )
    monkeypatch.setattr(
        main_module,
        'ingest_source_document',
        lambda source_url, submitted_by, settings, store, session_id=None, expected_title=None: main_module.SourceDocumentRecord(
            document_id='doc-1',
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            status='fetched',
            source_url=source_url,
            submitted_by=submitted_by,
            storage_uri='file:///tmp/source-documents/doc-1/source.html',
            content_type='text/html',
            size_bytes=42,
            sha256='abc123',
            title='source.html',
            session_id=session_id,
        ),
    )

    create = client.post(
        '/paper-intake-queues/from-research-problem',
        json={
            'problem_statement': 'Find bounded literature we can stage into intake before doing deeper understanding.',
            'max_candidate_papers': 1,
        },
    )
    assert create.status_code == 201
    queue_id = create.json()['queue_id']

    stage = client.post(f'/paper-intake-queues/{queue_id}/stage-next-intake')
    assert stage.status_code == 201
    intake = stage.json()
    assert intake['source_refs'][0] == 'https://arxiv.org/abs/2410.07095'
    assert intake['document_refs'] == ['doc-1']
    assert intake['status'] == 'ready_for_design'

    updated_queue = client.get(f'/paper-intake-queues/{queue_id}')
    assert updated_queue.status_code == 200
    queue_payload = updated_queue.json()
    assert queue_payload['status'] == 'exhausted'
    assert queue_payload['candidates'][0]['intake_status'] == 'staged'
    assert queue_payload['candidates'][0]['staged_intake_id'] == intake['intake_id']

    exhausted = client.post(f'/paper-intake-queues/{queue_id}/stage-next-intake')
    assert exhausted.status_code == 409
    assert exhausted.json()['detail'] == 'paper intake queue is exhausted'

    latest_document = client.get('/source-documents/latest')
    assert latest_document.status_code == 200
    assert latest_document.json()['document_id'] == 'doc-1'
    assert latest_document.json()['validation_status'] == 'unknown'


def test_stage_next_intake_falls_back_from_pdf_to_official_page(monkeypatch) -> None:
    client = build_client()

    class FakeResponse:
        def __init__(self, payload: dict) -> None:
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            import json
            return json.dumps(self.payload).encode('utf-8')

    monkeypatch.setattr(
        main_module.urllib_request,
        'urlopen',
        lambda request_obj, timeout: FakeResponse(
            {
                'request_id': 'paper-queue-3',
                'selected_tracks': [{'track_id': 'agent_evaluation', 'track': 'agent_evaluation'}],
                'selected_queries': [{'queries': ['site:arxiv.org machine learning agents benchmark Kaggle']}],
                'selected_papers': [
                    {
                        'paper_id': 'mle_bench_arxiv_2024',
                        'title': 'MLE-bench: Evaluating Machine Learning Agents on Machine Learning Engineering',
                        'year': 2024,
                        'venue': 'arXiv',
                        'priority': 'P1',
                        'tracks': ['agent_evaluation'],
                        'bounded_job_fit': 4,
                        'replication_complexity': 4,
                        'official_page': 'https://arxiv.org/abs/2410.07095',
                        'pdf_url': 'https://arxiv.org/pdf/2410.07095.pdf',
                        'why_seed': 'benchmark for measuring whether agents can do bounded ML engineering work on real tasks',
                        'first_jobs': ['adapt a reduced internal benchmark using 3-5 public competitions or equivalent tasks'],
                        'tags': ['agents'],
                    }
                ],
                'warnings': [],
            }
        ),
    )

    attempted_urls: list[str] = []

    def fake_ingest(source_url, submitted_by, settings, store, session_id=None, expected_title=None):
        attempted_urls.append(source_url)
        mismatch = source_url.endswith('.pdf')
        return main_module.SourceDocumentRecord(
            document_id='doc-fallback' if not mismatch else 'doc-mismatch',
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            status='fetched',
            source_url=source_url,
            submitted_by=submitted_by,
            storage_uri='file:///tmp/source-documents/doc-fallback/source.html',
            content_type='text/html',
            size_bytes=42,
            sha256='abc123',
            title='source.html',
            expected_title='MLE-bench: Evaluating Machine Learning Agents on Machine Learning Engineering',
            validation_status='mismatch' if mismatch else 'matched',
            validation_notes=['expected title terms not found'] if mismatch else ['matched title terms: benchmark'],
            session_id=session_id,
        )

    monkeypatch.setattr(main_module, 'ingest_source_document', fake_ingest)

    create = client.post(
        '/paper-intake-queues/from-research-problem',
        json={
            'problem_statement': 'Find bounded literature we can stage into intake before doing deeper understanding.',
            'max_candidate_papers': 1,
        },
    )
    assert create.status_code == 201
    queue_id = create.json()['queue_id']

    stage = client.post(f'/paper-intake-queues/{queue_id}/stage-next-intake')
    assert stage.status_code == 201
    assert attempted_urls == [
        'https://arxiv.org/pdf/2410.07095.pdf',
        'https://arxiv.org/abs/2410.07095',
    ]
    payload = stage.json()
    assert any('did not match expected paper title' in note for note in payload['notes'])


def test_validate_document_identity_marks_title_mismatch() -> None:
    status, notes = source_documents.validate_document_identity(
        expected_title='Forgery detection with vision transformers',
        fetched_title='2401.12345.pdf',
        text_excerpt='Distributionally Robust Receive Combining for Wireless Transmission',
    )
    assert status == 'mismatch'
    assert notes


def test_validate_document_identity_accepts_exact_title_match() -> None:
    status, notes = source_documents.validate_document_identity(
        expected_title='Forgery Detection with Vision Transformers',
        fetched_title='Forgery Detection with Vision Transformers',
        text_excerpt=None,
    )
    assert status == 'matched'
    assert any('exactly matched' in note for note in notes)


def test_build_intake_request_dedupes_manual_and_validation_notes() -> None:
    queue = main_module.PaperIntakeQueueRecord(
        queue_id='queue-1',
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        status='ready',
        problem_statement='Investigate forged-art detection with computer vision.',
        selected_tracks=['computer_vision_forgery'],
        selected_queries=['forged art detection'],
        candidates=[],
        coverage_summary={},
        submitted_by='operator',
    )
    candidate = main_module.PaperIntakeCandidateRecord(
        paper_id='manual-paper-1',
        title='Forgery Detection with Vision Transformers',
        year=2026,
        venue='manual',
        priority='manual',
        tracks=['computer_vision_forgery'],
        bounded_job_fit=3,
        replication_complexity=2,
        official_page='https://arxiv.org/abs/2501.00001',
        pdf_url='https://arxiv.org/pdf/2501.00001.pdf',
        why_seed='Manually added by the operator.',
        first_jobs=['Manually added by the operator.'],
        tags=['manual'],
    )

    intake = main_module.build_intake_request_from_problem_candidate(
        queue,
        candidate,
        extra_notes=['Manually added by the operator.', 'Fetched source did not match expected paper title: forgery'],
    )

    assert intake.notes.count('Manually added by the operator.') == 1
    assert any('did not match expected paper title' in note for note in intake.notes)


def test_extract_document_metadata_from_arxiv_abstract_page() -> None:
    excerpt = (
        '[2401.12345] Distributionally Robust Receive Combining '
        'Title: Distributionally Robust Receive Combining '
        'Authors: Shixiong Wang, Wei Dai, Geoffrey Ye Li '
        'View PDF HTML (experimental) '
        'Abstract: This article investigates signal estimation in wireless transmission with a transformer baseline, '
        'cross entropy loss, and accuracy as the main metric on a Kaggle benchmark. '
        'Subjects: Signal Processing'
    )
    metadata = source_documents.extract_document_metadata(
        source_url='https://arxiv.org/abs/2401.12345',
        guessed_title='2401.12345',
        text_excerpt=excerpt,
    )
    assert metadata['title'] == 'Distributionally Robust Receive Combining'
    assert metadata['authors'] == ['Shixiong Wang', 'Wei Dai', 'Geoffrey Ye Li']
    assert 'signal estimation' in (metadata['abstract_excerpt'] or '').lower()
    assert 'cross entropy' in metadata['loss_hints']
    assert 'transformer' in metadata['architecture_hints']
    assert 'baseline' in metadata['baseline_hints']
    assert 'accuracy' in metadata['metric_hints']
    assert 'kaggle' in metadata['dataset_hints']


def test_extract_document_metadata_captures_python_library_hints() -> None:
    excerpt = (
        'We implement the method in PyTorch with torchvision and timm, '
        'train a ViT backbone, and compare against a scikit-learn baseline.'
    )
    metadata = source_documents.extract_document_metadata(
        source_url='https://example.org/paper.pdf',
        guessed_title='paper',
        text_excerpt=excerpt,
    )
    assert 'torch' in metadata['python_library_hints']
    assert 'torchvision' in metadata['python_library_hints']
    assert 'timm' in metadata['python_library_hints']
    assert 'scikit-learn' in metadata['python_library_hints']


def test_research_session_can_store_persistent_notes() -> None:
    client = build_client()

    create = client.post(
        '/research-sessions',
        json={
            'goal_statement': 'Investigate computer vision methods for bounded research-agent experiments.',
            'submitted_by': 'operator',
        },
    )
    assert create.status_code == 201
    session_id = create.json()['session_id']

    note = client.post(
        '/research-sessions/latest/memory',
        json={
            'working_note': 'Prioritize papers that compare alternative loss functions or curriculum strategies.',
            'decision': 'Focus the first sweep on computer vision workloads.',
            'experiment_idea': 'Compare a standard cross-entropy baseline to a focal-loss variant on the same bounded setup.',
        },
    )
    assert note.status_code == 200
    payload = note.json()
    assert payload['session_id'] == session_id
    assert 'computer vision workloads' in ' '.join(payload['decision_log'])
    assert any('focal-loss variant' in item for item in payload['next_experiment_ideas'])

    context = client.get('/research-sessions/latest/context')
    assert context.status_code == 200
    session_payload = context.json()['session']
    assert any('alternative loss functions' in item for item in session_payload['working_notes'])


def test_research_session_tracks_controlled_literature_state(monkeypatch) -> None:
    client = build_client()

    class FakeResponse:
        def __init__(self, payload: dict) -> None:
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            import json
            return json.dumps(self.payload).encode('utf-8')

    monkeypatch.setattr(
        main_module.urllib_request,
        'urlopen',
        lambda request_obj, timeout: FakeResponse(
            {
                'request_id': 'session-queue-1',
                'selected_tracks': [{'track_id': 'agent_evaluation', 'track': 'agent_evaluation'}],
                'selected_queries': [{'queries': ['controlled corpus literature search']}],
                'selected_papers': [
                    {
                        'paper_id': 'mle_bench_arxiv_2024',
                        'title': 'MLE-bench: Evaluating Machine Learning Agents on Machine Learning Engineering',
                        'year': 2024,
                        'venue': 'arXiv',
                        'priority': 'P1',
                        'tracks': ['agent_evaluation'],
                        'bounded_job_fit': 4,
                        'replication_complexity': 4,
                        'official_page': 'https://arxiv.org/abs/2410.07095',
                        'pdf_url': None,
                        'why_seed': 'benchmark for measuring whether agents can do bounded ML engineering work on real tasks',
                        'first_jobs': ['adapt a reduced internal benchmark using 3-5 public competitions or equivalent tasks'],
                        'tags': ['agents'],
                    }
                ],
                'warnings': [],
            }
        ),
    )
    monkeypatch.setattr(
        main_module,
        'ingest_source_document',
        lambda source_url, submitted_by, settings, store, session_id=None, expected_title=None: main_module.SourceDocumentRecord(
            document_id='session-doc-1',
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            status='fetched',
            source_url=source_url,
            submitted_by=submitted_by,
            storage_uri='file:///tmp/source-documents/session-doc-1/source.html',
            content_type='text/html',
            size_bytes=42,
            sha256='def456',
            title='source.html',
            text_excerpt='Controlled literature excerpt for session testing.',
            session_id=session_id,
        ),
    )

    session = client.post(
        '/research-sessions',
        json={
            'goal_statement': 'Work on a controlled-corpus literature search for bounded ML engineering benchmarks.',
            'priorities': ['controlled corpus', 'bounded experiments'],
        },
    )
    assert session.status_code == 201
    session_id = session.json()['session_id']

    problem = client.post(f'/research-sessions/{session_id}/research-problems/from-session-goal')
    assert problem.status_code == 201
    assert problem.json()['session_id'] == session_id

    queue = client.post(f'/research-sessions/{session_id}/paper-intake-queues/from-latest-problem')
    assert queue.status_code == 201
    assert queue.json()['session_id'] == session_id

    intake = client.post(f'/paper-intake-queues/{queue.json()["queue_id"]}/stage-next-intake')
    assert intake.status_code == 201
    assert intake.json()['session_id'] == session_id
    assert intake.json()['document_refs'] == ['session-doc-1']

    context = client.get('/research-sessions/latest/context')
    assert context.status_code == 200
    payload = context.json()
    assert payload['session']['session_id'] == session_id
    assert payload['session']['latest_problem_id'] == problem.json()['problem_id']
    assert payload['session']['latest_queue_id'] == queue.json()['queue_id']
    assert payload['session']['latest_document_id'] == 'session-doc-1'
    assert payload['session']['latest_intake_id'] == intake.json()['intake_id']
    assert payload['paper_intake_queue']['queue_id'] == queue.json()['queue_id']
    assert payload['source_document']['document_id'] == 'session-doc-1'


def test_research_session_literature_digest_includes_document_metadata(monkeypatch) -> None:
    client = build_client()

    class FakeResponse:
        def __init__(self, payload: dict) -> None:
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            import json
            return json.dumps(self.payload).encode('utf-8')

    monkeypatch.setattr(
        main_module.urllib_request,
        'urlopen',
        lambda request_obj, timeout: FakeResponse(
            {
                'request_id': 'session-digest-1',
                'selected_tracks': [{'track_id': 'computer_vision_forgery', 'track': 'computer_vision_forgery'}],
                'selected_queries': [{'queries': ['forged art computer vision detection']}],
                'selected_papers': [
                    {
                        'paper_id': 'forgery_vit_2026',
                        'title': 'Forgery Detection with Vision Transformers',
                        'year': 2026,
                        'venue': 'arXiv',
                        'priority': 'P1',
                        'tracks': ['computer_vision_forgery'],
                        'bounded_job_fit': 4,
                        'replication_complexity': 3,
                        'official_page': 'https://arxiv.org/abs/2501.00001',
                        'pdf_url': 'https://arxiv.org/pdf/2501.00001.pdf',
                        'why_seed': 'vision-transformer baseline for art-forgery detection',
                        'first_jobs': ['compare transformer and cnn baselines'],
                        'tags': ['vision transformer', 'wikiart'],
                    }
                ],
                'warnings': [],
            }
        ),
    )
    monkeypatch.setattr(
        main_module,
        'ingest_source_document',
        lambda source_url, submitted_by, settings, store, session_id=None, expected_title=None: main_module.SourceDocumentRecord(
            document_id='session-doc-digest-1',
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            status='fetched',
            source_url=source_url,
            submitted_by=submitted_by,
            storage_uri='file:///tmp/source-documents/session-doc-digest-1/source.pdf',
            content_type='application/pdf',
            size_bytes=4096,
            sha256='digest123',
            title='Forgery Detection with Vision Transformers',
            text_excerpt='Abstract: We compare a vision transformer and cnn baseline on WikiArt forgery detection.',
            authors=['Alice Example', 'Bob Example'],
            abstract_excerpt='We compare a vision transformer and cnn baseline on WikiArt forgery detection.',
            method_hints=['vision transformer', 'cnn'],
            dataset_hints=['wikiart'],
            loss_hints=['cross entropy', 'focal loss'],
            architecture_hints=['vision transformer', 'cnn'],
            baseline_hints=['baseline'],
            metric_hints=['accuracy', 'f1 score'],
            domain_task_hints=['forgery detection', 'image classification'],
            expected_title=expected_title,
            validation_status='matched',
            validation_notes=['matched title terms: forgery, detection'],
            session_id=session_id,
        ),
    )

    session = client.post(
        '/research-sessions',
        json={
            'goal_statement': 'Investigate forged-art detection with computer vision methods and open datasets.',
            'priorities': ['computer vision', 'art forgery'],
        },
    )
    assert session.status_code == 201
    session_id = session.json()['session_id']

    assert client.post(f'/research-sessions/{session_id}/research-problems/from-session-goal').status_code == 201
    queue = client.post(f'/research-sessions/{session_id}/paper-intake-queues/from-latest-problem')
    assert queue.status_code == 201
    assert client.post(f'/paper-intake-queues/{queue.json()["queue_id"]}/stage-next-intake').status_code == 201

    documents = client.get('/research-sessions/latest/source-documents')
    assert documents.status_code == 200
    docs_payload = documents.json()
    assert len(docs_payload) == 1
    assert docs_payload[0]['storage_uri'].endswith('/source.pdf')
    assert docs_payload[0]['authors'] == ['Alice Example', 'Bob Example']

    digest = client.get('/research-sessions/latest/literature-digest')
    assert digest.status_code == 200
    digest_payload = digest.json()
    assert digest_payload['session_id'] == session_id
    assert digest_payload['matched_document_count'] == 1
    assert digest_payload['top_methods'][:2] == ['cnn', 'vision transformer'] or digest_payload['top_methods'][:2] == ['vision transformer', 'cnn']
    assert digest_payload['top_datasets'] == ['wikiart']
    assert 'cross entropy' in digest_payload['top_losses']
    assert 'vision transformer' in digest_payload['top_architectures']
    assert 'baseline' in digest_payload['top_baselines']
    assert 'accuracy' in digest_payload['top_metrics']
    assert 'forgery detection' in digest_payload['top_domain_tasks']
    assert digest_payload['notable_titles'] == ['Forgery Detection with Vision Transformers']
    assert any('validated source document' in note for note in digest_payload['summary_notes'])


def test_research_session_skill_routes_drive_literature_flow(monkeypatch) -> None:
    client = build_client()

    class FakeResponse:
        def __init__(self, payload: dict) -> None:
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            import json
            return json.dumps(self.payload).encode('utf-8')

    monkeypatch.setattr(
        main_module.urllib_request,
        'urlopen',
        lambda request_obj, timeout: FakeResponse(
            {
                'request_id': 'session-skill-queue-1',
                'selected_tracks': [{'track_id': 'agent_evaluation', 'track': 'agent_evaluation'}],
                'selected_queries': [{'queries': ['bounded literature harvest']}],
                'selected_papers': [
                    {
                        'paper_id': 'mle_bench_arxiv_2024',
                        'title': 'MLE-bench: Evaluating Machine Learning Agents on Machine Learning Engineering',
                        'year': 2024,
                        'venue': 'arXiv',
                        'priority': 'P1',
                        'tracks': ['agent_evaluation'],
                        'bounded_job_fit': 4,
                        'replication_complexity': 4,
                        'official_page': 'https://arxiv.org/abs/2410.07095',
                        'pdf_url': None,
                        'why_seed': 'benchmark for bounded ML engineering work',
                        'first_jobs': ['reduce the benchmark into a smaller internal harness'],
                        'tags': ['agents'],
                    }
                ],
                'warnings': [],
            }
        ),
    )
    monkeypatch.setattr(
        main_module,
        'ingest_source_document',
        lambda source_url, submitted_by, settings, store, session_id=None, expected_title=None: main_module.SourceDocumentRecord(
            document_id='session-skill-doc-1',
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            status='fetched',
            source_url=source_url,
            submitted_by=submitted_by,
            storage_uri='file:///tmp/source-documents/session-skill-doc-1/source.html',
            content_type='text/html',
            size_bytes=42,
            sha256='ghi789',
            title='source.html',
            text_excerpt='Session skill route document excerpt.',
            session_id=session_id,
        ),
    )

    session = client.post(
        '/research-sessions',
        json={
            'goal_statement': 'Develop a bounded literature search around ML engineering benchmark papers.',
        },
    )
    assert session.status_code == 201
    session_id = session.json()['session_id']

    problem = client.post(f'/research-sessions/{session_id}/skills/research-problem')
    assert problem.status_code == 201
    assert problem.json()['session_id'] == session_id

    queue = client.post(f'/research-sessions/{session_id}/skills/literature-harvest')
    assert queue.status_code == 201
    assert queue.json()['session_id'] == session_id

    intake = client.post(f'/research-sessions/{session_id}/skills/paper-intake')
    assert intake.status_code == 201
    assert intake.json()['session_id'] == session_id
    assert intake.json()['document_refs'] == ['session-skill-doc-1']

    context = client.get(f'/research-sessions/{session_id}/context')
    assert context.status_code == 200
    payload = context.json()
    assert payload['session']['latest_problem_id'] == problem.json()['problem_id']
    assert payload['session']['latest_queue_id'] == queue.json()['queue_id']
    assert payload['session']['latest_document_id'] == 'session-skill-doc-1'
    assert payload['session']['latest_intake_id'] == intake.json()['intake_id']


def test_research_session_skill_routes_advance_interpretation_assessment_and_design(monkeypatch) -> None:
    client = build_client()

    class FakeResponse:
        def __init__(self, payload: dict) -> None:
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            import json
            return json.dumps(self.payload).encode('utf-8')

    monkeypatch.setattr(
        main_module.urllib_request,
        'urlopen',
        lambda request_obj, timeout: FakeResponse(
            {
                'request_id': 'session-skill-queue-2',
                'selected_tracks': [{'track_id': 'agent_evaluation', 'track': 'agent_evaluation'}],
                'selected_queries': [{'queries': ['bounded literature harvest']}],
                'selected_papers': [
                    {
                        'paper_id': 'mle_bench_arxiv_2024',
                        'title': 'MLE-bench: Evaluating Machine Learning Agents on Machine Learning Engineering',
                        'year': 2024,
                        'venue': 'arXiv',
                        'priority': 'P1',
                        'tracks': ['agent_evaluation'],
                        'bounded_job_fit': 4,
                        'replication_complexity': 4,
                        'official_page': 'https://arxiv.org/abs/2410.07095',
                        'pdf_url': None,
                        'why_seed': 'benchmark for bounded ML engineering work',
                        'first_jobs': ['reduce the benchmark into a smaller internal harness'],
                        'tags': ['agents'],
                    }
                ],
                'warnings': [],
            }
        ),
    )
    monkeypatch.setattr(
        main_module,
        'ingest_source_document',
        lambda source_url, submitted_by, settings, store, session_id=None, expected_title=None: main_module.SourceDocumentRecord(
            document_id='session-skill-doc-2',
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            status='fetched',
            source_url=source_url,
            submitted_by=submitted_by,
            storage_uri='file:///tmp/source-documents/session-skill-doc-2/source.html',
            content_type='text/html',
            size_bytes=42,
            sha256='jkl012',
            title='source.html',
            text_excerpt='Session skill route document excerpt.',
            session_id=session_id,
        ),
    )

    session = client.post(
        '/research-sessions',
        json={
            'goal_statement': 'Develop a bounded literature search around ML engineering benchmark papers.',
        },
    )
    assert session.status_code == 201
    session_id = session.json()['session_id']

    assert client.post(f'/research-sessions/{session_id}/skills/research-problem').status_code == 201
    assert client.post(f'/research-sessions/{session_id}/skills/literature-harvest').status_code == 201
    intake = client.post(f'/research-sessions/{session_id}/skills/paper-intake')
    assert intake.status_code == 201

    interpretation = client.post(f'/research-sessions/{session_id}/skills/interpretation')
    assert interpretation.status_code == 201
    assert interpretation.json()['session_id'] == session_id

    assessment = client.post(f'/research-sessions/{session_id}/skills/assessment')
    assert assessment.status_code == 201
    assert assessment.json()['session_id'] == session_id

    design = client.post(f'/research-sessions/{session_id}/skills/design')
    assert design.status_code == 201
    assert design.json()['session_id'] == session_id

    context = client.get(f'/research-sessions/{session_id}/context')
    assert context.status_code == 200
    payload = context.json()
    assert payload['session']['latest_interpretation_id'] == interpretation.json()['interpretation_id']
    assert payload['session']['latest_assessment_id'] == assessment.json()['assessment_id']
    assert payload['session']['latest_design_id'] == design.json()['design_id']


def test_research_session_read_routes_return_session_scoped_records(monkeypatch) -> None:
    client = build_client()

    class FakeResponse:
        def __init__(self, payload: dict) -> None:
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            import json
            return json.dumps(self.payload).encode('utf-8')

    monkeypatch.setattr(
        main_module.urllib_request,
        'urlopen',
        lambda request_obj, timeout: FakeResponse(
            {
                'request_id': 'session-read-queue-1',
                'selected_tracks': [{'track_id': 'agent_evaluation', 'track': 'agent_evaluation'}],
                'selected_queries': [{'queries': ['bounded literature harvest']}],
                'selected_papers': [
                    {
                        'paper_id': 'mle_bench_arxiv_2024',
                        'title': 'MLE-bench: Evaluating Machine Learning Agents on Machine Learning Engineering',
                        'year': 2024,
                        'venue': 'arXiv',
                        'priority': 'P1',
                        'tracks': ['agent_evaluation'],
                        'bounded_job_fit': 4,
                        'replication_complexity': 4,
                        'official_page': 'https://arxiv.org/abs/2410.07095',
                        'pdf_url': None,
                        'why_seed': 'benchmark for bounded ML engineering work',
                        'first_jobs': ['reduce the benchmark into a smaller internal harness'],
                        'tags': ['agents'],
                    }
                ],
                'warnings': [],
            }
        ),
    )
    monkeypatch.setattr(
        main_module,
        'ingest_source_document',
        lambda source_url, submitted_by, settings, store, session_id=None, expected_title=None: main_module.SourceDocumentRecord(
            document_id='session-read-doc-1',
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            status='fetched',
            source_url=source_url,
            submitted_by=submitted_by,
            storage_uri='file:///tmp/source-documents/session-read-doc-1/source.html',
            content_type='text/html',
            size_bytes=42,
            sha256='mno345',
            title='source.html',
            text_excerpt='Session read route document excerpt.',
            session_id=session_id,
        ),
    )

    session = client.post('/research-sessions', json={'goal_statement': 'Explore bounded ML engineering benchmark literature.'})
    assert session.status_code == 201
    session_id = session.json()['session_id']

    assert client.post(f'/research-sessions/{session_id}/skills/research-problem').status_code == 201
    queue = client.post(f'/research-sessions/{session_id}/skills/literature-harvest')
    intake = client.post(f'/research-sessions/{session_id}/skills/paper-intake')
    interpretation = client.post(f'/research-sessions/{session_id}/skills/interpretation')
    assessment = client.post(f'/research-sessions/{session_id}/skills/assessment')
    design = client.post(f'/research-sessions/{session_id}/skills/design')

    assert queue.status_code == 201
    assert intake.status_code == 201
    assert interpretation.status_code == 201
    assert assessment.status_code == 201
    assert design.status_code == 201

    assert client.get(f'/research-sessions/{session_id}/paper-intake-queue').json()['queue_id'] == queue.json()['queue_id']
    assert client.get(f'/research-sessions/{session_id}/source-document').json()['document_id'] == 'session-read-doc-1'
    assert client.get(f'/research-sessions/{session_id}/intake').json()['intake_id'] == intake.json()['intake_id']
    assert client.get(f'/research-sessions/{session_id}/interpretation').json()['interpretation_id'] == interpretation.json()['interpretation_id']
    assert client.get(f'/research-sessions/{session_id}/assessment').json()['assessment_id'] == assessment.json()['assessment_id']
    assert client.get(f'/research-sessions/{session_id}/design').json()['design_id'] == design.json()['design_id']


def test_operation_records_capture_literature_harvest_and_paper_intake(monkeypatch) -> None:
    client = build_client()

    class FakeResponse:
        def __init__(self, payload: dict) -> None:
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            import json
            return json.dumps(self.payload).encode('utf-8')

    monkeypatch.setattr(
        main_module.urllib_request,
        'urlopen',
        lambda request_obj, timeout: FakeResponse(
            {
                'request_id': 'op-queue-1',
                'selected_tracks': [{'track_id': 'agent_evaluation', 'track': 'agent_evaluation'}],
                'selected_queries': [{'queries': ['operations-backed literature harvest']}],
                'selected_papers': [
                    {
                        'paper_id': 'mle_bench_arxiv_2024',
                        'title': 'MLE-bench: Evaluating Machine Learning Agents on Machine Learning Engineering',
                        'year': 2024,
                        'venue': 'arXiv',
                        'priority': 'P1',
                        'tracks': ['agent_evaluation'],
                        'bounded_job_fit': 4,
                        'replication_complexity': 4,
                        'official_page': 'https://arxiv.org/abs/2410.07095',
                        'pdf_url': None,
                        'why_seed': 'benchmark for bounded ML engineering work',
                        'first_jobs': ['reduce the benchmark into a smaller internal harness'],
                        'tags': ['agents'],
                    }
                ],
                'warnings': [],
            }
        ),
    )
    monkeypatch.setattr(
        main_module,
        'ingest_source_document',
        lambda source_url, submitted_by, settings, store, session_id=None, expected_title=None: main_module.SourceDocumentRecord(
            document_id='operations-doc-1',
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            status='fetched',
            source_url=source_url,
            submitted_by=submitted_by,
            storage_uri='file:///tmp/source-documents/operations-doc-1/source.html',
            content_type='text/html',
            size_bytes=42,
            sha256='jkl012',
            title='source.html',
            text_excerpt='Operation record test document excerpt.',
            session_id=session_id,
        ),
    )

    session = client.post(
        '/research-sessions',
        json={'goal_statement': 'Use operation records to inspect the literature path.'},
    )
    session_id = session.json()['session_id']
    client.post(f'/research-sessions/{session_id}/skills/research-problem')
    queue = client.post(f'/research-sessions/{session_id}/skills/literature-harvest')
    intake = client.post(f'/research-sessions/{session_id}/skills/paper-intake')

    assert queue.status_code == 201
    assert intake.status_code == 201

    operations = client.get('/operations')
    assert operations.status_code == 200
    payload = operations.json()
    operation_types = [item['operation_type'] for item in payload]
    assert 'literature-harvest' in operation_types
    assert 'source-document-fetch' in operation_types
    assert 'paper-intake' in operation_types

    latest = client.get('/operations/latest')
    assert latest.status_code == 200
    assert latest.json()['operation_type'] == 'paper-intake'
    assert latest.json()['intake_id'] == intake.json()['intake_id']


def test_research_session_transition_routes_promote_intake_and_create_interpretation(monkeypatch) -> None:
    client = build_client()

    class FakeResponse:
        def __init__(self, payload: dict) -> None:
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            import json
            return json.dumps(self.payload).encode('utf-8')

    monkeypatch.setattr(
        main_module.urllib_request,
        'urlopen',
        lambda request_obj, timeout: FakeResponse(
            {
                'request_id': 'session-transition-queue-1',
                'selected_tracks': [{'track_id': 'agent_evaluation', 'track': 'agent_evaluation'}],
                'selected_queries': [{'queries': ['bounded literature harvest']}],
                'selected_papers': [
                    {
                        'paper_id': 'mle_bench_arxiv_2024',
                        'title': 'MLE-bench: Evaluating Machine Learning Agents on Machine Learning Engineering',
                        'year': 2024,
                        'venue': 'arXiv',
                        'priority': 'P1',
                        'tracks': ['agent_evaluation'],
                        'bounded_job_fit': 4,
                        'replication_complexity': 4,
                        'official_page': 'https://arxiv.org/abs/2410.07095',
                        'pdf_url': None,
                        'why_seed': 'benchmark for bounded ML engineering work',
                        'first_jobs': ['reduce the benchmark into a smaller internal harness'],
                        'tags': ['agents'],
                    }
                ],
                'warnings': [],
            }
        ),
    )
    monkeypatch.setattr(
        main_module,
        'ingest_source_document',
        lambda source_url, submitted_by, settings, store, session_id=None, expected_title=None: main_module.SourceDocumentRecord(
            document_id='session-transition-doc-1',
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            status='fetched',
            source_url=source_url,
            submitted_by=submitted_by,
            storage_uri='file:///tmp/source-documents/session-transition-doc-1/source.html',
            content_type='text/html',
            size_bytes=42,
            sha256='ghi789',
            title='source.html',
            text_excerpt='Session transition route document excerpt.',
            session_id=session_id,
        ),
    )

    session = client.post('/research-sessions', json={'goal_statement': 'Explore bounded ML engineering benchmark literature.'})
    assert session.status_code == 201
    session_id = session.json()['session_id']

    assert client.post(f'/research-sessions/{session_id}/skills/research-problem').status_code == 201
    assert client.post(f'/research-sessions/{session_id}/skills/literature-harvest').status_code == 201

    intake = client.post(f'/research-sessions/{session_id}/transitions/promote-paper-to-intake')
    assert intake.status_code == 201
    intake_payload = intake.json()

    interpretation = client.post(f'/research-sessions/{session_id}/transitions/create-interpretation')
    assert interpretation.status_code == 201
    interpretation_payload = interpretation.json()

    latest_interpretation = client.post('/research-sessions/latest/transitions/create-interpretation')
    assert latest_interpretation.status_code == 201

    context = client.get(f'/research-sessions/{session_id}/context')
    assert context.status_code == 200
    context_payload = context.json()
    assert context_payload['session']['latest_intake_id'] == intake_payload['intake_id']
    assert context_payload['session']['latest_interpretation_id'] == latest_interpretation.json()['interpretation_id']
    assert interpretation_payload['session_id'] == session_id


def test_resolve_intake_agent_base_url_handles_normalize_endpoint() -> None:
    settings = Settings(
        registry_dir=str(REPO_ROOT / 'services' / 'workflow-registry' / 'definitions'),
        intake_agent_url='http://glasslab-intake-agent.glasslab-v2.svc.cluster.local:8090/normalize-intake',
    )
    assert (
        main_module.resolve_intake_agent_base_url(settings)
        == 'http://glasslab-intake-agent.glasslab-v2.svc.cluster.local:8090'
    )


def test_create_run_success() -> None:
    client = build_client()

    response = client.post(
        '/runs',
        json={
            'workflow_id': 'generic-tabular-benchmark',
            'objective': 'Benchmark approved models on Titanic.',
            'inputs': {
                'dataset_name': 'titanic',
                'train_uri': 's3://datasets/titanic/train.csv',
                'test_uri': 's3://datasets/titanic/test.csv',
                'target_column': 'Survived',
            },
            'models': ['logistic_regression', 'random_forest'],
        },
    )

    assert response.status_code == 201
    payload = response.json()
    run_id = payload['run_id']
    assert payload['workflow_id'] == 'generic-tabular-benchmark'
    assert payload['status']['status'] == 'accepted'
    assert payload['manifest']['expected_artifacts']['required'][0] == 'run_manifest.json'
    assert payload['manifest']['resource_requests'] == {'cpu': '500m', 'memory': '1Gi'}
    assert payload['manifest']['resource_limits'] == {'cpu': '1', 'memory': '2Gi'}
    assert payload['manifest']['node_selector'] == {}

    run = client.get(f'/runs/{run_id}')
    assert run.status_code == 200

    artifacts = client.get(f'/runs/{run_id}/artifacts')
    assert artifacts.status_code == 200
    assert any(item['name'] == 'report.md' for item in artifacts.json()['artifacts']['artifacts'])

    logs = client.get(f'/runs/{run_id}/logs')
    assert logs.status_code == 200
    assert logs.json()['logs'][0]['message'] == 'run accepted'


def test_workflow_execution_preflight_reports_current_contract() -> None:
    client = build_client()

    response = client.get('/workflow-families/literature-to-experiment/execution-preflight')

    assert response.status_code == 200
    payload = response.json()
    assert payload['workflow_id'] == 'literature-to-experiment'
    assert payload['resource_profile'] == 'cpu-medium'


def test_session_execution_preflight_surfaces_interpretation_runtime_hints() -> None:
    settings = Settings(registry_dir=str(REPO_ROOT / 'services' / 'workflow-registry' / 'definitions'))
    registry = WorkflowRegistry(settings.registry_dir)
    store = InMemoryRunStore()
    client = TestClient(create_app(settings=settings, registry=registry, store=store))

    session = client.post(
        '/research-sessions',
        json={'goal_statement': 'Check interpretation-aware execution preflight for a GPU-shaped paper.'},
    )
    session_id = session.json()['session_id']

    intake = client.post(
        '/intakes',
        json={
            'raw_request': 'Vision-transformer image forgery detection with PyTorch and timm.',
            'source_refs': ['https://example.org/forgery-vit-note'],
            'notes': ['Prefer GPU execution and explicit package checks.'],
        },
    )
    assert intake.status_code == 201

    interpretation = client.post('/interpretations/from-latest-intake')
    assert interpretation.status_code == 201
    interpretation_payload = interpretation.json()
    assert 'torch' in interpretation_payload['recommended_python_packages']

    design = client.post('/design-drafts/from-latest-intake')
    assert design.status_code == 201
    design_payload = design.json()
    store.save_research_session(
        store.get_research_session(session_id).model_copy(
            update={'latest_design_id': design_payload['design_id']}
        )
    )

    response = client.get(f"/research-sessions/{session_id}/execution-preflight")
    assert response.status_code == 200
    payload = response.json()
    assert payload['workflow_id'] == design_payload['workflow_id']
    assert any(
        fragment in warning
        for warning in payload['warnings']
        for fragment in (
                'interpretation-recommended',
            'interpretation preferred',
            'interpretation dataset hints:',
            'interpretation evaluation targets:',
            'interpretation indicates GPU is required',
        )
    )


def test_session_execution_preflight_flags_overfitting_split_risks() -> None:
    settings = Settings(registry_dir=str(REPO_ROOT / 'services' / 'workflow-registry' / 'definitions'))
    registry = WorkflowRegistry(settings.registry_dir)
    store = InMemoryRunStore()
    client = TestClient(create_app(settings=settings, registry=registry, store=store))

    session = client.post(
        '/research-sessions',
        json={'goal_statement': 'Check that preflight flags overfitting risk from a bad split contract.'},
    )
    session_id = session.json()['session_id']

    intake = client.post(
        '/intakes',
        json={
            'raw_request': 'Benchmark a bounded tabular baseline on a suspicious split setup.',
            'source_refs': ['https://example.org/bad-split-note'],
            'notes': ['This fixture intentionally uses the same file for train and test.'],
        },
    )
    assert intake.status_code == 201

    design = client.post('/design-drafts/from-latest-intake')
    assert design.status_code == 201
    design_payload = design.json()

    reviewed = client.post(
        f"/design-drafts/{design_payload['design_id']}/review",
        json={
            'resolved_inputs': {
                'dataset_name': 'titanic',
                'train_uri': 's3://datasets/titanic/all.csv',
                'test_uri': 's3://datasets/titanic/all.csv',
                'target_column': 'Survived',
            },
            'review_notes': ['No explicit validation split declared in this negative fixture.'],
        },
    )
    assert reviewed.status_code == 200
    store.save_research_session(
        store.get_research_session(session_id).model_copy(
            update={'latest_design_id': design_payload['design_id']}
        )
    )

    response = client.get(f"/research-sessions/{session_id}/execution-preflight")
    assert response.status_code == 200
    payload = response.json()
    assert any('train_uri and test_uri resolve to the same dataset path' in issue for issue in payload['blocking_issues'])
    assert any('declared validation split: 0.2' in warning for warning in payload['warnings'])


def test_declared_only_workflow_reports_not_executable() -> None:
    client = build_client()

    response = client.get('/workflow-families/replication-lite/execution-preflight')

    assert response.status_code == 200
    payload = response.json()
    assert payload['workflow_id'] == 'replication-lite'
    assert payload['execution_status'] == 'declared_only'
    assert payload['submission_backend'] == 'unimplemented'
    assert payload['ready'] is False
    assert any('execution_status is declared_only' in issue for issue in payload['blocking_issues'])
    assert any('submission_backend is unimplemented' in issue for issue in payload['blocking_issues'])


def test_gpu_workflow_execution_preflight_reports_gpu_contract() -> None:
    client = build_client()

    response = client.get('/workflow-families/gpu-experiment/execution-preflight')

    assert response.status_code == 200
    payload = response.json()
    assert payload['workflow_id'] == 'gpu-experiment'
    assert payload['resource_profile'] == 'gpu-small'
    assert payload['runner_image'] == 'ghcr.io/ccny-glasslab/glasslab-gpu-experiment-runner:0.1.7-local'
    assert payload['resource_requests'] == {'cpu': '2', 'memory': '4Gi', 'nvidia.com/gpu': '1'}
    assert payload['resource_limits'] == {'cpu': '4', 'memory': '8Gi', 'nvidia.com/gpu': '1'}
    assert payload['node_selector'] == {
        'glasslab.io/gpu-candidate': 'true',
        'glasslab.io/gpu-vendor': 'nvidia',
    }
    assert payload['execution_status'] == 'ready'
    assert payload['submission_backend'] == 'kubernetes'
    assert payload['runtime_requirements']['gpu'] is True
    assert 'computer_vision' in payload['runtime_requirements']['modalities']
    assert payload['ready'] is True
    assert any('preflight was skipped' in warning for warning in payload['warnings'])
    assert any('torch' in warning for warning in payload['warnings'])


def test_get_run_reflects_disk_artifacts_and_status(tmp_path) -> None:
    settings = Settings(
        registry_dir=str(REPO_ROOT / 'services' / 'workflow-registry' / 'definitions'),
        artifacts_mount_path=str(tmp_path),
    )
    registry = WorkflowRegistry(settings.registry_dir)
    store = InMemoryRunStore()
    client = TestClient(create_app(settings=settings, registry=registry, store=store))

    response = client.post(
        '/runs',
        json={
            'workflow_id': 'generic-tabular-benchmark',
            'objective': 'Benchmark approved models on Titanic.',
            'inputs': {
                'dataset_name': 'titanic',
                'train_uri': 's3://datasets/titanic/train.csv',
                'test_uri': 's3://datasets/titanic/test.csv',
                'target_column': 'Survived',
            },
            'models': ['logistic_regression', 'random_forest'],
        },
    )
    assert response.status_code == 201
    run_id = response.json()['run_id']

    run_dir = tmp_path / run_id
    (run_dir / 'logs').mkdir(parents=True)
    (run_dir / 'status.json').write_text(
        '{"run_id":"%s","status":"succeeded","updated_at":"2026-03-23T20:34:44Z","detail":"done"}' % run_id
    )
    (run_dir / 'artifacts_index.json').write_text(
        '{"run_id":"%s","artifacts":[{"name":"status.json","path":"artifacts/%s/status.json","media_type":"application/json","required":true}]}'
        % (run_id, run_id)
    )
    (run_dir / 'logs' / 'runner.log').write_text('2026-03-23 20:34:44,470 INFO glasslab.runner completed Titanic baseline run\n')

    run = client.get(f'/runs/{run_id}')
    assert run.status_code == 200
    assert run.json()['status']['status'] == 'succeeded'

    artifacts = client.get(f'/runs/{run_id}/artifacts')
    assert artifacts.status_code == 200
    assert artifacts.json()['artifacts']['artifacts'][0]['name'] == 'status.json'

    logs = client.get(f'/runs/{run_id}/logs')
    assert logs.status_code == 200
    assert logs.json()['logs'][0]['message'] == 'completed Titanic baseline run'


def test_autoresearch_campaign_happy_path(tmp_path) -> None:
    settings = Settings(
        registry_dir=str(REPO_ROOT / 'services' / 'workflow-registry' / 'definitions'),
        artifacts_mount_path=str(tmp_path),
    )
    registry = WorkflowRegistry(settings.registry_dir)
    store = InMemoryRunStore()
    client = TestClient(create_app(settings=settings, registry=registry, store=store))

    session = client.post(
        '/research-sessions',
        json={'goal_statement': 'Explore bounded methodology variants for a tabular benchmark session.'},
    )
    assert session.status_code == 201
    session_id = session.json()['session_id']

    intake = client.post(
        '/intakes',
        json={
            'raw_request': 'Benchmark bounded tabular baselines on Titanic and compare a small set of approved models.',
            'source_refs': ['https://example.org/titanic-benchmark-note'],
            'notes': ['Focus on approved tabular baselines and simple accuracy-driven comparison.'],
        },
    )
    assert intake.status_code == 201

    design = client.post('/design-drafts/from-latest-intake')
    assert design.status_code == 201
    design_payload = design.json()

    reviewed = client.post(
        f"/design-drafts/{design_payload['design_id']}/review",
        json={
            'resolved_inputs': {
                'dataset_name': 'titanic',
                'train_uri': 's3://datasets/titanic/train.csv',
                'test_uri': 's3://datasets/titanic/test.csv',
                'target_column': 'Survived',
            },
            'review_notes': ['Autoresearch seed inputs resolved for bounded validation.'],
        },
    )
    assert reviewed.status_code == 200
    assert reviewed.json()['status'] == 'ready_for_run'

    campaign = client.post(
        '/autoresearch/campaigns',
        json={
            'session_id': session_id,
            'source_design_id': design_payload['design_id'],
            'objective': 'Compare a small set of approved tabular methodology variants on Titanic.',
            'max_iterations': 2,
            'evaluator_contract': {
                'evaluator_type': 'titanic_tabular_v1',
                'primary_metric': {'name': 'accuracy', 'direction': 'maximize', 'minimum_effect': 0.01},
            },
        },
    )
    assert campaign.status_code == 201
    campaign_payload = campaign.json()
    campaign_id = campaign_payload['campaign_id']
    assert campaign_payload['source_design_id'] == design_payload['design_id']

    latest = client.get('/autoresearch/campaigns/latest')
    assert latest.status_code == 200
    assert latest.json()['campaign_id'] == campaign_id

    drafted = client.post(f'/autoresearch/campaigns/{campaign_id}/draft-initial-methodologies')
    assert drafted.status_code == 201
    drafted_payload = drafted.json()
    assert drafted_payload['campaign']['status'] == 'drafted'
    assert len(drafted_payload['methodology_drafts']) >= 1
    assert any(
        draft['declared_inputs'].get('validation_strategy')
        for draft in drafted_payload['methodology_drafts']
    )
    child_draft_id = drafted_payload['methodology_drafts'][0]['methodology_draft_id']

    launched = client.post(f'/autoresearch/campaigns/{campaign_id}/launch-next-iteration')
    assert launched.status_code == 201
    launched_payload = launched.json()
    run_id = launched_payload['run']['run_id']
    assert launched_payload['methodology_draft']['status'] == 'launched'
    assert launched_payload['run']['run_purpose'] == 'autoresearch-validation'
    assert launched_payload['iteration']['child_methodology_draft_id'] == child_draft_id

    run_dir = tmp_path / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / 'status.json').write_text(
        '{"run_id":"%s","status":"succeeded","updated_at":"2026-03-30T16:00:00Z","detail":"autoresearch iteration complete"}'
        % run_id
    )
    (run_dir / 'metrics.json').write_text('{"accuracy": 0.84, "loss": 0.41}')

    iterations = client.get(f'/autoresearch/campaigns/{campaign_id}/iterations')
    assert iterations.status_code == 200
    assert len(iterations.json()) == 1

    decision = client.post(f'/autoresearch/campaigns/{campaign_id}/decide-latest')
    assert decision.status_code == 201
    decision_payload = decision.json()
    assert decision_payload['decision']['decision_type'] == 'keep'
    assert decision_payload['iteration']['decision'] == 'keep'
    assert decision_payload['campaign']['current_best_methodology_draft_id'] == child_draft_id

    summary = client.get(f'/autoresearch/campaigns/{campaign_id}/summary')
    assert summary.status_code == 200
    summary_payload = summary.json()
    assert summary_payload['campaign']['campaign_id'] == campaign_id
    assert summary_payload['best_methodology_draft']['methodology_draft_id'] == child_draft_id
    assert summary_payload['latest_run']['run_id'] == run_id
    assert summary_payload['recommended_model'] == 'logistic_regression'
    assert summary_payload['model_comparison']
    assert summary_payload['model_comparison'][0]['candidate_models'] == ['logistic_regression']
    assert summary_payload['proposed_next_variants']

    comparison = client.get(f'/autoresearch/campaigns/{campaign_id}/model-comparison')
    assert comparison.status_code == 200
    comparison_payload = comparison.json()
    assert comparison_payload['recommended_model'] == 'logistic_regression'
    assert comparison_payload['model_comparison'][0]['decision'] == 'keep'


def test_autoresearch_decision_uses_best_metric_payload(tmp_path) -> None:
    settings = Settings(
        registry_dir=str(REPO_ROOT / 'services' / 'workflow-registry' / 'definitions'),
        artifacts_mount_path=str(tmp_path),
    )
    registry = WorkflowRegistry(settings.registry_dir)
    store = InMemoryRunStore()
    client = TestClient(create_app(settings=settings, registry=registry, store=store))

    session = client.post(
        '/research-sessions',
        json={'goal_statement': 'Explore bounded methodology variants for a GPU session.'},
    )
    assert session.status_code == 201
    session_id = session.json()['session_id']

    intake = client.post(
        '/intakes',
        json={
            'raw_request': 'Replicate DreamSim visual similarity metric with PyTorch and timm.',
            'source_refs': ['https://dreamsim-nights.github.io/'],
            'notes': ['Focus on approved GPU templates only.'],
            'technique_tags': ['dreamsim', 'pytorch', 'timm'],
        },
    )
    assert intake.status_code == 201

    interpretation = client.post('/interpretations/from-latest-intake')
    assert interpretation.status_code == 201

    design = client.post('/design-drafts/from-latest-intake')
    assert design.status_code == 201
    design_payload = design.json()
    reviewed = client.post(
        f"/design-drafts/{design_payload['design_id']}/review",
        json={
            'resolved_inputs': {
                'dataset_uri': 's3://datasets/dreamsim/train.csv',
                'model_family': 'lightweight replication',
                'training_notes': 'Replicate DreamSim visual similarity metric with PyTorch and timm.',
                'evaluation_target': 'embedding retrieval auc',
                'validation_strategy': 'stratified_holdout',
                'validation_split': '0.2',
            },
            'review_notes': ['Resolved GPU execution inputs for bounded autoresearch decision test.'],
        },
    )
    assert reviewed.status_code == 200
    design_payload = reviewed.json()
    assert design_payload['status'] == 'ready_for_run'

    campaign = client.post(
        '/autoresearch/campaigns',
        json={
            'session_id': session_id,
            'source_design_id': design_payload['design_id'],
            'objective': 'Compare approved GPU methodology variants.',
            'max_iterations': 2,
            'evaluator_contract': {
                'evaluator_type': 'gpu_method_validation_v1',
                'primary_metric': {'name': 'bounded_method_score', 'direction': 'maximize', 'minimum_effect': 0.01},
            },
        },
    )
    assert campaign.status_code == 201
    campaign_id = campaign.json()['campaign_id']

    drafted = client.post(f'/autoresearch/campaigns/{campaign_id}/draft-initial-methodologies')
    assert drafted.status_code == 201

    launched = client.post(f'/autoresearch/campaigns/{campaign_id}/launch-next-iteration')
    assert launched.status_code == 201
    run_id = launched.json()['run']['run_id']

    run_dir = tmp_path / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / 'status.json').write_text(
        '{"run_id":"%s","status":"succeeded","updated_at":"2026-04-04T01:00:00Z","detail":"gpu iteration complete"}'
        % run_id
    )
    (run_dir / 'metrics.json').write_text(
        '{"metric_name":"bounded_method_score","best_metric":0.9375,"required_python_packages":["torch","timm"]}'
    )

    decision = client.post(f'/autoresearch/campaigns/{campaign_id}/decide-latest')
    assert decision.status_code == 201
    decision_payload = decision.json()
    assert decision_payload['decision']['decision_type'] == 'keep'
    assert decision_payload['iteration']['score_summary']['primary_metric_name'] == 'bounded_method_score'
    assert decision_payload['iteration']['score_summary']['primary_metric_value'] == 0.9375


def test_autoresearch_contract_controls_metric_direction_and_selection() -> None:
    contract = EvaluatorContract(
        evaluator_type='loss_minimization_v1',
        primary_metric={'name': 'loss', 'direction': 'minimize', 'minimum_effect': 0.02},
        guardrails=[{'name': 'effective_rank', 'direction': 'maximize', 'minimum': 64.0, 'required': True}],
    )

    metric_name, metric_value, metric_source = autoresearch_module._extract_primary_metric(
        {'accuracy': 0.99, 'loss': 0.30, 'effective_rank': 80.0},
        contract,
    )
    assert metric_name == 'loss'
    assert metric_value == 0.30
    assert metric_source == 'evaluator_contract'

    missing_name, missing_value, missing_source = autoresearch_module._extract_primary_metric(
        {'accuracy': 0.99, 'effective_rank': 80.0},
        contract,
    )
    assert missing_name == 'loss'
    assert missing_value is None
    assert missing_source == 'evaluator_contract'

    child_summary = {
        'run_status': 'succeeded',
        'primary_metric_name': 'loss',
        'primary_metric_value': 0.25,
        'primary_metric_direction': 'minimize',
        'guardrails': [{'metric_name': 'effective_rank', 'passed': True}],
    }
    parent_summary = {
        'run_status': 'succeeded',
        'primary_metric_name': 'loss',
        'primary_metric_value': 0.30,
        'primary_metric_direction': 'minimize',
    }
    comparison = autoresearch_module.build_iteration_comparison(child_summary, parent_summary, evaluator_contract=contract)
    assert comparison['delta'] == -0.05
    assert comparison['normalized_delta'] == 0.05

    iteration = AutoresearchIterationRecord(
        iteration_id='iter-loss',
        campaign_id='campaign-loss',
        child_methodology_draft_id='method-loss',
        run_id='run-loss',
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        status='completed',
    )
    decision_type, rationale = autoresearch_module.build_decision(
        iteration,
        child_summary,
        comparison,
        evaluator_contract=contract,
    )
    assert decision_type == 'keep'
    assert 'minimize contract' in rationale


def test_autoresearch_decision_recomputes_stale_escalation(tmp_path) -> None:
    settings = Settings(
        registry_dir=str(REPO_ROOT / 'services' / 'workflow-registry' / 'definitions'),
        artifacts_mount_path=str(tmp_path),
    )
    registry = WorkflowRegistry(settings.registry_dir)
    store = InMemoryRunStore()
    client = TestClient(create_app(settings=settings, registry=registry, store=store))

    session = client.post(
        '/research-sessions',
        json={'goal_statement': 'Explore bounded methodology variants for a GPU session.'},
    )
    assert session.status_code == 201
    session_id = session.json()['session_id']

    intake = client.post(
        '/intakes',
        json={
            'raw_request': 'Replicate DreamSim visual similarity metric with PyTorch and timm.',
            'source_refs': ['https://dreamsim-nights.github.io/'],
            'notes': ['Focus on approved GPU templates only.'],
            'technique_tags': ['dreamsim', 'pytorch', 'timm'],
        },
    )
    assert intake.status_code == 201
    assert client.post('/interpretations/from-latest-intake').status_code == 201

    design = client.post('/design-drafts/from-latest-intake')
    assert design.status_code == 201
    design_payload = design.json()
    reviewed = client.post(
        f"/design-drafts/{design_payload['design_id']}/review",
        json={
            'resolved_inputs': {
                'dataset_uri': 's3://datasets/dreamsim/train.csv',
                'model_family': 'lightweight replication',
                'training_notes': 'Replicate DreamSim visual similarity metric with PyTorch and timm.',
                'evaluation_target': 'embedding retrieval auc',
                'validation_strategy': 'stratified_holdout',
                'validation_split': '0.2',
            },
            'review_notes': ['Resolved GPU execution inputs for stale escalation test.'],
        },
    )
    assert reviewed.status_code == 200

    campaign = client.post(
        '/autoresearch/campaigns',
        json={
            'session_id': session_id,
            'source_design_id': design_payload['design_id'],
            'objective': 'Compare approved GPU methodology variants.',
            'max_iterations': 2,
            'evaluator_contract': {
                'evaluator_type': 'gpu_method_validation_v1',
                'primary_metric': {'name': 'bounded_method_score', 'direction': 'maximize', 'minimum_effect': 0.01},
            },
        },
    )
    assert campaign.status_code == 201
    campaign_id = campaign.json()['campaign_id']

    drafted = client.post(f'/autoresearch/campaigns/{campaign_id}/draft-initial-methodologies')
    assert drafted.status_code == 201
    launched = client.post(f'/autoresearch/campaigns/{campaign_id}/launch-next-iteration')
    assert launched.status_code == 201
    run_id = launched.json()['run']['run_id']

    first_decision = client.post(f'/autoresearch/campaigns/{campaign_id}/decide-latest')
    assert first_decision.status_code == 201
    assert first_decision.json()['decision']['decision_type'] == 'escalate_for_review'

    run_dir = tmp_path / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / 'status.json').write_text(
        '{"run_id":"%s","status":"succeeded","updated_at":"2026-04-04T01:00:00Z","detail":"gpu iteration complete"}'
        % run_id
    )
    (run_dir / 'metrics.json').write_text(
        '{"metric_name":"bounded_method_score","best_metric":0.9375}'
    )

    second_decision = client.post(f'/autoresearch/campaigns/{campaign_id}/decide-latest')
    assert second_decision.status_code == 201
    second_payload = second_decision.json()
    assert second_payload['decision']['decision_type'] == 'keep'
    assert second_payload['campaign']['status'] == 'completed'


def test_autoresearch_launch_next_iteration_synthesizes_follow_on_from_guidance(tmp_path) -> None:
    settings = Settings(
        registry_dir=str(REPO_ROOT / 'services' / 'workflow-registry' / 'definitions'),
        artifacts_mount_path=str(tmp_path),
    )
    registry = WorkflowRegistry(settings.registry_dir)
    store = InMemoryRunStore()
    client = TestClient(create_app(settings=settings, registry=registry, store=store))

    imported = client.post(
        '/technique-catalog/import',
        json={
            'cards': [
                {
                    'name': 'DreamSim Transformer Similarity',
                    'aliases': ['dreamsim', 'visual similarity metric'],
                    'problem_types': ['multiclass_classification'],
                    'algorithm_family': 'transformers',
                    'specific_algorithms': ['vision_transformer', 'clip'],
                    'validation_strategies': ['holdout'],
                    'primary_metrics': ['embedding_retrieval_auc'],
                    'loss_functions': ['contrastive_loss'],
                    'python_packages': ['torch', 'timm'],
                    'gpu_required': True,
                    'resource_profile': 'gpu-small',
                    'workflow_ids': ['gpu-experiment'],
                }
            ]
        },
    )
    assert imported.status_code == 201

    session = client.post(
        '/research-sessions',
        json={'goal_statement': 'Explore bounded GPU methodology variants and auto-launch follow-ons.'},
    )
    assert session.status_code == 201
    session_id = session.json()['session_id']

    intake = client.post(
        '/intakes',
        json={
            'raw_request': 'Replicate DreamSim visual similarity metric with PyTorch and timm.',
            'source_refs': ['https://dreamsim-nights.github.io/'],
            'notes': ['Focus on approved GPU templates only.'],
            'technique_tags': ['dreamsim', 'pytorch', 'timm'],
        },
    )
    assert intake.status_code == 201
    assert client.post('/interpretations/from-latest-intake').status_code == 201

    design = client.post('/design-drafts/from-latest-intake')
    assert design.status_code == 201
    design_payload = design.json()
    reviewed = client.post(
        f"/design-drafts/{design_payload['design_id']}/review",
        json={
            'resolved_inputs': {
                'dataset_uri': 's3://datasets/dreamsim/train.csv',
                'model_family': 'lightweight replication',
                'training_notes': 'Replicate DreamSim visual similarity metric with PyTorch and timm.',
                'evaluation_target': 'embedding retrieval auc',
                'validation_strategy': 'holdout',
                'validation_split': '0.2',
            },
            'review_notes': ['Resolved GPU execution inputs for synthesized follow-on launch test.'],
        },
    )
    assert reviewed.status_code == 200

    campaign = client.post(
        '/autoresearch/campaigns',
        json={
            'session_id': session_id,
            'source_design_id': design_payload['design_id'],
            'objective': 'Compare approved GPU methodology variants.',
            'max_iterations': 4,
        },
    )
    assert campaign.status_code == 201
    campaign_id = campaign.json()['campaign_id']

    drafted = client.post(f'/autoresearch/campaigns/{campaign_id}/draft-initial-methodologies')
    assert drafted.status_code == 201

    all_drafts = store.list_methodology_drafts(campaign_id)
    kept_draft = next(record for record in all_drafts if record.candidate_models == ['vision_transformer', 'clip'])
    discarded_draft = next(record for record in all_drafts if record.candidate_models == ['pytorch-template-v1'])

    for draft in all_drafts:
        if draft.methodology_draft_id == kept_draft.methodology_draft_id:
            store.save_methodology_draft(draft.model_copy(update={'status': 'kept'}))
        elif draft.status == 'ready_for_execution':
            store.save_methodology_draft(draft.model_copy(update={'status': 'discarded'}))

    kept_iteration = AutoresearchIterationRecord(
        iteration_id='iter-keep',
        campaign_id=campaign_id,
        parent_methodology_draft_id=kept_draft.parent_methodology_draft_id,
        child_methodology_draft_id=kept_draft.methodology_draft_id,
        run_id='run-keep',
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        status='decided',
        score_summary={
            'run_status': 'succeeded',
            'run_detail': 'completed',
            'primary_metric_name': 'bounded_method_score',
            'primary_metric_value': 0.9688,
            'metrics': {
                'best_model': 'vision_transformer',
                'technique_components': {
                    'candidate_contract': 1.0,
                    'metric_contract': 1.0,
                    'objective_contract': 1.0,
                    'task_contract': 1.0,
                },
                'readiness_components': {
                    'package_stack': 1.0,
                    'runtime_stack': 0.75,
                    'split_contract': 1.0,
                    'target_alignment': 1.0,
                },
            },
        },
        comparison_summary={'baseline_available': False, 'metric_name': 'bounded_method_score'},
        decision='keep',
    )
    discarded_iteration = AutoresearchIterationRecord(
        iteration_id='iter-discard',
        campaign_id=campaign_id,
        parent_methodology_draft_id=discarded_draft.parent_methodology_draft_id,
        child_methodology_draft_id=discarded_draft.methodology_draft_id,
        run_id='run-discard',
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        status='decided',
        score_summary={
            'run_status': 'succeeded',
            'run_detail': 'completed',
            'primary_metric_name': 'bounded_method_score',
            'primary_metric_value': 0.9062,
            'metrics': {
                'best_model': 'pytorch-template-v1',
                'technique_components': {
                    'candidate_contract': 1.0,
                    'metric_contract': 1.0,
                    'objective_contract': 0.5,
                    'task_contract': 1.0,
                },
                'readiness_components': {
                    'package_stack': 1.0,
                    'runtime_stack': 0.75,
                    'split_contract': 1.0,
                    'target_alignment': 1.0,
                },
            },
        },
        comparison_summary={'baseline_available': True, 'metric_name': 'bounded_method_score', 'delta': -0.0626},
        decision='discard',
    )
    store.save_autoresearch_iteration(kept_iteration)
    store.save_autoresearch_iteration(discarded_iteration)
    store.save_autoresearch_decision(
        AutoresearchDecisionRecord(
            decision_id='decision-keep',
            campaign_id=campaign_id,
            iteration_id=kept_iteration.iteration_id,
            created_at=datetime.now(timezone.utc),
            decision_type='keep',
            rationale='Kept for higher bounded_method_score.',
            evidence_refs=['run:run-keep'],
        )
    )
    store.save_autoresearch_decision(
        AutoresearchDecisionRecord(
            decision_id='decision-discard',
            campaign_id=campaign_id,
            iteration_id=discarded_iteration.iteration_id,
            created_at=datetime.now(timezone.utc),
            decision_type='discard',
            rationale='Discarded for lower bounded_method_score.',
            evidence_refs=['run:run-discard'],
        )
    )
    store.save_autoresearch_campaign(
        store.get_autoresearch_campaign(campaign_id).model_copy(
            update={
                'current_best_methodology_draft_id': kept_draft.methodology_draft_id,
                'latest_iteration_id': discarded_iteration.iteration_id,
                'latest_decision_id': 'decision-discard',
                'status': 'completed',
            }
        )
    )

    launched = client.post(f'/autoresearch/campaigns/{campaign_id}/launch-next-iteration')
    assert launched.status_code == 201
    payload = launched.json()
    launched_draft = payload['methodology_draft']
    assert launched_draft['parent_methodology_draft_id'] == kept_draft.methodology_draft_id
    assert launched_draft['mutation_diff']['auto_follow_on']['mutation_axis'] == 'baseline_models'
    assert launched_draft['candidate_models'] == ['vision_transformer', 'clip', 'pytorch-template-v1']
    assert payload['iteration']['run_id']


def test_session_autoresearch_transition_chain(tmp_path) -> None:
    settings = Settings(
        registry_dir=str(REPO_ROOT / 'services' / 'workflow-registry' / 'definitions'),
        artifacts_mount_path=str(tmp_path),
    )
    registry = WorkflowRegistry(settings.registry_dir)
    store = InMemoryRunStore()
    client = TestClient(create_app(settings=settings, registry=registry, store=store))

    session = client.post(
        '/research-sessions',
        json={'goal_statement': 'Turn the current literature-backed tabular idea into a bounded validation loop.'},
    )
    assert session.status_code == 201
    session_id = session.json()['session_id']

    intake = client.post(
        '/intakes',
        json={
            'raw_request': 'Benchmark a bounded set of approved Titanic baselines and compare one small methodology variant.',
            'source_refs': ['https://example.org/titanic-method-note'],
            'notes': ['Keep the first autoresearch transition path narrow and reviewable.'],
        },
    )
    assert intake.status_code == 201

    design = client.post('/design-drafts/from-latest-intake')
    assert design.status_code == 201
    design_payload = design.json()

    reviewed = client.post(
        f"/design-drafts/{design_payload['design_id']}/review",
        json={
            'resolved_inputs': {
                'dataset_name': 'titanic',
                'train_uri': 's3://datasets/titanic/train.csv',
                'test_uri': 's3://datasets/titanic/test.csv',
                'target_column': 'Survived',
            },
            'review_notes': ['Session transition design review complete.'],
        },
    )
    assert reviewed.status_code == 200

    campaign = client.post(f'/research-sessions/{session_id}/transitions/start-autoresearch-campaign')
    assert campaign.status_code == 201

    drafted = client.post(f'/research-sessions/{session_id}/transitions/draft-methodologies')
    assert drafted.status_code == 201
    assert drafted.json()['methodology_drafts']

    launched = client.post(f'/research-sessions/{session_id}/transitions/launch-autoresearch-iteration')
    assert launched.status_code == 201
    run_id = launched.json()['run']['run_id']

    run_dir = tmp_path / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / 'status.json').write_text(
        '{"run_id":"%s","status":"succeeded","updated_at":"2026-03-30T17:00:00Z","detail":"session transition iteration complete"}'
        % run_id
    )
    (run_dir / 'metrics.json').write_text('{"accuracy": 0.79}')

    decided = client.post(f'/research-sessions/{session_id}/transitions/decide-autoresearch-latest')
    assert decided.status_code == 201
    assert decided.json()['decision']['decision_type'] == 'keep'

    summary = client.get(f'/research-sessions/{session_id}/autoresearch-summary')
    assert summary.status_code == 200
    assert summary.json()['campaign']['session_id'] == session_id


def test_transition_advance_autoresearch_bootstraps_campaign_and_launches_batch(tmp_path) -> None:
    settings = Settings(
        registry_dir=str(REPO_ROOT / 'services' / 'workflow-registry' / 'definitions'),
        artifacts_mount_path=str(tmp_path),
    )
    registry = WorkflowRegistry(settings.registry_dir)
    store = InMemoryRunStore()
    client = TestClient(create_app(settings=settings, registry=registry, store=store))

    session = client.post(
        '/research-sessions',
        json={'goal_statement': 'Turn the current literature-backed tabular idea into a bounded validation loop.'},
    )
    assert session.status_code == 201
    session_id = session.json()['session_id']

    intake = client.post(
        '/intakes',
        json={
            'raw_request': 'Benchmark a bounded set of approved Titanic baselines and compare one small methodology variant.',
            'source_refs': ['https://example.org/titanic-method-note'],
            'notes': ['Keep the happy-path autoresearch transition narrow and deterministic.'],
        },
    )
    assert intake.status_code == 201

    design = client.post('/design-drafts/from-latest-intake')
    assert design.status_code == 201
    design_payload = design.json()

    reviewed = client.post(
        f"/design-drafts/{design_payload['design_id']}/review",
        json={
            'resolved_inputs': {
                'dataset_name': 'titanic',
                'train_uri': 's3://datasets/titanic/train.csv',
                'test_uri': 's3://datasets/titanic/test.csv',
                'target_column': 'Survived',
            },
            'review_notes': ['Advance-autoresearch happy-path review complete.'],
        },
    )
    assert reviewed.status_code == 200

    advanced = client.post(f'/research-sessions/{session_id}/transitions/advance-autoresearch')
    assert advanced.status_code == 201
    payload = advanced.json()
    assert payload['session']['session_id'] == session_id
    assert payload['campaign']['session_id'] == session_id
    assert payload['drafted_methodology_count'] >= 1
    assert payload['launches_started'] >= 1
    assert payload['launch']['launches']


def test_autoresearch_launch_iteration_uses_method_spec_inputs(tmp_path) -> None:
    settings = Settings(
        registry_dir=str(REPO_ROOT / 'services' / 'workflow-registry' / 'definitions'),
        artifacts_mount_path=str(tmp_path),
    )
    registry = WorkflowRegistry(settings.registry_dir)
    store = InMemoryRunStore()
    client = TestClient(create_app(settings=settings, registry=registry, store=store))

    session = client.post(
        '/research-sessions',
        json={'goal_statement': 'Validate literature-derived launch iteration via bounded method spec.'},
    )
    session_id = session.json()['session_id']

    intake = client.post(
        '/intakes',
        json={
            'raw_request': 'Bounded literature-derived experiment using s3://datasets/paper-derived/train.csv for validation.',
            'source_refs': ['https://example.org/paper-derived-method'],
            'notes': ['Keep the methodology executable within approved templates.'],
        },
    )
    assert intake.status_code == 201

    interpretation = client.post('/interpretations/from-latest-intake')
    assert interpretation.status_code == 201
    design = client.post('/design-drafts/from-latest-intake')
    assert design.status_code == 201
    design_payload = design.json()
    assert design_payload['status'] == 'ready_for_run'

    store.save_research_session(
        store.get_research_session(session_id).model_copy(update={'latest_design_id': design_payload['design_id']})
    )

    campaign = client.post(f'/research-sessions/{session_id}/transitions/start-autoresearch-campaign')
    assert campaign.status_code == 201
    drafted = client.post(f'/research-sessions/{session_id}/transitions/draft-methodologies')
    assert drafted.status_code == 201

    launched = client.post(f'/research-sessions/{session_id}/transitions/launch-autoresearch-iteration')
    assert launched.status_code == 201
    payload = launched.json()
    assert payload['run']['workflow_id'] == 'generic-tabular-benchmark'
    assert payload['run']['manifest']['inputs']['train_uri'] == 's3://datasets/paper-derived/train.csv'
    assert payload['run']['manifest']['inputs']['validation_strategy'] == 'holdout'


def test_autoresearch_launch_batch_returns_multiple_runs(tmp_path) -> None:
    settings = Settings(
        registry_dir=str(REPO_ROOT / 'services' / 'workflow-registry' / 'definitions'),
        artifacts_mount_path=str(tmp_path),
    )
    registry = WorkflowRegistry(settings.registry_dir)
    store = InMemoryRunStore()
    client = TestClient(create_app(settings=settings, registry=registry, store=store))

    session = client.post(
        '/research-sessions',
        json={'goal_statement': 'Launch a bounded batch of methodology variants in parallel.'},
    )
    assert session.status_code == 201
    session_id = session.json()['session_id']

    intake = client.post(
        '/intakes',
        json={
            'raw_request': 'Benchmark a bounded set of approved Titanic baselines and compare several small methodology variants.',
            'source_refs': ['https://example.org/titanic-batch-note'],
            'notes': ['Keep the batch launch deterministic and reviewable.'],
        },
    )
    assert intake.status_code == 201

    design = client.post('/design-drafts/from-latest-intake')
    assert design.status_code == 201
    design_payload = design.json()

    reviewed = client.post(
        f"/design-drafts/{design_payload['design_id']}/review",
        json={
            'resolved_inputs': {
                'dataset_name': 'titanic',
                'train_uri': 's3://datasets/titanic/train.csv',
                'test_uri': 's3://datasets/titanic/test.csv',
                'target_column': 'Survived',
            },
            'review_notes': ['Autoresearch batch launch fixture is ready for bounded validation.'],
        },
    )
    assert reviewed.status_code == 200

    campaign = client.post(f'/research-sessions/{session_id}/transitions/start-autoresearch-campaign')
    assert campaign.status_code == 201
    drafted = client.post(f'/research-sessions/{session_id}/transitions/draft-methodologies')
    assert drafted.status_code == 201

    launched = client.post(f'/research-sessions/{session_id}/transitions/launch-autoresearch-batch')
    assert launched.status_code == 201
    payload = launched.json()
    launches = payload['launches']
    assert len(launches) == 2
    assert payload['campaign']['status'] == 'active'
    assert all(item['run']['workflow_id'] == 'generic-tabular-benchmark' for item in launches)
    assert all(item['iteration']['status'] == 'launched' for item in launches)


def test_autoresearch_decide_ready_batch_records_multiple_decisions(tmp_path) -> None:
    settings = Settings(
        registry_dir=str(REPO_ROOT / 'services' / 'workflow-registry' / 'definitions'),
        artifacts_mount_path=str(tmp_path),
    )
    registry = WorkflowRegistry(settings.registry_dir)
    store = InMemoryRunStore()
    client = TestClient(create_app(settings=settings, registry=registry, store=store))

    session = client.post(
        '/research-sessions',
        json={'goal_statement': 'Decide a bounded completed batch of methodology variants.'},
    )
    assert session.status_code == 201
    session_id = session.json()['session_id']

    intake = client.post(
        '/intakes',
        json={
            'raw_request': 'Benchmark a bounded set of approved Titanic baselines and compare several small methodology variants.',
            'source_refs': ['https://example.org/titanic-batch-decision-note'],
            'notes': ['Record decisions for all ready iterations in one pass.'],
        },
    )
    assert intake.status_code == 201

    design = client.post('/design-drafts/from-latest-intake')
    assert design.status_code == 201
    design_payload = design.json()

    reviewed = client.post(
        f"/design-drafts/{design_payload['design_id']}/review",
        json={
            'resolved_inputs': {
                'dataset_name': 'titanic',
                'train_uri': 's3://datasets/titanic/train.csv',
                'test_uri': 's3://datasets/titanic/test.csv',
                'target_column': 'Survived',
            },
            'review_notes': ['Autoresearch batch decision fixture is ready for bounded validation.'],
        },
    )
    assert reviewed.status_code == 200

    campaign = client.post(f'/research-sessions/{session_id}/transitions/start-autoresearch-campaign')
    assert campaign.status_code == 201
    drafted = client.post(f'/research-sessions/{session_id}/transitions/draft-methodologies')
    assert drafted.status_code == 201

    launched = client.post(f'/research-sessions/{session_id}/transitions/launch-autoresearch-batch')
    assert launched.status_code == 201
    launches = launched.json()['launches']
    assert len(launches) == 2

    for index, item in enumerate(launches):
        run_id = item['run']['run_id']
        score = 0.6 + (0.1 * index)
        run_dir = tmp_path / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / 'status.json').write_text(
            '{"run_id":"%s","status":"succeeded","updated_at":"2026-04-04T02:00:00Z","detail":"batch iteration complete"}'
            % run_id
        )
        (run_dir / 'metrics.json').write_text(
            '{"metric_name":"bounded_method_score","best_metric":%s}' % score
        )

    decided = client.post(f'/research-sessions/{session_id}/transitions/decide-autoresearch-batch')
    assert decided.status_code == 201
    payload = decided.json()
    assert len(payload['decisions']) == 2
    assert len(payload['iterations']) == 2
    assert payload['campaign']['latest_decision_id'] == payload['decisions'][-1]['decision_id']
    assert all(item['decision_type'] in {'keep', 'discard', 'escalate_for_review'} for item in payload['decisions'])


def test_autoresearch_summary_refreshes_completed_iteration_without_decide(tmp_path) -> None:
    settings = Settings(
        registry_dir=str(REPO_ROOT / 'services' / 'workflow-registry' / 'definitions'),
        artifacts_mount_path=str(tmp_path),
    )
    registry = WorkflowRegistry(settings.registry_dir)
    store = InMemoryRunStore()
    client = TestClient(create_app(settings=settings, registry=registry, store=store))

    session = client.post(
        '/research-sessions',
        json={'goal_statement': 'Refresh completed autoresearch runs into summary output without waiting for a decision.'},
    )
    assert session.status_code == 201
    session_id = session.json()['session_id']

    intake = client.post(
        '/intakes',
        json={
            'raw_request': 'Benchmark a bounded set of approved Titanic baselines and compare one small methodology variant.',
            'source_refs': ['https://example.org/titanic-summary-note'],
            'notes': ['Keep this fixture narrow and deterministic.'],
        },
    )
    assert intake.status_code == 201

    design = client.post('/design-drafts/from-latest-intake')
    assert design.status_code == 201
    design_payload = design.json()

    reviewed = client.post(
        f"/design-drafts/{design_payload['design_id']}/review",
        json={
            'resolved_inputs': {
                'dataset_name': 'titanic',
                'train_uri': 's3://datasets/titanic/train.csv',
                'test_uri': 's3://datasets/titanic/test.csv',
                'target_column': 'Survived',
            },
            'review_notes': ['Autoresearch summary refresh fixture is ready for bounded validation.'],
        },
    )
    assert reviewed.status_code == 200

    campaign = client.post(
        '/autoresearch/campaigns',
        json={
            'session_id': session_id,
            'source_design_id': design_payload['design_id'],
            'objective': 'Compare a small set of approved tabular methodology variants on Titanic.',
            'max_iterations': 2,
        },
    )
    assert campaign.status_code == 201
    campaign_id = campaign.json()['campaign_id']

    drafted = client.post(f'/autoresearch/campaigns/{campaign_id}/draft-initial-methodologies')
    assert drafted.status_code == 201

    launched = client.post(f'/autoresearch/campaigns/{campaign_id}/launch-next-iteration')
    assert launched.status_code == 201
    run_id = launched.json()['run']['run_id']
    iteration_id = launched.json()['iteration']['iteration_id']

    run_dir = tmp_path / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / 'status.json').write_text(
        '{"run_id":"%s","status":"succeeded","updated_at":"2026-04-04T01:00:00Z","detail":"autoresearch iteration complete"}'
        % run_id
    )
    (run_dir / 'metrics.json').write_text(
        '{"accuracy": 0.91, "loss": 0.22, "best_model": "logistic_regression",'
        ' "technique_components": {"objective_contract": 0.5, "metric_contract": 1.0}}'
    )

    summary = client.get(f'/autoresearch/campaigns/{campaign_id}/summary')
    assert summary.status_code == 200
    payload = summary.json()
    assert payload['iterations'][0]['iteration_id'] == iteration_id
    assert payload['iterations'][0]['status'] == 'completed'
    assert payload['iterations'][0]['score_summary']['run_status'] == 'succeeded'
    assert payload['iterations'][0]['score_summary']['primary_metric_name'] == 'accuracy'
    assert payload['iterations'][0]['score_summary']['primary_metric_value'] == 0.91
    assert payload['proposed_next_variants'][0] == 'Run an explicit objective/loss variant and compare it against the current winner.'
    assert payload['proposed_next_mutations'][0]['source_component'] == 'objective_contract'
    assert payload['proposed_next_mutations'][0]['mutation_axis'] == 'loss_or_distance'
    assert payload['proposed_next_mutations'][0]['suggested_updates']['loss_or_distance'] == 'alternate-approved-objective'

    comparison = client.get(f'/autoresearch/campaigns/{campaign_id}/model-comparison')
    assert comparison.status_code == 200
    comparison_payload = comparison.json()
    assert comparison_payload['model_comparison'][0]['run_id'] == run_id
    assert comparison_payload['model_comparison'][0]['best_model'] == 'logistic_regression'
    assert comparison_payload['model_comparison'][0]['primary_metric_name'] == 'accuracy'
    assert comparison_payload['model_comparison'][0]['primary_metric_value'] == 0.91
    assert comparison_payload['model_comparison'][0]['technique_components']['objective_contract'] == 0.5
    assert comparison_payload['recommended_model'] == 'logistic_regression'


def test_autoresearch_notebook_draft_is_written(tmp_path) -> None:
    settings = Settings(
        registry_dir=str(REPO_ROOT / 'services' / 'workflow-registry' / 'definitions'),
        artifacts_mount_path=str(tmp_path),
    )
    registry = WorkflowRegistry(settings.registry_dir)
    store = InMemoryRunStore()
    client = TestClient(create_app(settings=settings, registry=registry, store=store))

    session = client.post(
        '/research-sessions',
        json={'goal_statement': 'Draft a notebook scaffold for a bounded autoresearch methodology variant.'},
    )
    session_id = session.json()['session_id']

    client.post(
        '/intakes',
        json={
            'raw_request': 'Benchmark one bounded Titanic variant and expose the review path in a notebook.',
            'source_refs': ['https://example.org/titanic-notebook-note'],
            'notes': ['Notebook draft should stay tied to the methodology record.'],
        },
    )
    design = client.post('/design-drafts/from-latest-intake').json()
    client.post(
        f"/design-drafts/{design['design_id']}/review",
        json={
            'resolved_inputs': {
                'dataset_name': 'titanic',
                'train_uri': 's3://datasets/titanic/train.csv',
                'test_uri': 's3://datasets/titanic/test.csv',
                'target_column': 'Survived',
            },
            'review_notes': ['Notebook draft test review.'],
        },
    )
    client.post(f'/research-sessions/{session_id}/transitions/start-autoresearch-campaign')
    client.post(f'/research-sessions/{session_id}/transitions/draft-methodologies')

    notebook = client.post(f'/research-sessions/{session_id}/transitions/draft-autoresearch-notebook')
    assert notebook.status_code == 201
    payload = notebook.json()
    assert payload['storage_uri'].endswith('/analysis_notebook.ipynb')
    assert payload['notebook']['nbformat'] == 4
    assert payload['methodology_draft']['workflow_id'] == 'generic-tabular-benchmark'

    path = Path(payload['storage_uri'].removeprefix('file://'))
    assert path.exists()


def test_register_dataset_and_attach_to_session_context() -> None:
    client = build_client()

    session = client.post(
        '/research-sessions',
        json={'goal_statement': 'Train an artist similarity metric on a registered image dataset.'},
    )
    assert session.status_code == 201
    session_id = session.json()['session_id']

    dataset = client.post(
        f'/research-sessions/{session_id}/datasets',
        json={
            'uri': 'https://metmuseum.github.io/',
            'name': 'Met Open Access',
            'modality': 'image',
            'task_type': 'artist_similarity',
            'label_field': 'artist_id',
            'image_field': 'image_uri',
            'split_strategy': 'artist_grouped_holdout',
            'provenance_notes': ['Museum open-access collection metadata.'],
        },
    )
    assert dataset.status_code == 201
    dataset_payload = dataset.json()
    assert dataset_payload['name'] == 'Met Open Access'

    context = client.get(f'/research-sessions/{session_id}/context')
    assert context.status_code == 200
    context_payload = context.json()
    assert context_payload['active_dataset']['dataset_id'] == dataset_payload['dataset_id']
    assert context_payload['active_dataset']['uri'] == 'https://metmuseum.github.io/'
    assert context_payload['datasets'][0]['dataset_id'] == dataset_payload['dataset_id']


def test_session_dataset_bootstraps_design_and_run() -> None:
    client = build_client()

    session = client.post(
        '/research-sessions',
        json={'goal_statement': 'Develop an artist similarity metric from attached sources and a selected dataset.'},
    )
    assert session.status_code == 201
    session_id = session.json()['session_id']

    dataset = client.post(
        f'/research-sessions/{session_id}/datasets',
        json={
            'uri': 'https://www.wikiart.org/',
            'name': 'WikiArt',
            'modality': 'image',
            'task_type': 'artist_similarity',
            'label_field': 'artist_id',
            'image_field': 'image_uri',
            'split_strategy': 'artist_grouped_holdout',
        },
    )
    assert dataset.status_code == 201

    design = client.post(f'/research-sessions/{session_id}/skills/design')
    assert design.status_code == 201
    design_payload = design.json()
    assert design_payload['declared_inputs']['train_uri'] == 'https://www.wikiart.org/'
    assert 'dataset_uri is unresolved' not in ' '.join(design_payload['design_notes'])
    assert 'dataset_uri is still unresolved' not in ' '.join(design_payload['method_spec']['blocking_reasons'])

    run = client.post(f'/research-sessions/{session_id}/runs/from-design')
    assert run.status_code == 201
    assert run.json()['manifest']['inputs']['train_uri'] == 'https://www.wikiart.org/'


def test_explicit_session_dataset_overrides_builtin_titanic_fixture() -> None:
    client = build_client()

    session = client.post(
        '/research-sessions',
        json={'goal_statement': 'Run a bounded Titanic survival benchmark from an attached CSV dataset.'},
    )
    assert session.status_code == 201
    session_id = session.json()['session_id']

    dataset = client.post(
        f'/research-sessions/{session_id}/datasets',
        json={
            'uri': 'https://example.com/titanic-train.csv',
            'name': 'titanic-train.csv',
            'modality': 'tabular',
            'task_type': 'binary_classification',
        },
    )
    assert dataset.status_code == 201

    design = client.post(f'/research-sessions/{session_id}/skills/design')
    assert design.status_code == 201
    design_payload = design.json()
    assert design_payload['workflow_id'] == 'generic-tabular-benchmark'
    assert design_payload['declared_inputs']['train_uri'] == 'https://example.com/titanic-train.csv'
    assert design_payload['declared_inputs']['test_uri'] == 'https://example.com/titanic-train.csv'
    assert design_payload['declared_inputs']['dataset_name'] == 'titanic-train.csv'
    assert design_payload['declared_inputs']['target_column'] == 'Survived'


def test_autoresearch_notebook_refinement_falls_back_cleanly(tmp_path) -> None:
    settings = Settings(
        registry_dir=str(REPO_ROOT / 'services' / 'workflow-registry' / 'definitions'),
        artifacts_mount_path=str(tmp_path),
        coding_notebook_agent_enabled=False,
    )
    registry = WorkflowRegistry(settings.registry_dir)
    store = InMemoryRunStore()
    client = TestClient(create_app(settings=settings, registry=registry, store=store))

    session = client.post(
        '/research-sessions',
        json={'goal_statement': 'Refine a bounded autoresearch notebook through the coding-model fallback spine.'},
    )
    session_id = session.json()['session_id']

    client.post(
        '/intakes',
        json={
            'raw_request': 'Benchmark one bounded Titanic variant and capture the executable review path.',
            'source_refs': ['https://example.org/titanic-refine-note'],
            'notes': ['Notebook refinement should remain bounded and reviewable.'],
        },
    )
    design = client.post('/design-drafts/from-latest-intake').json()
    client.post(
        f"/design-drafts/{design['design_id']}/review",
        json={
            'resolved_inputs': {
                'dataset_name': 'titanic',
                'train_uri': 's3://datasets/titanic/train.csv',
                'test_uri': 's3://datasets/titanic/test.csv',
                'target_column': 'Survived',
            },
            'review_notes': ['Notebook refinement fallback test review.'],
        },
    )
    client.post(f'/research-sessions/{session_id}/transitions/start-autoresearch-campaign')
    client.post(f'/research-sessions/{session_id}/transitions/draft-methodologies')

    refined = client.post(f'/research-sessions/{session_id}/transitions/refine-autoresearch-notebook')
    assert refined.status_code == 201
    payload = refined.json()
    assert payload['refinement_source'] == 'deterministic'
    assert payload['warnings']
    assert payload['storage_uri'].endswith('/analysis_notebook.ipynb')


def test_autoresearch_notebook_refinement_uses_coding_model_response(tmp_path, monkeypatch) -> None:
    settings = Settings(
        registry_dir=str(REPO_ROOT / 'services' / 'workflow-registry' / 'definitions'),
        artifacts_mount_path=str(tmp_path),
        coding_notebook_agent_enabled=True,
        coding_notebook_agent_url='http://example.invalid/api/chat',
        coding_notebook_model='qwen2.5-coder:14b',
    )
    registry = WorkflowRegistry(settings.registry_dir)
    store = InMemoryRunStore()
    client = TestClient(create_app(settings=settings, registry=registry, store=store))

    session = client.post(
        '/research-sessions',
        json={'goal_statement': 'Refine a bounded autoresearch notebook with a coding model.'},
    )
    session_id = session.json()['session_id']

    client.post(
        '/intakes',
        json={
            'raw_request': 'Benchmark one bounded Titanic variant and capture a notebook with explicit package/runtime checks.',
            'source_refs': ['https://example.org/titanic-coder-note'],
            'notes': ['Notebook refinement should stay inside the approved template contract.'],
        },
    )
    interpretation = client.post('/interpretations/from-latest-intake').json()
    design = client.post('/design-drafts/from-latest-intake').json()
    client.post(
        f"/design-drafts/{design['design_id']}/review",
        json={
            'resolved_inputs': {
                'dataset_name': 'titanic',
                'train_uri': 's3://datasets/titanic/train.csv',
                'test_uri': 's3://datasets/titanic/test.csv',
                'target_column': 'Survived',
            },
            'review_notes': ['Notebook refinement coding-model test review.'],
        },
    )
    client.post(f'/research-sessions/{session_id}/transitions/start-autoresearch-campaign')
    client.post(f'/research-sessions/{session_id}/transitions/draft-methodologies')

    class FakeResponse:
        def __init__(self, payload: dict) -> None:
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            return json.dumps(self.payload).encode('utf-8')

    def fake_urlopen(request_obj, timeout):
        request_payload = json.loads(request_obj.data.decode('utf-8'))
        assert request_payload['model'] == 'qwen2.5-coder:14b'
        user_payload = json.loads(request_payload['messages'][1]['content'])
        assert user_payload['design_draft']['design_id'] == design['design_id']
        assert user_payload['interpretation']['interpretation_id'] == interpretation['interpretation_id']
        response_notebook = {
            'cells': [
                {
                    'cell_type': 'markdown',
                    'metadata': {},
                    'source': ['# Refined notebook\n'],
                },
                {
                    'cell_type': 'code',
                    'execution_count': None,
                    'metadata': {},
                    'outputs': [],
                    'source': ['import torch\n', 'import torchvision\n', "print('ok')\n"],
                },
            ],
            'metadata': {
                'glasslab': {
                    'kind': 'autoresearch-notebook-draft',
                }
            },
            'nbformat': 4,
            'nbformat_minor': 5,
        }
        return FakeResponse(
            {
                'message': {
                    'content': json.dumps(
                        {
                            'notebook': response_notebook,
                            'warnings': ['coding model refined notebook structure'],
                        }
                    )
                }
            }
        )

    monkeypatch.setattr(autoresearch_module.urllib_request, 'urlopen', fake_urlopen)

    refined = client.post(f'/research-sessions/{session_id}/transitions/refine-autoresearch-notebook')
    assert refined.status_code == 201
    payload = refined.json()
    assert payload['refinement_source'] == 'coding-model'
    assert 'coding model refined notebook structure' in payload['warnings']
    assert payload['storage_uri'].endswith('/analysis_notebook_refined.ipynb')
    assert payload['notebook']['cells'][1]['source'][0] == 'import torch\n'

    path = Path(payload['storage_uri'].removeprefix('file://'))
    assert path.exists()


def test_run_status_route_returns_200_during_kube_outage() -> None:
    settings = Settings(
        registry_dir=str(REPO_ROOT / 'services' / 'workflow-registry' / 'definitions'),
    )
    registry = WorkflowRegistry(settings.registry_dir)
    store = InMemoryRunStore()

    class OutageSubmitter(NullJobSubmitter):
        def get_live_status(self, record):
            raise LiveStatusUnavailableError(
                'Kubernetes transport failure during live status lookup'
            )

    client = TestClient(
        create_app(
            settings=settings,
            registry=registry,
            store=store,
            submitter=OutageSubmitter(namespace='default'),
        )
    )

    create = client.post(
        '/experiments/runs',
        json={
            'objective': 'Observe a run during a Kubernetes outage.',
            'experiment_type': 'gpu-training-job',
            'workload_id': 'metric-search-v0',
            'entrypoint': ['python3', 'scripts/run_experiment.py'],
            'config_payload': {'search_space_id': 'art-metric-baseline'},
            'dataset_bindings': {'train_uri': 's3://datasets/art/train.parquet'},
            'budget': {'max_epochs': 1, 'max_wallclock_minutes': 5},
            'submitted_by': 'test-suite',
        },
    )
    assert create.status_code == 201
    run_id = create.json()['run_id']

    got = client.get(f'/runs/{run_id}')
    assert got.status_code == 200
    status = got.json()['status']
    assert 'Live Kubernetes status unavailable' in status['detail']
    assert 'showing durable stored status' in status['detail']


def test_cancel_run_confirms_submitter_before_persisting_cancelled() -> None:
    class RecordingSubmitter(NullJobSubmitter):
        def __init__(self) -> None:
            super().__init__(namespace='default')
            self.cancelled = []

        def cancel_run(self, record) -> None:
            self.cancelled.append(record.run_id)

    submitter = RecordingSubmitter()
    client = build_client(submitter=submitter)
    created = client.post(
        '/experiments/runs',
        json={
            'objective': 'Cancel a bounded metric-search job.',
            'experiment_type': 'gpu-training-job',
            'workload_id': 'metric-search-v0',
            'entrypoint': ['python3', 'scripts/run_experiment.py'],
            'config_payload': {'search_space_id': 'art-metric-baseline'},
            'dataset_bindings': {'train_uri': 's3://datasets/art/train.parquet'},
            'budget': {'max_epochs': 1, 'max_wallclock_minutes': 5},
            'submitted_by': 'test-suite',
        },
    )
    run_id = created.json()['run_id']

    cancelled = client.post(f'/runs/{run_id}/cancel')

    assert cancelled.status_code == 200
    assert cancelled.json()['status']['status'] == 'cancelled'
    assert submitter.cancelled == [run_id]
    assert client.get(f'/runs/{run_id}').json()['status']['status'] == 'cancelled'

    repeated = client.post(f'/runs/{run_id}/cancel')
    assert repeated.status_code == 200
    assert submitter.cancelled == [run_id]


def test_cancel_run_does_not_persist_cancelled_when_submitter_fails() -> None:
    class FailingSubmitter(NullJobSubmitter):
        def cancel_run(self, record) -> None:
            raise LiveStatusUnavailableError('Kubernetes unavailable')

    client = build_client(submitter=FailingSubmitter(namespace='default'))
    created = client.post(
        '/experiments/runs',
        json={
            'objective': 'Fail closed during cancellation.',
            'experiment_type': 'gpu-training-job',
            'workload_id': 'metric-search-v0',
            'entrypoint': ['python3', 'scripts/run_experiment.py'],
            'config_payload': {'search_space_id': 'art-metric-baseline'},
            'dataset_bindings': {'train_uri': 's3://datasets/art/train.parquet'},
            'budget': {'max_epochs': 1, 'max_wallclock_minutes': 5},
            'submitted_by': 'test-suite',
        },
    )
    run_id = created.json()['run_id']

    cancelled = client.post(f'/runs/{run_id}/cancel')

    assert cancelled.status_code == 503
    assert client.get(f'/runs/{run_id}').json()['status']['status'] != 'cancelled'
