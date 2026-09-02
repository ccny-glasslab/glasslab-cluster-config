"""Discord rendering/controls and OpenCode runtime behavior.

Covers Discord message rendering and approval controls, the control-policy
gates and actor identity recording, HTTP adapter thread/status handling, and
the OpenCode runtime: event normalization, structured-output extraction and
repair, workspace-file materialization, and per-agent runtime isolation.
"""

from __future__ import annotations

from datetime import datetime, timezone
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import discord
import httpx
import pytest

from app.discord_adapter import DiscordHttpAdapter, DiscordRenderer
from app.config import Settings
from app.discord_controls import (
    DISCORD_MESSAGE_LIMIT,
    DiscordControlActor,
    DiscordControlGateway,
    DiscordControlPolicy,
    bound_discord_message,
    build_run_status_view,
    execute_discord_action,
    execute_discord_dataset_ingestion,
    execute_discord_research_question,
    execute_discord_run_control,
    execute_discord_run_cancellation,
    execute_discord_run_creation,
    execute_discord_turn_history,
    format_packet_for_discord,
    format_research_answer,
    job_status_counts,
    next_action_for,
    pending_human_approval,
    render_run_list,
    render_run_status,
    select_runs_for_list,
)
from app.opencode_runtime import (
    OpenCodeProcessRuntime,
    OpenCodeRuntimeError,
    extract_structured_output,
    materialize_declared_workspace_files,
    normalize_opencode_event,
    normalize_structured_output,
    parse_json_text,
)
from app.schemas import (
    AgentName,
    AgentTurnResult,
    Citation,
    EventRecord,
    ResearchAnswer,
    RunRecord,
    RunState,
    TurnKind,
    TurnRecord,
    utc_now,
    ApprovalStatus,
    JobStatus,
    PolicyClassification,
    RunState,
    TERMINAL_STATES,
)


def test_discord_renderer_has_no_live_api_dependency() -> None:
    renderer = DiscordRenderer()
    message = renderer.render(
        EventRecord(
            sequence_number=1,
            run_id='run-1',
            source='honeydew',
            event_type='agent.turn_completed',
            payload={'summary': 'Protocol drafted.'},
        )
    )
    assert message is not None
    assert message.identity == 'Honeydew'
    assert message.content == 'Protocol drafted.'


def test_discord_renderer_includes_agent_handoff() -> None:
    message = DiscordRenderer().render(
        EventRecord(
            sequence_number=2,
            run_id='run-1',
            source='beaker',
            event_type='agent.turn_completed',
            payload={
                'summary': 'Implementation proposal is ready.',
                'message_to_other_agent': 'Review the proposed controls.',
            },
        )
    )

    assert message is not None
    assert message.identity == 'Beaker'
    assert '**To Honeydew:** Review the proposed controls.' in message.content


def test_discord_pending_action_has_approval_controls() -> None:
    message = DiscordRenderer().render(
        EventRecord(
            sequence_number=3,
            run_id='run-1',
            source='orchestrator',
            event_type='action.proposed',
            payload={
                'action_id': 'action-1',
                'type': 'approve_protocol',
                'policy_classification': 'human_approval',
                'approval_status': 'pending',
                'human_approval_ready': True,
                'objective': 'Compare two bounded metric-learning methods.',
                'reason': 'Review the protocol before implementation.',
                'effect': (
                    'Authorize Beaker to implement; no cluster job is authorized.'
                ),
                'protocol_version': 1,
                'artifact': {
                    'uri': 'artifact://run-1/protocol/program.md',
                    'sha256': 'a' * 64,
                },
                'evaluation_contract': {
                    'contract_id': 'contract-1',
                    'version': '1.0.0',
                    'digest': 'b' * 64,
                },
                'contract_proposal': {
                    'evaluator_type': 'cifar100-unseen-v1',
                    'primary_metric': {
                        'name': 'test_unseen_global_recall_at_1',
                        'direction': 'maximize',
                        'minimum_effect': 0.02,
                    },
                    'guardrails': [
                        {
                            'name': 'effective_rank',
                            'direction': 'maximize',
                        }
                    ],
                    'budget_mode': 'training_exposure',
                    'resource_constraints': {
                        'cpu': 4,
                        'memory_gib': 16,
                        'gpus': 1,
                        'wallclock_minutes': 60,
                    },
                },
                'contract_binding': {
                    'status': 'requires_new_harness',
                    'contract_id': 'contract-1',
                    'version': '1.0.0',
                },
            },
        )
    )

    assert message is not None
    assert '**Research objective**' in message.content
    assert 'Compare two bounded metric-learning methods.' in message.content
    assert '**Approval authorizes**' in message.content
    assert 'no cluster job is authorized' in message.content
    assert 'artifact://run-1/protocol/program.md' in message.content
    assert "Honeydew's evaluation contract proposal" in message.content
    assert 'test_unseen_global_recall_at_1' in message.content
    assert 'requires_new_harness' in message.content
    assert message.components is not None
    buttons = message.components[0]['components']
    assert buttons[0]['label'] == 'Approve protocol'
    assert [button['custom_id'] for button in buttons] == [
        'glasslab:approve:action-1',
        'glasslab:reject:action-1',
    ]


def test_discord_matrix_waits_for_honeydew_before_showing_controls() -> None:
    payload = {
        'action_id': 'matrix-1',
        'type': 'submit_experiment_matrix',
        'policy_classification': 'honeydew_and_human_approval',
        'approval_status': 'pending',
        'human_approval_ready': False,
        'objective': 'Compare naive and semi-hard triplet mining.',
        'reason': 'The matrix requires methodology and human approval.',
        'effect': 'Authorize bounded cluster submission.',
        'preflight': {
            'passed': True,
            'job_count': 6,
            'checks': [
                'candidate config parsed',
                'deterministic expansion produces 6 jobs',
            ],
            'comparisons': {
                'miner': ['naive', 'semi_hard'],
            },
            'decisions': {
                'encoding': ['one_hot'],
            },
            'errors': [],
        },
        'arguments': {
            'variants': [
                {'name': 'naive-mining', 'overrides': {}},
                {'name': 'semi-hard-mining', 'overrides': {}},
            ],
            'seeds': [17, 31, 49],
            'maximum_parallel_jobs': 2,
            'runner_image': 'example/runner@sha256:abc',
            'resources': {
                'cpu': 4,
                'memory_gib': 16,
                'gpus': 1,
                'wallclock_minutes': 60,
            },
        },
    }
    renderer = DiscordRenderer()

    proposed = renderer.render(
        EventRecord(
            sequence_number=4,
            run_id='run-1',
            source='beaker',
            event_type='action.proposed',
            payload=payload,
        )
    )
    assert proposed is not None
    assert 'under methodology review' in proposed.content
    assert proposed.components is None
    assert '6 jobs' in proposed.content
    assert '1 GPU' in proposed.content
    assert '**Deterministic preflight**' in proposed.content
    assert 'miner=[naive, semi_hard]' in proposed.content

    requested = renderer.render(
        EventRecord(
            sequence_number=5,
            run_id='run-1',
            source='orchestrator',
            event_type='action.human_approval_requested',
            payload={**payload, 'human_approval_ready': True},
        )
    )
    assert requested is not None
    assert 'Approval requested' in requested.content
    assert requested.components is not None
    assert (
        requested.components[0]['components'][0]['label']
        == 'Approve 6 jobs'
    )


