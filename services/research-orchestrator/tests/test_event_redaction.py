"""Free-form read payloads are redacted at the HTTP boundary, never in store.

Regression coverage for issue #369: read endpoints returned unredacted event
payloads, action arguments, artifact metadata, and run task/seed state. The
API now redacts response copies with ``app.redaction.redact_payload`` so the
durable records keep their original bytes.
"""

from __future__ import annotations

import json
from hashlib import sha256

from fastapi.testclient import TestClient

from app.main import create_app
from app.schemas import (
    ActionRecord,
    AgentName,
    ApprovalStatus,
    ArtifactRecord,
    PolicyClassification,
    RunCreateRequest,
)

# A Discord-bot-token-shaped value and a bearer token: both match concrete
# credential formats, so redaction is exercised by value shape, not only by
# a credential-looking field name.
TOKEN_SHAPED = 'Bearer abcdefghijklmnop0123456789'
BOT_TOKEN = 'MTIzNDU2Nzg5MDEyMzQ1Njc4.GHIJKL.mnopqrstuvwx'


def _app(settings, engine) -> TestClient:
    return TestClient(create_app(settings, engine=engine, start_watcher=False))


def test_run_events_redact_payload_without_mutating_store(
    orchestrator_bundle,
) -> None:
    settings, store, _, _, engine = orchestrator_bundle
    run = engine.create_run(
        RunCreateRequest(objective='Exercise event payload redaction.')
    )
    seeded = store.append_event(
        run_id=run.run_id,
        source='test',
        event_type='action.proposed',
        payload={
            'discord_bot_token': BOT_TOKEN,
            'note': TOKEN_SHAPED,
            'token_count': 3,
        },
    )

    with _app(settings, engine) as client:
        response = client.get(f'/runs/{run.run_id}/events')
        assert response.status_code == 200
        target = next(
            event
            for event in response.json()['events']
            if event['event_id'] == seeded.event_id
        )
        assert target['payload']['discord_bot_token'] == '[REDACTED]'
        assert target['payload']['note'] == '[REDACTED]'
        # token_count is a count, not a credential; the safe-key exception
        # keeps it readable.
        assert target['payload']['token_count'] == 3
        assert BOT_TOKEN not in json.dumps(response.json())
        assert 'abcdefghijklmnop0123456789' not in json.dumps(response.json())

    persisted = next(
        event
        for event in store.list_events(run.run_id)
        if event.event_id == seeded.event_id
    )
    assert persisted.payload['discord_bot_token'] == BOT_TOKEN
    assert persisted.payload['note'] == TOKEN_SHAPED


def test_event_stream_frame_redacts_payload(orchestrator_bundle) -> None:
    # The SSE route yields sse_frame(event); Starlette's TestClient buffers an
    # infinite stream and would hang, so the frame builder is tested directly.
    from app.main import sse_frame

    settings, store, _, _, engine = orchestrator_bundle
    run = engine.create_run(
        RunCreateRequest(objective='Exercise SSE event payload redaction.')
    )
    seeded = store.append_event(
        run_id=run.run_id,
        source='test',
        event_type='action.proposed',
        payload={'discord_bot_token': BOT_TOKEN},
    )

    frame = sse_frame(seeded)
    assert frame.startswith(f'id: {seeded.sequence_number}\n')
    assert frame.endswith('\n\n')
    data = json.loads(frame.split('data: ', 1)[1])
    assert data['payload']['discord_bot_token'] == '[REDACTED]'
    assert BOT_TOKEN not in frame


