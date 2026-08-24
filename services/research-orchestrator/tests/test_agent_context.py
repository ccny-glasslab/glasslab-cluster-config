"""Agent context retrieval and the knowledge packets it feeds to turns.

Covers the empty state (no artifacts yet), surfaced recorded artifacts,
durable packet persistence and citation, injection of retrieved context into
later Honeydew/Beaker turns, and the ordering guarantee that recovery framing
always leads the prompt over retrieved reference material.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from app.schemas import AgentName, RunCreateRequest, RunRecord, RunState, TurnKind
from test_workflow import _advance_to_jobs, _complete_jobs, _pending_action


def _bare_run(store) -> RunRecord:
    # A run inserted directly through the store, bypassing engine.create_run,
    # so it has no events and no recorded artifacts yet.
    now = datetime.now(timezone.utc)
    return store.create_run(
        RunRecord(
            run_id='bare-run-1',
            objective='A run with no events yet.',
            state=RunState.CREATED,
            evaluation_contract_id='example-research-v1',
            evaluation_contract_version='1.0.0',
            evaluation_contract_digest='a' * 64,
            beaker_workspace='/tmp/beaker',
            honeydew_workspace='/tmp/honeydew',
            shared_artifacts_path='/tmp/shared',
            reports_path='/tmp/reports',
            maximum_turns=20,
            maximum_runtime_seconds=3600,
            maximum_parallel_jobs=2,
            created_at=now,
            updated_at=now,
        ),
        one_active_run=False,
    )


def _context_text(engine, **kwargs):
    packet = engine._get_agent_context(**kwargs)
    return packet.exact_text_supplied if packet else None


def test_agent_context_is_empty_before_any_artifacts(orchestrator_bundle) -> None:
    _, store, _, _, engine = orchestrator_bundle
    run = engine.create_run(
        RunCreateRequest(
            objective='Verify retrieval stays empty before evidence exists.'
        )
    )
    # create_run records the protocol artifact, so assert it is surfaced as
    # context rather than being absent from the very first retrieval.
    context = _context_text(
        engine,
        run_id=run.run_id,
        agent=AgentName.BEAKER,
        turn_number=1,
        turn_kind=TurnKind.IMPLEMENTATION_PLAN,
        query='implementation',
    )
    assert context is not None
    assert f'artifact://{run.run_id}/protocol/program.md' in context


def test_agent_context_is_empty_for_run_without_events(
    orchestrator_bundle,
) -> None:
    _, store, _, _, engine = orchestrator_bundle
    run = _bare_run(store)
    context = _context_text(
        engine,
        run_id=run.run_id,
        agent=AgentName.BEAKER,
        turn_number=1,
        turn_kind=TurnKind.IMPLEMENTATION_PLAN,
        query='implementation',
    )
    assert context is None


def test_agent_context_surfaces_recorded_artifacts(
    orchestrator_bundle,
) -> None:
    _, store, _, _, engine = orchestrator_bundle
    run = engine.create_run(
        RunCreateRequest(
            objective='Verify retrieval surfaces prior recorded artifacts.'
        )
    )
    store.append_event(
        run_id=run.run_id,
        source='honeydew',
        event_type='artifact.recorded',
        payload={
            'uri': f'artifact://{run.run_id}/protocol/program.md',
            'type': 'protocol',
        },
    )
    store.append_event(
        run_id=run.run_id,
        source='beaker',
        event_type='artifact.recorded',
        payload={
            'uri': f'artifact://{run.run_id}/metrics/metrics.json',
            'type': 'metrics',
        },
    )
    context = _context_text(
        engine,
        run_id=run.run_id,
        agent=AgentName.HONEYDEW,
        turn_number=1,
        turn_kind=TurnKind.VERIFICATION,
        query='protocol metrics evidence',
    )
    assert context is not None
    assert '[Event' in context
    assert f'artifact://{run.run_id}/protocol/program.md' in context
    assert f'artifact://{run.run_id}/metrics/metrics.json' in context


def test_retrieval_persists_context_packet_and_events(
    orchestrator_bundle,
) -> None:
    _, store, _, _, engine = orchestrator_bundle
    run = engine.create_run(
        RunCreateRequest(
            objective='Verify context packets are durable and citable.'
        )
    )
    packet = engine._get_agent_context(
        run_id=run.run_id,
        agent=AgentName.BEAKER,
        turn_number=1,
        turn_kind=TurnKind.IMPLEMENTATION_PLAN,
        query='implementation',
    )
    assert packet is not None
    stored = engine.knowledge.get_context_packet(packet.packet_id)
    assert stored.run_id == run.run_id
    assert stored.agent == AgentName.BEAKER
    assert stored.evidence_uri() == f'knowledge://context:{packet.packet_id}'
    packets = engine.store.list_context_packets(run.run_id)
    assert any(p.packet_id == packet.packet_id for p in packets)
    events = store.list_events(run.run_id)
    assert any(
        event.event_type == 'agent.context_retrieved'
        for event in events
    )


def test_complete_workflow_injects_retrieved_context_into_later_turns(
    orchestrator_bundle,
) -> None:
    # Drives the whole workflow to the final-report turn, then asserts the
    # report prompt carries retrieved reference material, proving retrieval
    # feeds live turns rather than existing only as an isolated API.
    _, store, cluster, runtime, engine = orchestrator_bundle
    run = _advance_to_jobs(engine, store)
    run = _complete_jobs(engine, store, cluster, run.run_id)
    final_action = _pending_action(store, run.run_id, 'accept_final_report')
    engine.approve_action(
        final_action.action_id,
        reviewer='test-human',
        reason='Report accepted.',
    )
    report_prompt = next(
        prompt
        for agent, prompt in runtime.prompts
        if agent == AgentName.HONEYDEW and 'Write report.md' in prompt
    )
    assert 'The following is retrieved reference material' in report_prompt
    assert '<knowledge-context' in report_prompt
    assert 'Artifact: artifact://' in report_prompt
    assert 'Write report.md' in report_prompt


def test_report_prompt_requires_new_workspace_file(
    orchestrator_bundle,
) -> None:
    # Pins the live incident where runs 4be29763/f12cbc14 failed three times:
    # Honeydew returned purpose='report' pointing at 'reports/report.md'
    # without creating any file in its workspace, so copy_agent_output raised
    # 'agent output is not a real file'. The prompt must demand creation.
    _, store, cluster, runtime, engine = orchestrator_bundle
    run = _advance_to_jobs(engine, store)
    _complete_jobs(engine, store, cluster, run.run_id)
    report_prompt = next(
        prompt
        for agent, prompt in runtime.prompts
        if agent == AgentName.HONEYDEW and 'Write report.md' in prompt
    )
    assert 'create' in report_prompt.lower()
    assert 'your own workspace' in report_prompt.lower()
    assert 'must exist as a real file' in report_prompt.lower()
    assert 'do not reference job artifacts' in report_prompt.lower()


def test_context_attached_event_records_packet_id(
    orchestrator_bundle,
) -> None:
    _, store, _, _, engine = orchestrator_bundle
    run = engine.create_run(
        RunCreateRequest(
            objective='Context attachment must be recorded as an event.'
        )
    )
    store.append_event(
        run_id=run.run_id,
        source='beaker',
        event_type='artifact.recorded',
        payload={
            'uri': f'artifact://{run.run_id}/protocol/program.md',
            'type': 'protocol',
        },
    )
    engine._run_agent_turn(
        run_id=run.run_id,
        agent=AgentName.BEAKER,
        prompt='Write implementation-plan.md for the bounded task.',
        expected_kind=TurnKind.IMPLEMENTATION_PLAN,
        input_event={'turn_id': 'manual-turn'},
    )
    attached = next(
        event
        for event in store.list_events(run.run_id)
        if event.event_type == 'agent.context_attached'
    )
    packet = engine.knowledge.get_context_packet(attached.payload['packet_id'])
    assert packet.run_id == run.run_id
    assert attached.payload['agent'] == AgentName.BEAKER.value


def test_recovery_context_still_leads_after_retrieval(orchestrator_bundle) -> None:
    _, store, _, runtime, engine = orchestrator_bundle
    run = engine.create_run(
        RunCreateRequest(
            objective='Recovery framing must lead over retrieved context.'
        )
    )
    store.append_event(
        run_id=run.run_id,
        source='beaker',
        event_type='artifact.recorded',
        payload={
            'uri': f'artifact://{run.run_id}/protocol/program.md',
            'type': 'protocol',
        },
    )
    checkpoint = engine.workspaces.paths(run.run_id).events / (
        f'{AgentName.BEAKER.value}-recovery-checkpoint.json'
    )
    checkpoint.write_text(json.dumps({'checkpoint': True}))
    engine._run_agent_turn(
        run_id=run.run_id,
        agent=AgentName.BEAKER,
        prompt='Write implementation-plan.md for the bounded task.',
        expected_kind=TurnKind.IMPLEMENTATION_PLAN,
        input_event={'turn_id': 'manual-turn'},
    )
    prompt = runtime.prompts[-1][1]
    assert prompt.startswith(
        'This is a fresh OpenCode session after an interrupted or failed turn.'
    )
    assert '<knowledge-context' in prompt
    assert f'artifact://{run.run_id}/protocol/program.md' in prompt
    assert prompt.index(
        'This is a fresh OpenCode session after an interrupted or failed turn.'
    ) < prompt.index('Write implementation-plan.md')
    assert 'AUTHORITATIVE STRUCTURED OUTPUT CONTRACT' in prompt
    assert 'exactly `implementation_plan`' in prompt
