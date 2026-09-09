"""Tests for the persistence layer: JSON file store and Postgres backend.

Validates that both backends round-trip records correctly and survive a
simulated restart (reloading the store), and that settings reject invalid
or blank backend configuration.  The Postgres tests use a fake psycopg
adapter so they run without a live database.
"""

import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

for module_name in list(sys.modules):
    if module_name == 'app' or module_name.startswith('app.'):
        del sys.modules[module_name]

from app.autoresearch import summarize_campaign
from app.config import Settings
from app.job_submission import NullJobSubmitter
import app.persistence as persistence_module
from app.persistence import JsonFileRunStore, create_run_store
from app.schemas import (
    AutoresearchCampaignRecord,
    AutoresearchIterationRecord,
    ComparisonRecord,
    InvestigationHypothesisRecord,
    InvestigationRecord,
    JobSubmissionReceipt,
    MethodologyDraftRecord,
    OperationRecord,
    PaperIntakeQueueRecord,
    ResearchProblemRecord,
    ResearchSessionRecord,
    RunRecord,
    ScheduledExecutionRecord,
    SourceDocumentRecord,
)
from services.common.schemas import RunManifest, RunStatus

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_settings_fail_closed_when_memory_store_is_disallowed() -> None:
    with pytest.raises(ValueError, match='allow_inmemory_store=false'):
        Settings(
            registry_dir=str(REPO_ROOT / 'services' / 'workflow-registry' / 'definitions'),
            store_backend='memory',
            allow_inmemory_store=False,
        )


def test_json_store_rejects_blank_path() -> None:
    with pytest.raises(ValueError, match='non-empty store_json_path'):
        Settings(
            registry_dir=str(REPO_ROOT / 'services' / 'workflow-registry' / 'definitions'),
            store_backend='json',
            store_json_path='   ',
        )


def test_json_store_persists_session_and_stage_metadata_across_restart(tmp_path: Path) -> None:
    state_path = tmp_path / 'run-store.json'
    now = datetime(2026, 3, 26, 15, 30, tzinfo=timezone.utc)

    store = JsonFileRunStore(state_path)

    session = ResearchSessionRecord(
        session_id='session-1',
        created_at=now,
        updated_at=now,
        status='active',
        title='Durable state validation',
        goal_statement='Keep the durable store path explicit before Postgres lands.',
        priorities=['durability'],
        submitted_by='glasslab-operator',
        latest_problem_id='problem-1',
        latest_queue_id='queue-1',
        latest_document_id='document-1',
    )
    problem = ResearchProblemRecord(
        problem_id='problem-1',
        created_at=now,
        updated_at=now,
        status='ready',
        problem_statement='Verify the JSON backend survives a restart.',
        submitted_by='glasslab-operator',
        session_id=session.session_id,
    )
    queue = PaperIntakeQueueRecord(
        queue_id='queue-1',
        created_at=now,
        updated_at=now,
        problem_statement='Verify the JSON backend survives a restart.',
        submitted_by='glasslab-operator',
        session_id=session.session_id,
    )
    document = SourceDocumentRecord(
        document_id='document-1',
        created_at=now,
        updated_at=now,
        status='fetched',
        source_url='https://example.org/paper.pdf',
        submitted_by='glasslab-operator',
        storage_uri='file:///tmp/paper.pdf',
        session_id=session.session_id,
    )
    execution = ScheduledExecutionRecord(
        execution_id='execution-1',
        schedule_id='schedule-1',
        operation_type='paper-intake',
        started_at=now,
        finished_at=now,
        result_status='ok',
        result_detail='persisted',
    )
    operation = OperationRecord(
        operation_id='operation-1',
        operation_type='paper-intake',
        status='completed',
        started_at=now,
        finished_at=now,
        session_id=session.session_id,
        queue_id=queue.queue_id,
        document_id=document.document_id,
        result_detail='stored session and stage metadata',
    )
    investigation = InvestigationRecord(
        investigation_id='investigation-1',
        created_at=now,
        updated_at=now,
        status='planning',
        title='Durable investigation state',
        research_question='Does the durable investigation aggregate survive a store restart?',
        research_mode='confirmatory',
        hypotheses=[
            InvestigationHypothesisRecord(
                hypothesis_id='hypothesis-1',
                statement='The investigation aggregate survives a JSON store restart.',
                created_at=now,
                submitted_by='glasslab-operator',
            )
        ],
        submitted_by='glasslab-operator',
    )

    store.save_research_session(session)
    store.save_investigation(investigation)
    store.save_research_problem(problem)
    store.save_paper_intake_queue(queue)
    store.save_source_document(document)
    store.save_execution(execution)
    store.save_operation(operation)

    reloaded = create_run_store('json', state_path=state_path)

    reloaded_session = reloaded.get_latest_research_session()
    assert reloaded_session is not None
    assert reloaded_session.session_id == session.session_id
    assert reloaded_session.latest_queue_id == queue.queue_id
    assert reloaded.get_investigation(investigation.investigation_id) == investigation
    assert reloaded.get_latest_investigation() == investigation
    assert reloaded.get_research_problem(problem.problem_id) == problem
    assert reloaded.get_paper_intake_queue(queue.queue_id) == queue
    assert reloaded.get_source_document(document.document_id) == document
    assert reloaded.get_latest_operation() == operation
    assert reloaded.list_executions(schedule_id='schedule-1') == [execution]


