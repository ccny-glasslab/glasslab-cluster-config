"""Issue #379: Honeydew-only, read-only ``retrieve_evidence`` agent tool.

Drives a real orchestrator run on the scripted runtime. A Honeydew verification
turn calls ``retrieve_evidence`` twice with queries derived from its reasoning,
cites the returned ``knowledge://`` URIs, and the run still passes the existing
evidence gates. Also pins that Beaker never receives the tool and that the
tool file is written into Honeydew's OpenCode workspace only.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.evidence_resolver import EvidenceURIResolver
from app.knowledge_manager import digest_text
from app.knowledge_tool import (
    KnowledgeToolDenied,
    KnowledgeToolRegistry,
)
from app.main import create_app
from app.mock_runtime import ScriptedMockRuntime
from app.opencode_runtime import OpenCodeProcessRuntime
from app.schemas import (
    AgentName,
    AgentTurnResult,
    Claim,
    KnowledgeChunk,
    KnowledgeSource,
    RunCreateRequest,
    RunState,
    SourceType,
    TurnKind,
    VerificationVerdict,
)
from test_workflow import _advance_to_jobs, _complete_jobs

RUNNER_IMAGE = 'ghcr.io/ccny-glasslab/glasslab-test-runner:test'

COVERAGE_QUERY = 'conformal prediction coverage calibration'
INTERVAL_QUERY = 'split conformal intervals nonconformity quantile'
PAPER_TEXT = (
    'Conformal prediction provides finite-sample coverage guarantees by '
    'construction on exchangeable calibration data. Split conformal intervals '
    'use a held-out calibration set to set the quantile of nonconformity '
    'scores, so marginal coverage is at least one minus alpha without '
    'distributional assumptions. '
) * 4


class RetrieveEvidenceRuntime(ScriptedMockRuntime):
    """Scripted Honeydew verification turn that iterates the retrieval tool."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.tool_calls: list[tuple[str, int]] = []
        self.tool_results: list = []

    def run_turn(self, **kwargs):
        agent = kwargs.get('agent')
        prompt = kwargs.get('prompt', '')
        tool = kwargs.get('knowledge_tool')
        if agent is AgentName.HONEYDEW and 'Independently verify' in prompt:
            assert tool is not None, 'Honeydew must receive the tool'
            first = tool(COVERAGE_QUERY, 3)
            second = tool(INTERVAL_QUERY, 3)
            self.tool_calls.extend(
                [(COVERAGE_QUERY, 3), (INTERVAL_QUERY, 3)]
            )
            self.tool_results.extend([first, second])
            knowledge_uris = list(
                dict.fromkeys(
                    uri
                    for result in (first, second)
                    for uri in result.uris
                    if uri.startswith('knowledge://')
                )
            )
            return (
                AgentTurnResult(
                    kind=TurnKind.VERIFICATION,
                    summary='Verified the measured results against the corpus.',
                    claims=[
                        Claim(
                            text='The retrieved corpus supports the '
                            'measured coverage.',
                            evidence=knowledge_uris[:1],
                        )
                    ],
                    verification_verdict=VerificationVerdict(
                        status='consistent',
                        summary='Measured results are consistent with the '
                        'retrieved corpus material.',
                        citations=knowledge_uris,
                    ),
                    recommended_next_state=RunState.HONEYDEW_WRITING_REPORT,
                    done=True,
                ),
                'mock-verification-message',
            )
        return super().run_turn(**kwargs)


def _ingest_paper(engine, run_id: str) -> KnowledgeSource:
    return engine.knowledge.ingest_text(
        source_type=SourceType.PAPER,
        canonical_uri=f'paper://{run_id}/conformal-prediction',
        text=PAPER_TEXT,
        title='Conformal prediction primer',
        run_scope=run_id,
        access_policy='run-approved',
        emit_event_for_run=run_id,
    )


def test_honeydew_retrieve_evidence_iterates_and_passes_evidence_gates(
    orchestrator_bundle,
) -> None:
    _, store, cluster, _, engine = orchestrator_bundle
    runtime = RetrieveEvidenceRuntime(runner_image=RUNNER_IMAGE)
    engine.runtime = runtime

    run = _advance_to_jobs(engine, store)
    source = _ingest_paper(engine, run.run_id)

    run = _complete_jobs(engine, store, cluster, run.run_id)

    # The verification turn called the tool twice and the run advanced through
    # the existing evidence gates instead of bouncing to a revision.
    assert runtime.tool_calls == [(COVERAGE_QUERY, 3), (INTERVAL_QUERY, 3)]
    assert run.state == RunState.AWAITING_FINAL_ACCEPTANCE

    resolver = EvidenceURIResolver(store)
    returned = [uri for result in runtime.tool_results for uri in result.uris]
    assert source.evidence_uri() in returned
    for uri in returned:
        if uri.startswith('knowledge://'):
            resolved = resolver.resolve(uri)
            assert resolved.resolved is True, resolved.error
    assert any(uri.startswith('knowledge://') for uri in returned)

    events = store.list_events(run.run_id)
    tool_calls = [
        event for event in events if event.event_type == 'agent.tool_call'
    ]
    tool_results = [
        event for event in events if event.event_type == 'agent.tool_result'
    ]
    assert [event.payload['query'] for event in tool_calls] == [
        COVERAGE_QUERY,
        INTERVAL_QUERY,
    ]
    assert all(event.payload['tool'] == 'retrieve_evidence' for event in tool_calls)
    assert all(
        event.payload['returned_uris'] for event in tool_results
    )
    assert source.evidence_uri() in tool_results[0].payload['returned_uris']

    context = runtime.tool_results[0].context or ''
    assert '<knowledge-context' in context