def test_discord_renders_durable_action_execution_failure() -> None:
    message = DiscordRenderer().render(
        EventRecord(
            sequence_number=6,
            run_id='run-1',
            source='orchestrator',
            event_type='action.execution_failed',
            payload={
                'action_id': 'matrix-1',
                'type': 'submit_experiment_matrix',
                'error': 'evaluation contract resource limit exceeded',
                'jobs_created': 0,
                'artifacts_created': 0,
                'resulting_state': 'BEAKER_REVISING',
                'next_step': (
                    'Beaker will revise the matrix before another approval.'
                ),
            },
        )
    )

    assert message is not None
    assert 'could not be executed' in message.content
    assert '0 job(s), 0 artifact(s)' in message.content
    assert 'BEAKER_REVISING' in message.content
    assert 'Beaker will revise' in message.content
    assert message.components is None


def test_discord_control_policy_uses_guild_role_or_user_id() -> None:
    policy = DiscordControlPolicy(
        guild_id='guild-1',
        admin_role_id='role-1',
        admin_user_ids=['user-1'],
    )

    assert policy.is_authorized(
        DiscordControlActor(
            user_id='user-1',
            display_name='Tyler',
            guild_id='guild-1',
            role_ids=frozenset(),
        )
    )
    assert policy.is_authorized(
        DiscordControlActor(
            user_id='user-2',
            display_name='Mike',
            guild_id='guild-1',
            role_ids=frozenset({'role-1'}),
        )
    )
    assert not policy.is_authorized(
        DiscordControlActor(
            user_id='user-3',
            display_name='Unapproved',
            guild_id='guild-1',
            role_ids=frozenset(),
        )
    )
    assert not policy.is_authorized(
        DiscordControlActor(
            user_id='user-1',
            display_name='Tyler',
            guild_id='other-guild',
            role_ids=frozenset({'role-1'}),
        )
    )


def test_discord_control_dispatch_records_immutable_identity() -> None:
    engine = Mock()
    actor = DiscordControlActor(
        user_id='142100176322953216',
        display_name='Tyler',
        guild_id='guild-1',
        role_ids=frozenset(),
    )

    execute_discord_action(
        engine,
        operation='approve',
        action_id='action-1',
        actor=actor,
    )

    engine.approve_action.assert_called_once_with(
        'action-1',
        reviewer='discord:142100176322953216:Tyler',
        reason='Approved through Discord controls.',
    )
    engine.reject_action.assert_not_called()


def test_discord_rejection_passes_human_revision_feedback() -> None:
    engine = Mock()
    actor = DiscordControlActor(
        user_id='142100176322953216',
        display_name='Tyler',
        guild_id='guild-1',
        role_ids=frozenset(),
    )

    execute_discord_action(
        engine,
        operation='reject',
        action_id='action-1',
        actor=actor,
        reason='Use the fixed 80/20 split and available GPU hardware.',
    )

    engine.reject_action.assert_called_once_with(
        'action-1',
        reviewer='discord:142100176322953216:Tyler',
        reason='Use the fixed 80/20 split and available GPU hardware.',
    )


def test_discord_gateway_registers_component_handler() -> None:
    gateway = DiscordControlGateway(
        engine=Mock(),
        bot_token='bot-token',
        guild_id='123456789',
        channel_id='987654321',
        admin_role_id='role-1',
        admin_user_ids=[],
        maximum_dataset_upload_bytes=1024,
    )

    assert gateway.client.on_interaction == gateway._on_interaction
    assert gateway.client.on_ready == gateway._on_ready
    start_command = gateway.tree.get_command(
        'task-start',
        guild=discord.Object(id=123456789),
    )
    assert start_command is not None
    archive_param = next(
        p for p in start_command.parameters if p.name == 'archive'
    )
    objective_param = next(
        p for p in start_command.parameters if p.name == 'objective'
    )
    assert archive_param.required is False
    assert objective_param.required is False
    for command in gateway.tree.get_commands(guild=discord.Object(id=123456789)):
        assert len(command.description) <= 100, (
            f'{command.name} description exceeds Discord\'s 100-char limit'
        )
    for retired_name in ('research-start', 'benchmark-start'):
        assert gateway.tree.get_command(
            retired_name,
            guild=discord.Object(id=123456789),
        ) is None
    cancel_command = gateway.tree.get_command(
        'research-cancel',
        guild=discord.Object(id=123456789),
    )
    assert cancel_command is not None
    for command_name in (
        'research-pause',
        'research-resume',
        'research-artifacts',
        'research-turns',
        'research-question',
        'dataset-upload',
        'research-status',
        'research-list',
    ):
        assert gateway.tree.get_command(
            command_name,
            guild=discord.Object(id=123456789),
        ) is not None


def test_discord_cancellation_records_actor_and_reason() -> None:
    engine = Mock()
    expected = SimpleNamespace(
        run_id='run-1',
        state=SimpleNamespace(value='CANCELLED'),
    )
    engine.cancel_run.return_value = expected
    actor = DiscordControlActor(
        user_id='142100176322953216',
        display_name='Tyler',
        guild_id='guild-1',
        role_ids=frozenset({'role-1'}),
    )

    result = execute_discord_run_cancellation(
        engine,
        run_id='run-1',
        actor=actor,
        reason='Superseded by benchmark validation.',
    )

    assert result is expected
    engine.cancel_run.assert_called_once_with(
        'run-1',
        requested_by='discord:142100176322953216:Tyler',
        reason='Superseded by benchmark validation.',
    )


def test_discord_pause_and_resume_record_actor_and_reason() -> None:
    engine = Mock()
    actor = DiscordControlActor(
        user_id='142100176322953216',
        display_name='Tyler',
        guild_id='guild-1',
        role_ids=frozenset({'role-1'}),
    )

    execute_discord_run_control(
        engine,
        operation='pause',
        run_id='run-1',
        actor=actor,
        reason='Hold while checking the dataset.',
    )
    execute_discord_run_control(
        engine,
        operation='resume',
        run_id='run-1',
        actor=actor,
    )

    engine.pause_run.assert_called_once_with(
        'run-1',
        requested_by='discord:142100176322953216:Tyler',
        reason='Hold while checking the dataset.',
    )
    engine.resume_run.assert_called_once_with(
        'run-1',
        requested_by='discord:142100176322953216:Tyler',
        reason='Resumed through Discord controls.',
    )


def test_discord_dataset_ingestion_records_actor() -> None:
    engine = Mock()
    expected = SimpleNamespace(reference_uri='glasslab-dataset://' + 'a' * 64)
    engine.datasets.ingest_bytes.return_value = expected
    actor = DiscordControlActor(
        user_id='142100176322953216',
        display_name='Tyler',
        guild_id='guild-1',
        role_ids=frozenset({'role-1'}),
    )

    result = execute_discord_dataset_ingestion(
        engine,
        filename='train.csv',
        content=b'x,y\n1,0\n',
        name='training_data',
        role='train',
        contains_labels=True,
        actor=actor,
        media_type='text/csv',
    )

    assert result is expected
    engine.datasets.ingest_bytes.assert_called_once_with(
        b'x,y\n1,0\n',
        filename='train.csv',
        name='training_data',
        role='train',
        contains_labels=True,
        media_type='text/csv',
        uploaded_by='discord:142100176322953216:Tyler',
    )