def test_action_arguments_redacted_without_mutating_store(
    orchestrator_bundle,
) -> None:
    settings, store, _, _, engine = orchestrator_bundle
    run = engine.create_run(
        RunCreateRequest(objective='Exercise action argument redaction.')
    )
    action = ActionRecord(
        run_id=run.run_id,
        proposed_by=AgentName.BEAKER,
        type='request_cluster_execution',
        arguments={
            'operator_api_token': 'super-secret-value',
            'note': TOKEN_SHAPED,
        },
        policy_classification=PolicyClassification.HUMAN_APPROVAL,
        approval_status=ApprovalStatus.PENDING,
        reason='Seed an action for redaction coverage.',
        idempotency_key='redaction-test-action',
    )
    store.save_action(action)

    with _app(settings, engine) as client:
        response = client.get(f'/actions/{action.action_id}')
        assert response.status_code == 200
        body = response.json()
        assert body['arguments']['operator_api_token'] == '[REDACTED]'
        assert body['arguments']['note'] == '[REDACTED]'
        assert 'super-secret-value' not in json.dumps(body)

    persisted = store.get_action(action.action_id)
    assert persisted.arguments['operator_api_token'] == 'super-secret-value'
    assert persisted.arguments['note'] == TOKEN_SHAPED


def test_artifact_metadata_redacted_without_mutating_store(
    orchestrator_bundle,
) -> None:
    settings, store, _, _, engine = orchestrator_bundle
    run = engine.create_run(
        RunCreateRequest(objective='Exercise artifact metadata redaction.')
    )
    artifact = ArtifactRecord(
        run_id=run.run_id,
        type='report',
        uri='s3://artifacts/research-orchestrator/report.md',
        sha256=sha256(b'report-body').hexdigest(),
        metadata={
            'discord_webhook_url': 'https://discord.com/api/webhooks/secret',
            'note': TOKEN_SHAPED,
        },
    )
    store.save_artifact(artifact)

    with _app(settings, engine) as client:
        response = client.get(f'/runs/{run.run_id}/artifacts')
        assert response.status_code == 200
        target = next(
            item
            for item in response.json()['artifacts']
            if item['artifact_id'] == artifact.artifact_id
        )
        assert target['metadata']['discord_webhook_url'] == '[REDACTED]'
        assert target['metadata']['note'] == '[REDACTED]'
        # Digests and URIs are provenance, not secrets; they stay intact.
        assert target['sha256'] == artifact.sha256
        assert target['uri'] == artifact.uri

    persisted = next(
        item
        for item in store.list_artifacts(run.run_id)
        if item.artifact_id == artifact.artifact_id
    )
    assert (
        persisted.metadata['discord_webhook_url']
        == 'https://discord.com/api/webhooks/secret'
    )


def test_run_structured_fields_redacted_but_objective_kept(
    orchestrator_bundle,
) -> None:
    settings, store, _, _, engine = orchestrator_bundle
    run = engine.create_run(
        RunCreateRequest(
            objective='Exercise run-level redaction of structured fields.',
            seed_context='carry token=abc123 into the protocol draft',
        )
    )
    current = store.get_run(run.run_id)
    store.replace_run(
        current.model_copy(
            update={
                'task_definition': {
                    'runner_image': 'ghcr.io/example/runner:test',
                    'api_key': 'sk-0123456789abcdef',
                    'nested': {'note': TOKEN_SHAPED},
                }
            }
        ),
        expected_version=current.version,
    )

    with _app(settings, engine) as client:
        single = client.get(f'/runs/{run.run_id}').json()
        assert single['seed_context'] == '[REDACTED]'
        assert single['task_definition']['api_key'] == '[REDACTED]'
        assert single['task_definition']['nested']['note'] == '[REDACTED]'
        # A human-facing objective is plain prose; the keyword heuristic would
        # blank it wholesale, so it is deliberately left readable.
        assert single['objective'] == run.objective
        # ids/digests/paths are provenance and must survive redaction.
        assert single['evaluation_contract_digest'] == (
            run.evaluation_contract_digest
        )
        assert single['beaker_workspace'] == run.beaker_workspace

        listed = client.get('/runs').json()['runs']
        target = next(item for item in listed if item['run_id'] == run.run_id)
        assert target['seed_context'] == '[REDACTED]'
        assert target['task_definition']['api_key'] == '[REDACTED]'

    persisted = store.get_run(run.run_id)
    assert persisted.seed_context == 'carry token=abc123 into the protocol draft'
    assert persisted.task_definition['api_key'] == 'sk-0123456789abcdef'