def test_beaker_never_receives_the_tool(orchestrator_bundle) -> None:
    _, store, cluster, _, engine = orchestrator_bundle
    runtime = RetrieveEvidenceRuntime(runner_image=RUNNER_IMAGE)
    engine.runtime = runtime

    run = _advance_to_jobs(engine, store)
    _complete_jobs(engine, store, cluster, run.run_id)

    received = runtime.knowledge_tools_received
    assert any(agent is AgentName.HONEYDEW for agent, _ in received)
    assert all(
        tool is None
        for agent, tool in received
        if agent is AgentName.BEAKER
    )

    # The per-agent surface gate is in the engine, not just the runtime.
    assert (
        engine._knowledge_tool_for(
            run_id=run.run_id,
            agent=AgentName.BEAKER,
            turn_number=1,
            turn_kind=TurnKind.IMPLEMENTATION_PLAN,
        )
        is None
    )
    honeydew_tool = engine._knowledge_tool_for(
        run_id=run.run_id,
        agent=AgentName.HONEYDEW,
        turn_number=1,
        turn_kind=TurnKind.VERIFICATION,
    )
    assert honeydew_tool is not None

    # Even a bound tool refuses a non-Honeydew caller.
    with pytest.raises(KnowledgeToolDenied):
        registry = KnowledgeToolRegistry(
            knowledge=engine.knowledge,
            store=store,
            settings=engine.settings,
        )
        registry.bind(
            run_id=run.run_id,
            agent=AgentName.BEAKER,
            turn_number=1,
            turn_kind=TurnKind.VERIFICATION,
        )(COVERAGE_QUERY, 3)


def test_tool_output_is_bounded_by_evidence_snapshot_budget(
    orchestrator_bundle,
) -> None:
    _, store, _, _, engine = orchestrator_bundle
    engine.settings.evidence_snapshot_max_bytes = 1024
    run = engine.create_run(
        RunCreateRequest(objective='Bound the retrieval tool output.')
    )
    _ingest_paper(engine, run.run_id)
    tool = engine._knowledge_tool_for(
        run_id=run.run_id,
        agent=AgentName.HONEYDEW,
        turn_number=1,
        turn_kind=TurnKind.VERIFICATION,
    )
    assert tool is not None
    first = tool(COVERAGE_QUERY, 10)
    second = tool(INTERVAL_QUERY, 10)
    assert first.bytes_returned + second.bytes_returned <= 1024
    assert first.truncated or second.truncated

    events = store.list_events(run.run_id)
    recorded = sum(
        event.payload['bytes_returned']
        for event in events
        if event.event_type == 'agent.tool_result'
    )
    assert recorded <= 1024


def test_tool_output_excludes_secret_bearing_chunks(orchestrator_bundle) -> None:
    _, store, _, _, engine = orchestrator_bundle
    run = engine.create_run(
        RunCreateRequest(objective='Never surface secret-bearing chunks.')
    )
    secret_text = 'conformal coverage api_key = sk-abc123456789'
    secret_source = KnowledgeSource(
        source_type=SourceType.PAPER,
        canonical_uri='paper://secret/conformal',
        run_scope=run.run_id,
        access_policy='run-approved',
        digest=digest_text(secret_text),
        title='Secret reprint',
    )
    store.save_knowledge_source(secret_source)
    store.replace_knowledge_chunks(
        secret_source.source_id,
        [
            KnowledgeChunk(
                source_id=secret_source.source_id,
                chunk_index=0,
                text=secret_text,
                digest=digest_text(secret_text),
                token_count=8,
            )
        ],
    )
    tool = engine._knowledge_tool_for(
        run_id=run.run_id,
        agent=AgentName.HONEYDEW,
        turn_number=1,
        turn_kind=TurnKind.VERIFICATION,
    )
    assert tool is not None
    result = tool('conformal coverage', 5)
    assert secret_source.evidence_uri() not in result.uris
    assert 'api_key' not in (result.context or '')
    assert 'sk-abc123456789' not in (result.context or '')