def _run_record(**overrides) -> RunRecord:
    now = utc_now()
    fields = dict(
        run_id='run-1',
        objective='Inspect turn history through Discord.',
        state=RunState.BEAKER_IMPLEMENTING,
        evaluation_contract_id='contract-1',
        evaluation_contract_version='1.0.0',
        evaluation_contract_digest='a' * 64,
        beaker_workspace='/tmp/beaker',
        honeydew_workspace='/tmp/honeydew',
        shared_artifacts_path='/tmp/shared',
        reports_path='/tmp/reports',
        maximum_turns=20,
        maximum_runtime_seconds=86400,
        maximum_parallel_jobs=1,
        created_at=now,
        updated_at=now,
    )
    fields.update(overrides)
    return RunRecord(**fields)


def test_discord_turn_history_redacts_credentials() -> None:
    engine = Mock()
    engine.store.get_run.return_value = _run_record()
    engine.store.list_turns.return_value = [
        TurnRecord(
            run_id='run-1',
            agent=AgentName.HONEYDEW,
            input_event={'objective': 'Inspect turn history through Discord.'},
            structured_output=AgentTurnResult(
                kind=TurnKind.PROTOCOL_DRAFT,
                summary='Drafted the initial protocol.',
            ),
            status='completed',
        ),
        TurnRecord(
            run_id='run-1',
            agent=AgentName.BEAKER,
            input_event={
                'objective': 'Inspect turn history through Discord.',
                'discord_bot_token': 'should-not-appear',
            },
            status='failed',
            error='provider rejected request: Bearer abcdefghijklmnop0123456789',
        ),
    ]

    message = execute_discord_turn_history(engine, run_id='run-1', limit=5)

    assert 'should-not-appear' not in message
    assert 'abcdefghijklmnop0123456789' not in message
    assert 'Honeydew' in message
    assert 'Beaker' in message
    assert 'Drafted the initial protocol.' in message
    engine.store.get_run.assert_called_once_with('run-1')
    engine.store.list_turns.assert_called_once_with('run-1')


def test_discord_turn_history_message_is_bounded() -> None:
    engine = Mock()
    engine.store.get_run.return_value = _run_record()
    engine.store.list_turns.return_value = [
        TurnRecord(
            run_id='run-1',
            agent=AgentName.BEAKER if index % 2 else AgentName.HONEYDEW,
            input_event={},
            structured_output=AgentTurnResult(
                kind=TurnKind.REVISION,
                summary='x' * 500,
            ),
            status='completed',
        )
        for index in range(20)
    ]

    message = execute_discord_turn_history(engine, run_id='run-1', limit=20)

    # Discord messages are capped at 2000 characters; the command must never
    # produce a payload that Discord would itself reject.
    assert len(message) < 2000
    assert 'truncated' in message


def test_discord_turn_history_reports_no_turns() -> None:
    engine = Mock()
    engine.store.get_run.return_value = _run_record()
    engine.store.list_turns.return_value = []

    message = execute_discord_turn_history(engine, run_id='run-1', limit=5)

    assert 'run-1' in message
    assert 'no recorded agent turns' in message


def test_discord_run_creation_uses_objective_without_http() -> None:
    engine = Mock()
    expected = SimpleNamespace(
        run_id='run-1',
        discord_thread_id='thread-1',
    )
    engine.create_run.return_value = expected

    result = execute_discord_run_creation(
        engine,
        objective='Compare bounded metric-learning miners.',
    )

    request = engine.create_run.call_args.args[0]
    assert request.objective == 'Compare bounded metric-learning miners.'
    assert result is expected


def test_discord_webhook_uses_agent_identity_and_thread() -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={'id': 'message-1'})

    adapter = DiscordHttpAdapter(
        bot_token='bot-token',
        channel_id='channel-1',
        webhook_url='https://discord.com/api/webhooks/webhook-id/token',
        transport=httpx.MockTransport(respond),
    )
    status_id = adapter.publish(
        thread_id='thread-1',
        status_message_id='status-1',
        event=EventRecord(
            sequence_number=3,
            run_id='run-1',
            source='honeydew',
            event_type='agent.turn_completed',
            payload={'summary': 'Methodology review complete.'},
        ),
    )

    assert status_id == 'status-1'
    assert len(requests) == 1
    assert requests[0].url.params['thread_id'] == 'thread-1'
    assert json.loads(requests[0].content)['username'] == 'Honeydew'


def test_discord_creates_public_run_thread() -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(201, json={'id': 'thread-1'})

    adapter = DiscordHttpAdapter(
        bot_token='bot-token',
        channel_id='channel-1',
        transport=httpx.MockTransport(respond),
    )

    thread_id = adapter.create_thread(
        run_id='1234567890abcdef',
        objective='Compare two bounded methods.',
    )

    assert thread_id == 'thread-1'
    assert requests[0].url.path.endswith('/channels/channel-1/threads')
    assert json.loads(requests[0].content) == {
        'name': 'research-12345678',
        'type': 11,
        'auto_archive_duration': 1440,
    }
    assert 'X-Audit-Log-Reason' in requests[0].headers


def test_discord_action_controls_are_posted_by_bot() -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={'id': 'control-message'})

    adapter = DiscordHttpAdapter(
        bot_token='bot-token',
        channel_id='channel-1',
        webhook_url='https://discord.com/api/webhooks/webhook-id/token',
        transport=httpx.MockTransport(respond),
    )
    adapter.publish(
        thread_id='thread-1',
        status_message_id=None,
        event=EventRecord(
            sequence_number=4,
            run_id='run-1',
            source='orchestrator',
            event_type='action.proposed',
            payload={
                'action_id': 'action-1',
                'type': 'approve_protocol',
                'policy_classification': 'human_approval',
                'approval_status': 'pending',
            },
        ),
    )

    assert len(requests) == 1
    assert requests[0].url.path == '/api/v10/channels/thread-1/messages'
    payload = json.loads(requests[0].content)
    assert (
        payload['components'][0]['components'][0]['label']
        == 'Approve protocol'
    )


def test_discord_status_message_id_is_reused() -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={'id': 'new-status'})

    adapter = DiscordHttpAdapter(
        bot_token='bot-token',
        channel_id='channel-1',
        transport=httpx.MockTransport(respond),
    )
    event = EventRecord(
        sequence_number=4,
        run_id='run-1',
        source='orchestrator',
        event_type='run.state_changed',
        payload={'from': 'CREATED', 'to': 'PREPARING'},
    )

    created = adapter.publish(
        thread_id='thread-1',
        status_message_id=None,
        event=event,
    )
    reused = adapter.publish(
        thread_id='thread-1',
        status_message_id=created,
        event=event,
    )

    assert created == 'new-status'
    assert reused == 'new-status'
    assert requests[0].method == 'POST'
    assert requests[1].method == 'PATCH'
    assert requests[1].url.path.endswith('/messages/new-status')