def test_json_store_persists_comparison_records_across_restart(tmp_path: Path) -> None:
    state_path = tmp_path / 'run-store.json'
    now = datetime(2026, 4, 22, 16, 0, tzinfo=timezone.utc)

    store = JsonFileRunStore(state_path)
    comparison = ComparisonRecord(
        comparison_id='cmp-1',
        created_at=now,
        updated_at=now,
        status='completed',
        comparison_type='model-selection',
        evaluator_type='art_retrieval_v1',
        session_id='session-1',
        campaign_id='campaign-1',
        workload_id='metric-search-v0',
        run_ids=['run-a', 'run-b'],
        baseline_run_id='run-a',
        candidate_run_ids=['run-b'],
        summary_metrics={
            'primary_metric_name': 'retrieval_recall_at_10',
            'baseline_value': 0.71,
            'candidate_value': 0.76,
            'delta': 0.05,
            'comparable': True,
        },
        artifact_refs={
            'comparison_json': 's3://artifacts/cmp-1/comparison.json',
            'summary_md': 's3://artifacts/cmp-1/summary.md',
        },
        notes=['bounded two-run comparison'],
    )

    store.save_comparison(comparison)
    reloaded = create_run_store('json', state_path=state_path)

    assert reloaded.get_comparison('cmp-1') == comparison
    assert reloaded.get_latest_comparison() == comparison
    assert reloaded.list_comparisons(session_id='session-1') == [comparison]


def test_postgres_store_requires_dsn() -> None:
    with pytest.raises(ValueError, match='non-empty store_postgres_dsn'):
        Settings(
            registry_dir=str(REPO_ROOT / 'services' / 'workflow-registry' / 'definitions'),
            store_backend='postgres',
            store_postgres_dsn='   ',
        )


def test_postgres_store_round_trips_through_psycopg_adapter(monkeypatch) -> None:
    # Fake psycopg adapter: intercepts INSERT and SELECT on the single
    # workflow_state table.  The real PostgresStore issues a CREATE TABLE
    # IF NOT EXISTS on initialization; the fake silently ignores that DDL
    # because the test only needs to verify payload round-trip, not schema
    # creation.
    state: dict[str, object] = {}

    class FakeCursor:
        def __init__(self) -> None:
            self._row = None

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, query, params=None):
            if 'SELECT payload' in query:
                payload = state.get('payload')
                self._row = (payload,) if payload is not None else None
            elif 'INSERT INTO workflow_state' in query:
                state['payload'] = params[1]

        def fetchone(self):
            return self._row

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return FakeCursor()

        def commit(self):
            return None

    fake_psycopg = SimpleNamespace(connect=lambda dsn: FakeConnection())
    monkeypatch.setattr(persistence_module, '_import_psycopg', lambda: fake_psycopg)

    now = datetime(2026, 4, 22, 12, 0, tzinfo=timezone.utc)
    store = create_run_store('postgres', postgres_dsn='postgresql://test')
    session = ResearchSessionRecord(
        session_id='session-1',
        created_at=now,
        updated_at=now,
        status='active',
        title='Postgres durability',
        goal_statement='Persist workflow metadata in Postgres instead of the artifacts share.',
        priorities=['durability'],
        submitted_by='glasslab-operator',
    )
    investigation = InvestigationRecord(
        investigation_id='investigation-1',
        created_at=now,
        updated_at=now,
        status='planning',
        title='Postgres investigation durability',
        research_question='Does Postgres preserve the first-class investigation aggregate?',
        research_mode='exploratory',
        hypotheses=[
            InvestigationHypothesisRecord(
                hypothesis_id='hypothesis-1',
                statement='Postgres preserves the investigation aggregate.',
                created_at=now,
                submitted_by='glasslab-operator',
            )
        ],
        submitted_by='glasslab-operator',
    )
    store.save_research_session(session)
    store.save_investigation(investigation)

    reloaded = create_run_store('postgres', postgres_dsn='postgresql://test')
    reloaded_session = reloaded.get_latest_research_session()
    assert reloaded_session is not None
    assert reloaded_session.session_id == session.session_id
    assert reloaded.get_latest_investigation() == investigation