def test_tool_ranking_matches_the_per_turn_retrieval_pipeline(
    orchestrator_bundle,
) -> None:
    _, store, _, _, engine = orchestrator_bundle
    run = engine.create_run(
        RunCreateRequest(objective='Prove the tool adds no second ranking.')
    )
    _ingest_paper(engine, run.run_id)
    packet = engine.knowledge.retrieve(
        run_id=run.run_id,
        agent='honeydew',
        turn_number=1,
        turn_kind='protocol_draft',
        query=COVERAGE_QUERY,
        max_results=5,
        run_scope=run.run_id,
    )
    evidence = engine.knowledge.retrieve_evidence(
        run_id=run.run_id,
        agent='honeydew',
        turn_number=1,
        turn_kind='protocol_draft',
        query=COVERAGE_QUERY,
        max_results=5,
        run_scope=run.run_id,
    )
    assert [entry['entry_id'] for entry in packet.ranked_sources] == [
        entry['entry_id'] for entry in evidence.packet.ranked_sources
    ]


def test_tool_binding_token_is_stable_across_turns(orchestrator_bundle) -> None:
    _, _, _, _, engine = orchestrator_bundle
    run = engine.create_run(
        RunCreateRequest(objective='Reuse one capability token per run.')
    )
    first = engine._knowledge_tool_for(
        run_id=run.run_id,
        agent=AgentName.HONEYDEW,
        turn_number=1,
        turn_kind=TurnKind.PROTOCOL_DRAFT,
    )
    second = engine._knowledge_tool_for(
        run_id=run.run_id,
        agent=AgentName.HONEYDEW,
        turn_number=2,
        turn_kind=TurnKind.VERIFICATION,
    )
    assert first is second
    assert second.turn_number == 2
    assert second.turn_kind is TurnKind.VERIFICATION

    with pytest.raises(KnowledgeToolDenied):
        engine.execute_knowledge_tool(
            token='not-a-real-token', query=COVERAGE_QUERY, k=3
        )


def test_opencode_registers_the_tool_in_honeydew_workspace_only(
    tmp_path,
) -> None:
    runtime = OpenCodeProcessRuntime(
        Settings(opencode_shared_cache_root=str(tmp_path / 'cache'))
    )
    honeydew_workspace = tmp_path / 'honeydew-worktree'
    beaker_workspace = tmp_path / 'beaker-worktree'
    honeydew_workspace.mkdir()
    beaker_workspace.mkdir()

    class _FakeBinding:
        token = 'capability-token-123'

    binding = _FakeBinding()
    runtime._sync_knowledge_tool_file(
        workspace=honeydew_workspace,
        agent=AgentName.HONEYDEW,
        knowledge_tool=binding,
    )
    tool_path = honeydew_workspace / '.opencode' / 'tools' / 'retrieve_evidence.js'
    assert tool_path.is_file()
    source = tool_path.read_text()
    assert 'retrieve_evidence' in source
    assert binding.token in source
    assert Settings().knowledge_tool_endpoint_url in source

    runtime._sync_knowledge_tool_file(
        workspace=beaker_workspace,
        agent=AgentName.BEAKER,
        knowledge_tool=binding,
    )
    assert not (
        beaker_workspace / '.opencode' / 'tools' / 'retrieve_evidence.js'
    ).exists()

    runtime._sync_knowledge_tool_file(
        workspace=honeydew_workspace,
        agent=AgentName.HONEYDEW,
        knowledge_tool=None,
    )
    assert not tool_path.exists()


def test_http_retrieve_evidence_endpoint_requires_bound_token(
    orchestrator_bundle,
) -> None:
    settings, store, _, _, engine = orchestrator_bundle
    run = engine.create_run(
        RunCreateRequest(objective='Serve the tool over the callback endpoint.')
    )
    _ingest_paper(engine, run.run_id)
    tool = engine._knowledge_tool_for(
        run_id=run.run_id,
        agent=AgentName.HONEYDEW,
        turn_number=1,
        turn_kind=TurnKind.VERIFICATION,
    )
    assert tool is not None
    app = create_app(settings, engine=engine, start_watcher=False)
    with TestClient(app) as client:
        payload = {'query': COVERAGE_QUERY, 'k': 3}
        missing = client.post(
            '/internal/agent-tools/retrieve-evidence', json=payload
        )
        assert missing.status_code == 401
        unknown = client.post(
            '/internal/agent-tools/retrieve-evidence',
            json=payload,
            headers={'X-Glasslab-Tool-Token': 'not-a-real-token'},
        )
        assert unknown.status_code == 403
        response = client.post(
            '/internal/agent-tools/retrieve-evidence',
            json=payload,
            headers={'X-Glasslab-Tool-Token': tool.token},
        )
        assert response.status_code == 200
        body = response.json()
        assert body['returned_uris']
        assert any(
            uri.startswith('knowledge://') for uri in body['returned_uris']
        )
        assert '<knowledge-context' in body['context']