def test_opencode_event_normalization() -> None:
    normalized = normalize_opencode_event(
        {
            'type': 'permission.asked',
            'properties': {'permission': 'bash'},
        },
        run_id='run-1',
        agent=AgentName.BEAKER,
    )
    assert normalized is not None
    assert normalized[0] == 'agent.permission_requested'
    assert normalized[1]['runtime_event_type'] == 'permission.asked'
    assert normalize_opencode_event(
        {'type': 'unstable.internal.event', 'properties': {}},
        run_id='run-1',
        agent=AgentName.BEAKER,
    ) is None


def test_opencode_terminal_tool_signatures_ignore_incomplete_calls() -> None:
    messages = [
        {
            'parts': [
                {
                    'type': 'tool',
                    'tool': 'read',
                    'state': {
                        'status': 'completed',
                        'input': {'filePath': '/workspace/run.py', 'offset': 15},
                    },
                },
                {
                    'type': 'tool',
                    'tool': 'read',
                    'state': {
                        'status': 'pending',
                        'input': {'filePath': '/workspace/other.py'},
                    },
                },
            ]
        },
        {
            'parts': [
                {
                    'type': 'tool',
                    'tool': 'read',
                    'state': {
                        'status': 'error',
                        'input': {'offset': 15, 'filePath': '/workspace/run.py'},
                    },
                }
            ]
        },
    ]

    signatures = OpenCodeProcessRuntime._terminal_tool_signatures(messages)

    assert len(signatures) == 2
    assert signatures[0] == signatures[1]


def test_extracts_current_and_legacy_opencode_structured_output() -> None:
    current = {'info': {'structured': {'kind': 'protocol_draft'}}}
    legacy = {'info': {'structured_output': {'kind': 'protocol_draft'}}}

    assert extract_structured_output(current) == {'kind': 'protocol_draft'}
    assert extract_structured_output(legacy) == {'kind': 'protocol_draft'}
    assert extract_structured_output({'info': {}}) is None


def test_normalizes_live_qwen_nested_json_strings() -> None:
    normalized = normalize_structured_output(
        {
            'kind': 'protocol_draft',
            'summary': 'Draft complete.',
            'evaluation_contract_proposal': json.dumps(
                {
                    'evaluator_type': 'cifar100-unseen-v1',
                    'primary_metric': 'test_unseen_global_recall_at_1',
                    'primary_metric_direction': 'maximize',
                    'minimum_effect': 0.02,
                    'required_artifacts': ['metrics.json'],
                    'budget_mode': 'wallclock',
                    'max_wallclock_minutes': 60,
                    'resource_constraints': {
                        'cpu': 4,
                        'memory_gib': 16,
                        'gpus': 1,
                        'wallclock_minutes': 60,
                    },
                    'rationale': 'Compare the methods under one budget.',
                }
            ),
            'requested_actions': '[]',
            'produced_files': [
                {'path': 'program.md', 'purpose': 'protocol'}
            ],
        }
    )

    result = AgentTurnResult.model_validate(normalized)
    assert result.evaluation_contract_proposal is not None
    assert result.evaluation_contract_proposal.primary_metric.name == (
        'test_unseen_global_recall_at_1'
    )
    assert result.evaluation_contract_proposal.primary_metric.minimum_effect == 0.02


def test_materializes_only_declared_agent_workspace_file(tmp_path) -> None:
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    structured = {
        'produced_files': [{'path': 'program.md', 'purpose': 'protocol'}],
        'requested_actions': [
            {
                'type': 'write_file',
                'arguments': {
                    'path': 'program.md',
                    'content': '# Protocol\n',
                },
            },
            {
                'type': 'write_file',
                'arguments': {
                    'path': '../outside.md',
                    'content': 'not allowed',
                },
            },
            {'type': 'transition', 'arguments': {'to_state': 'COMPLETE'}},
        ],
    }

    normalized = materialize_declared_workspace_files(
        structured=structured,
        workspace=workspace,
        agent=AgentName.HONEYDEW,
    )

    assert (workspace / 'program.md').read_text() == '# Protocol\n'
    assert not (tmp_path / 'outside.md').exists()
    assert len(normalized['requested_actions']) == 1
    assert normalized['requested_actions'][0]['arguments']['path'] == '../outside.md'


def test_discord_failed_run_includes_authoritative_cause() -> None:
    message = DiscordRenderer().render(
        EventRecord(
            sequence_number=9,
            run_id='run-1',
            source='orchestrator',
            event_type='run.failed',
            payload={'error': 'Structured output field was invalid.'},
        )
    )

    assert message is not None
    assert '**Run failed**' in message.content
    assert 'Structured output field was invalid.' in message.content
    assert message.is_status is True


def test_opencode_writable_runtime_directories_are_per_agent(
    tmp_path,
) -> None:
    # config/data/state/home are session-specific (auth, logs, conversation
    # state) and must stay isolated per run; only the package/model download
    # cache (see the next test) is intentionally shared.
    runtime = OpenCodeProcessRuntime(
        Settings(opencode_shared_cache_root=str(tmp_path / 'shared-cache'))
    )
    workspace = tmp_path / 'run-1' / 'honeydew-worktree'
    workspace.mkdir(parents=True)

    config_root, data_root, cache_root, state_root, home_root = (
        runtime._write_runtime_config(
            run_id='run-1',
            agent=AgentName.HONEYDEW,
            workspace=workspace,
        )
    )

    per_run_roots = (config_root, data_root, state_root, home_root)
    assert all(path.is_dir() for path in per_run_roots)
    assert all(path.is_relative_to(tmp_path / 'run-1') for path in per_run_roots)
    assert cache_root.is_dir()
    assert not cache_root.is_relative_to(tmp_path / 'run-1')
    config = json.loads((config_root / 'opencode' / 'opencode.json').read_text())
    assert config['lsp'] is False
    assert config['permission']['task'] == 'deny'
    assert config['permission']['websearch'] == 'deny'
    assert config['permission']['external_directory'] == 'deny'
    assert config['model'].startswith('exo/')
    assert 'exo' in config['provider']


def test_opencode_cache_directory_is_shared_across_runs_and_agents(
    tmp_path,
) -> None:
    shared_cache = tmp_path / 'shared-cache'
    runtime = OpenCodeProcessRuntime(
        Settings(opencode_shared_cache_root=str(shared_cache))
    )

    roots = {}
    for run_id, agent in (
        ('run-1', AgentName.HONEYDEW),
        ('run-1', AgentName.BEAKER),
        ('run-2', AgentName.HONEYDEW),
    ):
        workspace = tmp_path / run_id / f'{agent.value}-worktree'
        workspace.mkdir(parents=True)
        _, _, cache_root, _, _ = runtime._write_runtime_config(
            run_id=run_id,
            agent=agent,
            workspace=workspace,
        )
        roots[(run_id, agent)] = cache_root

    # Every run and agent resolves to the exact same cache directory: the
    # whole point is that OpenCode's package/model downloads are fetched
    # once, not once per run.
    assert len(set(roots.values())) == 1
    assert next(iter(roots.values())) == shared_cache


