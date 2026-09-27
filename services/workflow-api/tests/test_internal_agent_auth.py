"""Every workflow-api -> stage-agent call must carry the shared internal token.

Issue #602: the four stage agents (intake, interpretation, assessment, design)
and schedule-worker fail closed until the shared internal token is configured
and presented. These tests capture the outbound urllib request at each of the
five workflow-api call sites and assert the token header is attached when
``agent_internal_token`` is configured.

The token is deliberately omitted when it is not configured: rollout order is
callers first, server enforcement second, so a caller deployed before the
shared Secret exists must keep working against servers that do not yet enforce
authentication. The server side fails closed on its own missing token.

The ranker is intentionally excluded: it is not one of the four stage agents
and does not validate the token, so sending it there would only widen secret
exposure.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import URLError

from pydantic import SecretStr

# Prevent stale ``app.*`` module state from leaking between test modules.
for module_name in list(sys.modules):
    if module_name == 'app' or module_name.startswith('app.'):
        del sys.modules[module_name]

import app.main as main_module  # noqa: E402
import app.stage_design as stage_design  # noqa: E402
import app.stage_inference as stage_inference  # noqa: E402
import app.stage_interpretation as stage_interpretation  # noqa: E402
from app.config import Settings  # noqa: E402
from app.internal_agent_auth import INTERNAL_TOKEN_HEADER, internal_agent_headers  # noqa: E402
from app.persistence import InMemoryRunStore  # noqa: E402
from app.registry import WorkflowRegistry  # noqa: E402
from app.schemas import (  # noqa: E402
    IntakeCreateRequest,
    IntakeRecord,
    InterpretationRecord,
    ResearchProblemPipelineRequest,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
REGISTRY_DIR = str(REPO_ROOT / 'services' / 'workflow-registry' / 'definitions')
INTERNAL_TOKEN = 'shared-internal-token'


def build_settings(**overrides) -> Settings:
    return Settings(
        registry_dir=REGISTRY_DIR,
        agent_internal_token=SecretStr(INTERNAL_TOKEN),
        **overrides,
    )


def capture_outbound_headers(monkeypatch, module) -> list[dict[str, str]]:
    """Stub urlopen just long enough to capture the request headers."""
    captured: list[dict[str, str]] = []

    def fake_urlopen(request_obj, timeout):
        captured.append(dict(request_obj.header_items()))
        raise URLError('captured outbound request')

    monkeypatch.setattr(module.urllib_request, 'urlopen', fake_urlopen)
    return captured


def header_value(headers: dict[str, str], name: str) -> str | None:
    # urllib normalizes header keys (str.capitalize()), so compare case-blind.
    return {key.lower(): value for key, value in headers.items()}.get(name.lower())


def now() -> datetime:
    return datetime.now(timezone.utc)


def build_intake_record() -> IntakeRecord:
    return IntakeRecord(
        intake_id='intake-internal-auth-1',
        created_at=now(),
        updated_at=now(),
        status='ready_for_interpretation',
        source_type='paper-link',
        source_refs=['https://example.org/paper'],
        document_refs=[],
        raw_request='Read this paper and propose a bounded benchmark with one evaluation target.',
        normalized_summary='Bounded benchmark intake.',
        workflow_family_candidates=['generic-tabular-benchmark', 'literature-to-experiment'],
        notes=[],
        submitted_by='test-user',
    )


def build_interpretation_record() -> InterpretationRecord:
    return InterpretationRecord(
        interpretation_id='interpretation-internal-auth-1',
        intake_id='intake-internal-auth-1',
        created_at=now(),
        updated_at=now(),
        status='ready_for_assessment',
        source_type='paper-link',
        normalized_summary='Bounded benchmark intake.',
        extracted_method_summary='Interpreted intake as generic-tabular-benchmark.',
        literature_state_summary='Current bounded literature view: baseline comparison.',
        candidate_workflow_families=['generic-tabular-benchmark'],
        dataset_hints=['titanic'],
        evaluation_targets=['baseline comparison'],
        submitted_by='test-user',
    )


def test_internal_agent_headers_carry_content_type_and_token() -> None:
    headers = internal_agent_headers(build_settings())
    assert headers == {
        'Content-Type': 'application/json',
        INTERNAL_TOKEN_HEADER: INTERNAL_TOKEN,
    }


def test_internal_agent_headers_omit_token_when_unconfigured() -> None:
    headers = internal_agent_headers(Settings(registry_dir=REGISTRY_DIR))
    assert INTERNAL_TOKEN_HEADER not in headers
    assert headers == {'Content-Type': 'application/json'}


def test_call_interpretation_agent_sends_internal_token(monkeypatch) -> None:
    captured = capture_outbound_headers(monkeypatch, stage_interpretation)

    result = stage_interpretation.call_interpretation_agent(
        build_intake_record(),
        build_settings(interpretation_agent_enabled=True),
        WorkflowRegistry(REGISTRY_DIR),
        InMemoryRunStore(),
    )

    assert result is None
    assert len(captured) == 1
    assert header_value(captured[0], INTERNAL_TOKEN_HEADER) == INTERNAL_TOKEN


def test_call_intake_agent_sends_internal_token(monkeypatch) -> None:
    captured = capture_outbound_headers(monkeypatch, stage_inference)

    result = stage_inference.call_intake_agent(
        IntakeCreateRequest(
            raw_request='Read this paper and propose a bounded benchmark.',
            source_refs=['https://example.org/paper'],
        ),
        build_settings(intake_agent_enabled=True),
        WorkflowRegistry(REGISTRY_DIR),
    )

    assert result is None
    assert len(captured) == 1
    assert header_value(captured[0], INTERNAL_TOKEN_HEADER) == INTERNAL_TOKEN


def test_call_assessment_agent_sends_internal_token(monkeypatch) -> None:
    captured = capture_outbound_headers(monkeypatch, stage_design)

    result = stage_design.call_assessment_agent(
        build_interpretation_record(),
        build_settings(assessment_agent_enabled=True),
        WorkflowRegistry(REGISTRY_DIR),
    )

    assert result is None
    assert len(captured) == 1
    assert header_value(captured[0], INTERNAL_TOKEN_HEADER) == INTERNAL_TOKEN


def test_call_design_agent_sends_internal_token(monkeypatch) -> None:
    captured = capture_outbound_headers(monkeypatch, stage_design)
    registry = WorkflowRegistry(REGISTRY_DIR)
    workflow = registry.get_workflow('generic-tabular-benchmark')
    assert workflow is not None

    result = stage_design.call_design_agent(
        build_intake_record(),
        workflow,
        'test-user',
        build_settings(design_agent_enabled=True),
    )

    assert result is None
    assert len(captured) == 1
    assert header_value(captured[0], INTERNAL_TOKEN_HEADER) == INTERNAL_TOKEN


def test_call_problem_harvester_plan_sends_internal_token(monkeypatch) -> None:
    captured = capture_outbound_headers(monkeypatch, main_module)

    try:
        main_module.call_problem_harvester_plan(
            ResearchProblemPipelineRequest(
                problem_statement='Find bounded reproducibility papers for a cluster benchmark run.',
            ),
            build_settings(),
        )
    except URLError:
        pass
    else:
        raise AssertionError('capture stub must abort the outbound request')

    assert len(captured) == 1
    assert header_value(captured[0], INTERNAL_TOKEN_HEADER) == INTERNAL_TOKEN


def test_agent_calls_omit_internal_token_when_unconfigured(monkeypatch) -> None:
    captured = capture_outbound_headers(monkeypatch, stage_inference)

    result = stage_inference.call_intake_agent(
        IntakeCreateRequest(
            raw_request='Read this paper and propose a bounded benchmark.',
            source_refs=['https://example.org/paper'],
        ),
        Settings(registry_dir=REGISTRY_DIR, intake_agent_enabled=True),
        WorkflowRegistry(REGISTRY_DIR),
    )

    assert result is None
    assert len(captured) == 1
    assert header_value(captured[0], INTERNAL_TOKEN_HEADER) is None


def test_ranker_call_does_not_receive_the_internal_token(monkeypatch) -> None:
    captured = capture_outbound_headers(monkeypatch, stage_inference)

    result = stage_inference.reorder_intake_candidates_with_ranker(
        build_intake_record(),
        build_settings(ranker_enabled=True),
        WorkflowRegistry(REGISTRY_DIR),
    )

    # The ranker is not one of the four stage agents and does not validate the
    # token; the fallback keeps the original record when the stub fails.
    assert result.workflow_family_candidates == ['generic-tabular-benchmark', 'literature-to-experiment']
    assert len(captured) == 1
    assert header_value(captured[0], INTERNAL_TOKEN_HEADER) is None