def test_postgres_store_round_trips_comparison_records(monkeypatch) -> None:
    state: dict[str, object] = {}

    class FakeCursor:
        def __init__(self) -> None:
            self._row = None

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, query, params=None):
            if 'SELECT payload' in query:
                payload = state.get('payload')
                self._row = (payload,) if payload is not None else None
            elif 'INSERT INTO workflow_state' in query:
                state['payload'] = params[1]

        def fetchone(self):
            return self._row

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return FakeCursor()

        def commit(self):
            return None

    fake_psycopg = SimpleNamespace(connect=lambda dsn: FakeConnection())
    monkeypatch.setattr(persistence_module, '_import_psycopg', lambda: fake_psycopg)

    now = datetime(2026, 4, 22, 18, 0, tzinfo=timezone.utc)
    store = create_run_store('postgres', postgres_dsn='postgresql://test')
    comparison = ComparisonRecord(
        comparison_id='cmp-1',
        created_at=now,
        updated_at=now,
        status='completed',
        comparison_type='model-selection',
        evaluator_type='art_retrieval_v1',
        session_id='session-1',
        run_ids=['run-a', 'run-b'],
        baseline_run_id='run-a',
        candidate_run_ids=['run-b'],
        summary_metrics={'delta': 0.05, 'comparable': True},
        artifact_refs={'comparison_json': 's3://artifacts/cmp-1/comparison.json'},
    )

    store.save_comparison(comparison)
    reloaded = create_run_store('postgres', postgres_dsn='postgresql://test')

    assert reloaded.get_comparison('cmp-1') == comparison
    assert reloaded.get_latest_comparison() == comparison


def test_failed_iteration_maps_to_needs_review_and_reloads_cleanly(tmp_path: Path) -> None:
    """A failed run must map to the valid out-of-contract status 'needs_review'.

    Regression for #256: refresh_campaign_iterations used to persist the
    schema-forbidden status 'failed' (model_copy does not re-validate), which
    made the store crash-loop on reload. The refreshed iteration must be
    'needs_review' and a restarted store must parse cleanly.
    """
    state_path = tmp_path / 'run-store.json'
    now = datetime(2026, 4, 22, 18, 0, tzinfo=timezone.utc)

    store = JsonFileRunStore(state_path)

    campaign = AutoresearchCampaignRecord(
        campaign_id='campaign-1',
        session_id='session-1',
        created_at=now,
        updated_at=now,
        status='active',
        objective='Find the best tabular model.',
        evaluation_policy='primary-metric',
        mutation_policy='single-axis',
        latest_iteration_id='iteration-1',
    )
    draft = MethodologyDraftRecord(
        methodology_draft_id='method-1',
        campaign_id='campaign-1',
        session_id='session-1',
        created_at=now,
        updated_at=now,
        objective='Compare logistic regression baselines.',
        hypothesis='A tuned baseline beats the default.',
        method_family='tabular-benchmark',
        bounded_experimentability='bounded',
        status='launched',
        workflow_id='generic-tabular-benchmark',
        workflow_family='tabular-benchmark',
        resource_profile='cpu-small',
        approval_tier='tier-1-read-only',
    )
    iteration = AutoresearchIterationRecord(
        iteration_id='iteration-1',
        campaign_id='campaign-1',
        child_methodology_draft_id='method-1',
        run_id='run-1',
        created_at=now,
        updated_at=now,
        status='launched',
    )
    manifest = RunManifest(
        run_id='run-1',
        workflow_id='generic-tabular-benchmark',
        workflow_family='tabular-benchmark',
        display_name='Tabular Benchmark',
        objective='Test failed-run status mapping.',
        submitted_by='tester',
        submitted_at=now,
        run_priority='user',
        inputs={'dataset_name': 'titanic'},
        requested_models=['logistic_regression'],
        resource_profile='cpu-small',
        resource_requests={},
        resource_limits={},
        node_selector={},
        runner_image='busybox:latest',
        runner_service_account_name='registry-runner',
        evaluator_type='none',
        approval_tier='tier-1-read-only',
        expected_artifacts={'required': ['status.json'], 'optional': []},
    )
    run = RunRecord(
        run_id='run-1',
        workflow_id='generic-tabular-benchmark',
        created_at=now,
        updated_at=now,
        manifest=manifest,
        status=RunStatus(run_id='run-1', status='failed', updated_at=now, detail='boom'),
        job_submission=JobSubmissionReceipt(
            job_name='job',
            namespace='glasslab-v2',
            accepted_at=now,
            status='accepted',
            detail='ok',
        ),
    )

    store.save_autoresearch_campaign(campaign)
    store.save_methodology_draft(draft)
    store.save_autoresearch_iteration(iteration)
    store.save_run(run)

    settings = Settings(
        registry_dir=str(REPO_ROOT / 'services' / 'workflow-registry' / 'definitions'),
    )
    submitter = NullJobSubmitter(namespace='glasslab-v2')

    summary = summarize_campaign(store, campaign, settings=settings, submitter=submitter)
    refreshed = summary.iterations[0]
    assert refreshed.status == 'needs_review'

    reloaded = create_run_store('json', state_path=state_path)
    reloaded_iteration = reloaded.get_autoresearch_iteration('iteration-1')
    assert reloaded_iteration is not None
    assert reloaded_iteration.status == 'needs_review'