def test_opencode_uses_builtin_zen_provider_for_big_pickle(tmp_path) -> None:
    runtime = OpenCodeProcessRuntime(
        Settings(
            agent_model_provider_id='opencode',
            agent_model_name='big-pickle',
        )
    )
    workspace = tmp_path / 'run-1' / 'beaker-worktree'
    workspace.mkdir(parents=True)

    roots = runtime._write_runtime_config(
        run_id='run-1',
        agent=AgentName.BEAKER,
        workspace=workspace,
    )

    config = json.loads((roots[0] / 'opencode' / 'opencode.json').read_text())
    assert config['model'] == 'opencode/big-pickle'
    assert config['small_model'] == 'opencode/big-pickle'
    assert 'provider' not in config


def test_prompt_structured_output_avoids_forced_tool_choice(
    tmp_path,
    monkeypatch,
) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                'info': {'id': 'message-prompt-json'},
                'parts': [
                    {
                        'type': 'text',
                        'text': json.dumps(
                            {
                                'kind': 'verification',
                                'summary': 'Prompt JSON validated.',
                            }
                        ),
                    }
                ],
            },
        )

    runtime = OpenCodeProcessRuntime(
        Settings(
            agent_model_provider_id='opencode',
            agent_model_name='big-pickle',
            opencode_structured_output_mode='prompt',
        )
    )
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    handle = SimpleNamespace(
        base_url='http://opencode.test',
        password='password',
    )
    monkeypatch.setattr(runtime, '_start_process', lambda **_: handle)
    monkeypatch.setattr(
        runtime,
        '_client',
        lambda _: httpx.Client(
            base_url=handle.base_url,
            transport=httpx.MockTransport(respond),
        ),
    )

    result, _ = runtime.run_turn(
        run_id='run-1',
        agent=AgentName.BEAKER,
        workspace=workspace,
        session_id='session-1',
        prompt='Verify provider compatibility.',
    )

    assert result.summary == 'Prompt JSON validated.'
    payload = json.loads(requests[0].content)
    assert 'format' not in payload
    assert 'Return only a JSON object' in payload['parts'][0]['text']
    assert 'AgentTurnResult' in payload['parts'][0]['text']


def test_opencode_provider_error_is_reported(tmp_path, monkeypatch) -> None:
    def respond(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                'info': {
                    'error': {
                        'name': 'APIError',
                        'data': {'message': 'provider rejected request'},
                    }
                }
            },
        )

    runtime = OpenCodeProcessRuntime(Settings())
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    handle = SimpleNamespace(
        base_url='http://opencode.test',
        password='password',
    )
    monkeypatch.setattr(runtime, '_start_process', lambda **_: handle)
    monkeypatch.setattr(
        runtime,
        '_client',
        lambda _: httpx.Client(
            base_url=handle.base_url,
            transport=httpx.MockTransport(respond),
        ),
    )

    with pytest.raises(OpenCodeRuntimeError, match='provider rejected request'):
        runtime.run_turn(
            run_id='run-1',
            agent=AgentName.BEAKER,
            workspace=workspace,
            session_id='session-1',
            prompt='Run.',
        )


def test_parse_json_text_accepts_json_markdown_fence() -> None:
    assert parse_json_text('```json\n{"kind": "verification"}\n```') == {
        'kind': 'verification'
    }


def test_opencode_repairs_invalid_structured_output(
    tmp_path,
    monkeypatch,
) -> None:
    requests: list[httpx.Request] = []
    responses = [
        {
            'info': {
                'id': 'message-invalid',
                'structured': {
                    'kind': 'protocol_draft',
                    'summary': 'Malformed action.',
                    'requested_actions': [{'reason': 'Missing type.'}],
                },
            }
        },
        {
            'info': {
                'id': 'message-repaired',
                'structured': {
                    'kind': 'protocol_draft',
                    'summary': 'Corrected structured result.',
                    'requested_actions': [],
                },
            }
        },
    ]

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=responses[len(requests) - 1])

    runtime = OpenCodeProcessRuntime(
        Settings(opencode_structured_repair_attempts=1)
    )
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    handle = SimpleNamespace(
        base_url='http://opencode.test',
        password='password',
    )
    monkeypatch.setattr(runtime, '_start_process', lambda **_: handle)
    monkeypatch.setattr(
        runtime,
        '_client',
        lambda _: httpx.Client(
            base_url=handle.base_url,
            transport=httpx.MockTransport(respond),
        ),
    )

    result, message_id = runtime.run_turn(
        run_id='run-1',
        agent=AgentName.HONEYDEW,
        workspace=workspace,
        session_id='session-1',
        prompt='Draft the protocol.',
    )

    assert result.summary == 'Corrected structured result.'
    assert message_id == 'message-repaired'
    assert len(requests) == 2
    repair_payload = json.loads(requests[1].content)
    assert repair_payload['model'] == {
        'providerID': 'exo',
        'modelID': 'mlx-community/Qwen3-Coder-Next-4bit',
    }
    assert 'Correct only the structured result' in (
        repair_payload['parts'][0]['text']
    )


def test_opencode_repairs_missing_structured_output(
    tmp_path,
    monkeypatch,
) -> None:
    requests: list[httpx.Request] = []
    responses = [
        {
            'info': {'id': 'message-without-structure'},
            'parts': [{'type': 'text', 'text': 'Implementation is complete.'}],
        },
        {
            'info': {
                'id': 'message-repaired',
                'structured': {
                    'kind': 'implementation_proposal',
                    'summary': 'Returned the completed implementation proposal.',
                    'requested_actions': [],
                },
            }
        },
    ]

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=responses[len(requests) - 1])

    runtime = OpenCodeProcessRuntime(
        Settings(opencode_structured_repair_attempts=1)
    )
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    handle = SimpleNamespace(
        base_url='http://opencode.test',
        password='password',
    )
    monkeypatch.setattr(runtime, '_start_process', lambda **_: handle)
    monkeypatch.setattr(
        runtime,
        '_client',
        lambda _: httpx.Client(
            base_url=handle.base_url,
            transport=httpx.MockTransport(respond),
        ),
    )

    result, message_id = runtime.run_turn(
        run_id='run-1',
        agent=AgentName.BEAKER,
        workspace=workspace,
        session_id='session-1',
        prompt='Implement the experiment.',
    )

    assert result.kind.value == 'implementation_proposal'
    assert message_id == 'message-repaired'
    assert len(requests) == 2
    repair_payload = json.loads(requests[1].content)
    assert 'Return only the structured result' in (
        repair_payload['parts'][0]['text']
    )


# ------------------------------------------------------------------ #
# /research-status and /research-list pure rendering and resolution
# ------------------------------------------------------------------ #


def _run(
    *,
    run_id: str,
    state: RunState,
    updated_at: datetime | None = None,
    thread_id: str | None = None,
):
    return SimpleNamespace(
        run_id=run_id,
        state=state,
        updated_at=updated_at or datetime(2026, 1, 1, tzinfo=timezone.utc),
        discord_thread_id=thread_id,
    )


def _action(
    *,
    type_: str,
    classification: PolicyClassification,
    status: ApprovalStatus = ApprovalStatus.PENDING,
    honeydew_approved: bool = False,
):
    return SimpleNamespace(
        type=type_,
        policy_classification=classification,
        approval_status=status,
        honeydew_approved=honeydew_approved,
    )


def _job(status: JobStatus):
    return SimpleNamespace(status=status)


def _gateway(runs) -> DiscordControlGateway:
    store = Mock()
    store.list_runs.return_value = list(runs)

    def get_run(run_id: str):
        for run in runs:
            if run.run_id == run_id:
                return run
        raise KeyError(run_id)

    store.get_run.side_effect = get_run
    engine = Mock()
    engine.store = store
    return DiscordControlGateway(
        engine=engine,
        bot_token='bot-token',
        guild_id='123456789',
        channel_id='main-channel',
        admin_role_id='role-1',
        admin_user_ids=[],
        maximum_dataset_upload_bytes=1024,
    )


def test_status_resolves_run_from_thread_without_run_id() -> None:
    run = _run(run_id='run-1', state=RunState.AWAITING_PROTOCOL_APPROVAL,
               thread_id='thread-1')
    gateway = _gateway([run])

    resolved = gateway._resolve_controlled_run(
        channel_id='thread-1',
        run_id=None,
    )

    assert resolved.run_id == 'run-1'


def test_status_requires_run_id_outside_thread() -> None:
    gateway = _gateway([_run(run_id='run-1', state=RunState.COMPLETE,
                             thread_id='thread-1')])

    with pytest.raises(ValueError, match='run_id is required'):
        gateway._resolve_controlled_run(
            channel_id='main-channel',
            run_id=None,
        )


def test_status_rejects_unknown_run_id() -> None:
    gateway = _gateway([_run(run_id='run-1', state=RunState.COMPLETE,
                             thread_id='thread-1')])

    with pytest.raises(KeyError):
        gateway._resolve_controlled_run(
            channel_id='main-channel',
            run_id='run-missing',
        )


def test_status_rejects_mismatched_thread_and_run() -> None:
    gateway = _gateway([
        _run(run_id='run-1', state=RunState.COMPLETE, thread_id='thread-1'),
        _run(run_id='run-2', state=RunState.JOB_RUNNING, thread_id='thread-2'),
    ])

    with pytest.raises(ValueError, match='control the run from'):
        gateway._resolve_controlled_run(
            channel_id='thread-1',
            run_id='run-2',
        )


def test_status_rejects_unsupported_channel() -> None:
    gateway = _gateway([_run(run_id='run-1', state=RunState.COMPLETE,
                             thread_id='thread-1')])

    with pytest.raises(ValueError, match='control the run from'):
        gateway._resolve_controlled_run(
            channel_id='other-channel',
            run_id='run-1',
        )


def test_pending_human_approval_precedes_automatic_actions() -> None:
    actions = [
        _action(
            type_='read_workspace',
            classification=PolicyClassification.AUTOMATIC,
        ),
        _action(
            type_='approve_protocol',
            classification=PolicyClassification.HUMAN_APPROVAL,
        ),
    ]

    pending = pending_human_approval(actions)

    assert pending is not None
    assert pending.type == 'approve_protocol'


def test_pending_human_approval_ignores_non_pending_and_agent_gates() -> None:
    actions = [
        _action(
            type_='submit_validation_job',
            classification=PolicyClassification.HONEYDEW_APPROVAL,
        ),
        _action(
            type_='approve_protocol',
            classification=PolicyClassification.HUMAN_APPROVAL,
            status=ApprovalStatus.APPROVED,
        ),
    ]

    assert pending_human_approval(actions) is None


@pytest.mark.parametrize(
    ('action_type', 'classification', 'expected'),
    [
        ('approve_protocol', PolicyClassification.HUMAN_APPROVAL,
         'approve the proposed protocol'),
        ('propose_evaluation_contract',
         PolicyClassification.HONEYDEW_AND_HUMAN_APPROVAL,
         'approve evaluation-contract promotion'),
        ('submit_experiment_matrix',
         PolicyClassification.HONEYDEW_AND_HUMAN_APPROVAL,
         'approve cluster execution'),
        ('accept_final_report', PolicyClassification.HUMAN_APPROVAL,
         'accept the final report'),
    ],
)
def test_next_action_prefers_pending_approval(
    action_type, classification, expected,
) -> None:
    run = _run(run_id='run-1', state=RunState.AWAITING_PROTOCOL_APPROVAL)
    actions = [
        _action(
            type_=action_type,
            classification=classification,
            honeydew_approved=(
                classification
                == PolicyClassification.HONEYDEW_AND_HUMAN_APPROVAL
            ),
        )
    ]

    assert next_action_for(run, actions) == expected


def test_pending_human_approval_requires_honeydew_gate() -> None:
    # A combined gate is not human-ready until Honeydew has signed off; before
    # that, the next action is derived from the Honeydew-review state.
    run = _run(run_id='run-1', state=RunState.HONEYDEW_REVIEWING)
    actions = [
        _action(
            type_='submit_experiment_matrix',
            classification=PolicyClassification.HONEYDEW_AND_HUMAN_APPROVAL,
            honeydew_approved=False,
        ),
    ]

    assert pending_human_approval(actions) is None
    assert next_action_for(run, actions) == (
        'Honeydew is reviewing the implementation'
    )


def test_job_status_counts_across_states() -> None:
    jobs = [
        _job(JobStatus.QUEUED),
        _job(JobStatus.QUEUED),
        _job(JobStatus.SUBMITTING),
        _job(JobStatus.RUNNING),
        _job(JobStatus.SUCCEEDED),
        _job(JobStatus.SUCCEEDED),
        _job(JobStatus.SUCCEEDED),
        _job(JobStatus.FAILED),
        _job(JobStatus.UNKNOWN),
    ]

    counts = job_status_counts(jobs)

    assert counts == {
        'queued': 2,
        'submitting': 1,
        'running': 1,
        'succeeded': 3,
        'failed': 1,
        'cancelled': 0,
        'unknown': 1,
    }


def test_terminal_run_has_no_pending_action() -> None:
    run = _run(run_id='run-1', state=RunState.COMPLETE)
    assert next_action_for(run, []) == 'no action; run is complete'


def test_run_list_active_first_then_recent_terminal() -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    runs = [
        _run(run_id='old-terminal', state=RunState.COMPLETE,
             updated_at=now),
        _run(run_id='active-old', state=RunState.JOB_RUNNING,
             updated_at=now),
        _run(run_id='active-new', state=RunState.JOB_RUNNING,
             updated_at=datetime(2026, 2, 1, tzinfo=timezone.utc)),
        _run(run_id='new-terminal', state=RunState.FAILED,
             updated_at=datetime(2026, 2, 2, tzinfo=timezone.utc)),
    ]

    selected = select_runs_for_list(runs)

    assert [run.run_id for run in selected] == [
        'active-new',
        'active-old',
        'new-terminal',
        'old-terminal',
    ]


def test_run_list_caps_at_ten() -> None:
    runs = [
        _run(run_id=f'run-{i}', state=RunState.JOB_RUNNING)
        for i in range(15)
    ]

    selected = select_runs_for_list(runs)

    assert len(selected) == 10


def test_run_list_uses_stable_tie_breaker() -> None:
    ts = datetime(2026, 3, 1, tzinfo=timezone.utc)
    runs = [
        _run(run_id='run-b', state=RunState.JOB_RUNNING, updated_at=ts),
        _run(run_id='run-a', state=RunState.JOB_RUNNING, updated_at=ts),
        _run(run_id='run-c', state=RunState.JOB_RUNNING, updated_at=ts),
    ]

    forward = select_runs_for_list(runs)
    reversed_ = select_runs_for_list(list(reversed(runs)))

    # Equal updated_at must order identically regardless of input order.
    assert [run.run_id for run in forward] == [
        run.run_id for run in reversed_
    ]
    assert len(set(run.run_id for run in forward)) == 3


def test_render_run_status_includes_durable_fields() -> None:
    run = _run(run_id='run-1', state=RunState.AWAITING_EXECUTION_APPROVAL)
    actions = [
        _action(
            type_='submit_experiment_matrix',
            classification=PolicyClassification.HONEYDEW_AND_HUMAN_APPROVAL,
            honeydew_approved=True,
        ),
    ]
    jobs = [_job(JobStatus.SUCCEEDED), _job(JobStatus.RUNNING)]

    view = build_run_status_view(run, actions, jobs)
    message = render_run_status(view)

    assert 'run-1' in message
    assert 'AWAITING_EXECUTION_APPROVAL' in message
    assert 'approve cluster execution' in message
    assert 'succeeded 1' in message
    assert 'running 1' in message
    assert 'submit_experiment_matrix' in message


def test_render_run_list_marks_terminal() -> None:
    runs = [
        _run(run_id='run-1', state=RunState.COMPLETE),
        _run(run_id='run-2', state=RunState.JOB_RUNNING),
    ]

    message = render_run_list(select_runs_for_list(runs))

    assert 'run-1' in message
    assert 'terminal' in message
    assert 'run-2' in message


def test_bound_discord_message_truncates() -> None:
    long = 'x' * (DISCORD_MESSAGE_LIMIT + 100)
    bounded = bound_discord_message(long)
    assert len(bounded) <= DISCORD_MESSAGE_LIMIT
    assert bounded.endswith('...')

    short = 'hello'
    assert bound_discord_message(short) == short

def test_execute_discord_research_question_delegates_to_engine() -> None:
    engine = Mock()
    expected = ResearchAnswer(
        answer='Conformal prediction guarantees coverage by construction.',
        citations=[
            Citation(
                knowledge_uri='knowledge://context/abc123',
                source='conformal-prediction-paper',
                excerpt='guarantees coverage without distributional assumptions',
            )
        ],
    )
    engine.answer_research_question.return_value = expected

    result = execute_discord_research_question(
        engine,
        question='how does conformal prediction guarantee coverage',
        conversation_id='discord-123',
    )

    assert result is expected
    engine.answer_research_question.assert_called_once_with(
        question='how does conformal prediction guarantee coverage',
        conversation_id='discord-123',
    )


def test_format_research_answer_cites_sources_with_excerpts() -> None:
    answer = ResearchAnswer(
        answer='Batch normalization accelerates training by stabilizing '
        'layer-input distributions.',
        citations=[
            Citation(
                knowledge_uri='knowledge://context/abc',
                source='batch-norm-paper',
                excerpt='fixes the means and variances of layer inputs',
            ),
            Citation(
                knowledge_uri='knowledge://context/abc',
                source='another-source',
                excerpt='reduces the need for Dropout',
            ),
        ],
    )
    rendered = format_research_answer(answer)
    assert 'batch-norm-paper' in rendered
    assert 'fixes the means and variances of layer inputs' in rendered
    assert 'Sources (2)' in rendered
    assert len(rendered) <= 2000


def test_format_research_answer_marks_unanswerable() -> None:
    rendered = format_research_answer(
        ResearchAnswer(
            answer='The corpus does not cover this.',
            unanswerable=True,
        )
    )
    assert 'does not contain material' in rendered
    assert len(rendered) <= 2000


def test_format_research_answer_truncates_long_answers_and_excerpts() -> None:
    answer = ResearchAnswer(
        answer=('word ' * 500).strip(),
        citations=[
            Citation(
                knowledge_uri='knowledge://context/abc',
                source='long-source-name',
                excerpt=('sentence ' * 60).strip(),
            )
        ],
    )
    rendered = format_research_answer(answer)
    assert len(rendered) <= 2000
    assert '...' in rendered


def _task_start_interaction() -> MagicMock:
    interaction = MagicMock()
    interaction.user.id = '142100176322953216'
    interaction.user.display_name = 'Tyler'
    interaction.guild_id = '123456789'
    interaction.channel_id = '987654321'
    interaction.user.roles = [SimpleNamespace(id='role-1')]
    interaction.response.send_message = AsyncMock()
    return interaction


def _task_start_interaction() -> MagicMock:
    interaction = MagicMock()
    interaction.user.id = '142100176322953216'
    interaction.user.display_name = 'Tyler'
    interaction.guild_id = '123456789'
    interaction.channel_id = '987654321'
    interaction.user.roles = []
    interaction.response.send_message = AsyncMock()
    return interaction


def _authorize(gateway: DiscordControlGateway) -> None:
    # MagicMock users are not discord.Member instances, so _actor() cannot
    # collect roles from them; substitute a real authorized actor directly.
    gateway._actor = lambda interaction: DiscordControlActor(
        user_id='142100176322953216',
        display_name='Tyler',
        guild_id='123456789',
        role_ids=frozenset({'role-1'}),
    )


def test_task_start_without_archive_creates_objective_run() -> None:
    engine = Mock()
    engine.create_run.return_value = SimpleNamespace(
        run_id='run-1',
        discord_thread_id='1234',
    )
    gateway = DiscordControlGateway(
        engine=engine,
        bot_token='bot-token',
        guild_id='123456789',
        channel_id='987654321',
        admin_role_id='role-1',
        admin_user_ids=[],
        maximum_dataset_upload_bytes=1024,
    )
    _authorize(gateway)
    interaction = _task_start_interaction()

    async def run_all() -> None:
        await gateway._on_task_start(
            interaction,
            archive=None,
            objective='Compare two uncertainty quantification methods on a held-out split.',
        )
        while gateway._tasks:
            tasks = list(gateway._tasks)
            gateway._tasks.clear()
            await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(run_all())

    interaction.response.send_message.assert_called_once()
    engine.create_run.assert_called_once()
    created = engine.create_run.call_args.args[0]
    assert 'uncertainty quantification' in created.objective


def test_task_start_with_archive_compiles_and_starts() -> None:
    engine = Mock()
    engine.task_bundles.MAX_ARCHIVE_BYTES = 10 * 1024 * 1024
    engine.import_task_bundle.return_value = SimpleNamespace(
        run_id='run-2',
        discord_thread_id='5678',
        task_id='task-2',
        display_name='UCI Adult Income',
        digest='a' * 64,
    )
    gateway = DiscordControlGateway(
        engine=engine,
        bot_token='bot-token',
        guild_id='123456789',
        channel_id='987654321',
        admin_role_id='role-1',
        admin_user_ids=[],
        maximum_dataset_upload_bytes=1024,
    )
    _authorize(gateway)
    interaction = _task_start_interaction()
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()
    archive = MagicMock()
    archive.size = 100
    archive.filename = 'task.zip'
    archive.read = AsyncMock(return_value=b'zip-bytes')

    asyncio.run(
        gateway._on_task_start(
            interaction,
            archive=archive,
            objective=None,
        )
    )

    interaction.response.defer.assert_called_once()
    interaction.followup.send.assert_called_once()
    engine.import_task_bundle.assert_called_once_with(
        filename='task.zip',
        content=b'zip-bytes',
    )
    engine.create_run.assert_called_once()
    created = engine.create_run.call_args.args[0]
    assert created.task_id == engine.import_task_bundle.return_value.task_id


def test_task_start_requires_archive_or_objective() -> None:
    engine = Mock()
    gateway = DiscordControlGateway(
        engine=engine,
        bot_token='bot-token',
        guild_id='123456789',
        channel_id='987654321',
        admin_role_id='role-1',
        admin_user_ids=[],
        maximum_dataset_upload_bytes=1024,
    )
    _authorize(gateway)
    interaction = _task_start_interaction()

    asyncio.run(
        gateway._on_task_start(
            interaction,
            archive=None,
            objective=None,
        )
    )

    interaction.response.send_message.assert_called_once()
    engine.create_run.assert_not_called()
    engine.import_task_bundle.assert_not_called()
class _FakeFollowup:
    def __init__(self, sink):
        self._sink = sink

    async def send(self, content, **kwargs):
        message = _FakeMessage(self._sink, content)
        self._sink.append(content)
        return message


class _FakeMessage:
    def __init__(self, sink, content):
        self._sink = sink
        self._content = content

    async def edit(self, content, **kwargs):
        self._sink.append(content)


class _FakeResponse:
    def __init__(self, defer_error=None):
        self._defer_error = defer_error

    async def defer(self, *args, **kwargs):
        if self._defer_error is not None:
            raise self._defer_error

    async def send_message(self, *args, **kwargs):
        pass


class _FakeInteraction:
    def __init__(self, *, channel_id='main-channel', user_id='doll-user'):
        self.channel_id = channel_id
        self.guild_id = '123456789'
        self.user = SimpleNamespace(id=user_id, name='doll', display_name='doll')
        self.followup_messages = []
        self.response = _FakeResponse()
        self.followup = _FakeFollowup(self.followup_messages)


def _build_test_gateway():
    store = Mock()
    store.list_runs.return_value = []
    engine = Mock()
    engine.store = store
    return DiscordControlGateway(
        engine=engine,
        bot_token='bot-token',
        guild_id='123456789',
        channel_id='main-channel',
        admin_role_id='role-1',
        admin_user_ids=['doll-user'],
        maximum_dataset_upload_bytes=1024,
    )


def test_research_question_survives_ack_failure() -> None:
    import asyncio

    gateway = _build_test_gateway()
    interaction = _FakeInteraction()
    interaction.response = _FakeResponse(defer_error=RuntimeError('ack raced'))
    # The handler must complete without raising when the ack can no longer be
    # claimed (duplicate invocation / expired interaction) — otherwise Discord
    # reports "The application did not respond".
    asyncio.run(
        gateway._on_research_question(interaction, question='some question')
    )
    assert interaction.followup_messages == []


def test_research_question_contains_followup_failure() -> None:
    import asyncio

    gateway = _build_test_gateway()

    def boom(*args, **kwargs):
        raise RuntimeError('engine exploded')

    gateway.engine.answer_research_question = boom
    interaction = _FakeInteraction()

    class _BrokenFollowup:
        async def send(self, *args, **kwargs):
            raise RuntimeError('followup window expired')

    interaction.followup = _BrokenFollowup()
    # Engine failure + a followup that also fails must still not escape the
    # handler; the error is logged, never a silent "did not respond".
    asyncio.run(
        gateway._on_research_question(interaction, question='some question')
    )
    assert interaction.followup_messages == []


def test_format_research_answer_uses_packet_id_not_dead_link() -> None:
    answer = ResearchAnswer(
        answer='A metric space pairs a set with a metric.',
        citations=[
            Citation(
                knowledge_uri='knowledge://context/0123456789abcdef',
                source='real-analysis-trench',
                excerpt='we must specify the couple (A, rho)',
            )
        ],
    )
    rendered = format_research_answer(answer)
    assert 'http://127.0.0.1' not in rendered
    assert '/packet <id>' in rendered
    assert '0123456789abcdef' in rendered
    assert len(rendered) <= 2000


def test_format_packet_for_discord_chunks_exact_text() -> None:
    engine = Mock()
    packet = Mock()
    packet.exact_text_supplied = ('word ' * 2000).strip()
    packet.ranked_sources = [{'source_id': 'calc-openstax'}]
    engine.knowledge.get_context_packet.return_value = packet

    chunks = format_packet_for_discord(engine, 'packet-123')

    assert chunks[0].startswith('**Packet `packet-123`**')
    assert 'calc-openstax' in chunks[0]
    assert all(len(c) <= 2000 for c in chunks)
    assert len(chunks) >= 3  # header + at least two body chunks
    engine.knowledge.get_context_packet.assert_called_once_with('packet-123')


def test_research_promote_in_thread_creates_run() -> None:
    import asyncio
    from types import SimpleNamespace

    gateway = _build_test_gateway()
    gateway.engine.promote_conversation = Mock(
        return_value=SimpleNamespace(
            run_id='run-promoted',
            state=SimpleNamespace(value='AWAITING_PROTOCOL_APPROVAL'),
        )
    )
    interaction = _FakeInteraction(channel_id='987654321')
    interaction.channel = discord.Thread.__new__(discord.Thread)
    asyncio.run(
        gateway._on_research_promote(interaction, objective='Run it.')
    )
    assert gateway.engine.promote_conversation.call_args[0] == (
        'discord-thread-987654321',
    )
    joined = ' '.join(interaction.followup_messages)
    assert 'run-promoted' in joined
    assert 'AWAITING_PROTOCOL_APPROVAL' in joined


def test_research_promote_requires_thread_and_authorization() -> None:
    import asyncio

    gateway = _build_test_gateway()
    main = _FakeInteraction(channel_id='main-channel')
    asyncio.run(
        gateway._on_research_promote(main, objective='Not a thread.')
    )
    assert gateway.engine.promote_conversation.called is False

    gateway_deny = _build_test_gateway()
    gateway_deny.engine.promote_conversation = lambda *a, **k: (_ for _ in ()).throw(AssertionError('must not run'))
    thread_interaction = _FakeInteraction(channel_id='987654321', user_id='stranger')
    thread_interaction.channel = discord.Thread.__new__(discord.Thread)
    asyncio.run(
        gateway_deny._on_research_promote(thread_interaction, objective='Nope.')
    )
    assert thread_interaction.followup_messages == []
