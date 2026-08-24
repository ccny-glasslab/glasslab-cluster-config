"""Deterministic assessment of verification-turn unresolved concerns.

The assessor must be a pure function of its inputs: the accept_final_report
idempotency key folds the recorded assessment into its sha256, so identical
inputs must always yield an identical assessment regardless of when it runs.
"""

from __future__ import annotations

import pytest

from app.schemas import AgentTurnResult, Claim, FindingClassification, TurnKind
from app.final_acceptance_assessment import build_final_acceptance_assessment


def _result(**overrides) -> AgentTurnResult:
    defaults: dict = {
        'kind': TurnKind.VERIFICATION,
        'summary': 'verified',
        'done': True,
    }
    defaults.update(overrides)
    return AgentTurnResult(**defaults)


RESOLVED_SETS = {
    'artifact_uris': {
        'artifact://run-1/metrics.json',
        'artifact://run-1/tables/comparison.csv',
    },
    'job_ids': {'job-1'},
    'knowledge_ids': {'src-1'},
    'evaluation_contract_id': 'wine-clustering-v1',
}


def test_clean_verification_yields_empty_unresolved() -> None:
    result = _result(
        claims=[
            Claim(
                text='job ran',
                evidence=['job://job-1'],
            ),
            Claim(
                text='metrics exist',
                evidence=['artifact://run-1/metrics.json'],
            ),
            Claim(
                text='contract bound',
                evidence=['contract://wine-clustering-v1'],
            ),
        ],
    )
    assessment = build_final_acceptance_assessment('turn-1', result, **RESOLVED_SETS)
    assert assessment.clean is True
    assert assessment.unresolved == []
    assert assessment.claim_count == 3


def test_claim_without_evidence_is_derived_missing_evidence() -> None:
    result = _result(claims=[Claim(text='the clusters are stable')])
    assessment = build_final_acceptance_assessment('turn-1', result, **RESOLVED_SETS)
    assert assessment.clean is False
    entry = assessment.unresolved[0]
    assert (
        entry.classification == FindingClassification.MISSING_EVIDENCE
    )
    assert entry.source == 'derived'
    assert 'stable' in entry.text


def test_broken_artifact_citation_is_derived_missing_evidence() -> None:
    result = _result(
        claims=[
            Claim(
                text='report exists',
                evidence=['artifact://run-1/reports/missing.md'],
            )
        ]
    )
    assessment = build_final_acceptance_assessment('turn-1', result, **RESOLVED_SETS)
    assert assessment.clean is False
    entry = assessment.unresolved[0]
    assert entry.classification == FindingClassification.MISSING_EVIDENCE
    assert 'missing.md' in entry.text


def test_contract_mismatch_is_derived_missing_evidence() -> None:
    result = _result(
        claims=[Claim(text='bound', evidence=['contract://other-contract'])]
    )
    assessment = build_final_acceptance_assessment('turn-1', result, **RESOLVED_SETS)
    assert assessment.clean is False
    assert (
        assessment.unresolved[0].classification
        == FindingClassification.MISSING_EVIDENCE
    )


def test_knowledge_citation_resolves_against_source_ids() -> None:
    result = _result(
        claims=[Claim(text='arbitration', evidence=['knowledge://src-1'])]
    )
    assessment = build_final_acceptance_assessment('turn-1', result, **RESOLVED_SETS)
    assert assessment.clean is True


def test_agent_findings_pass_through_verbatim() -> None:
    result = _result(
        findings=[
            {
                'classification': 'structured_contradiction',
                'text': 'csv says 0, metrics say 147',
                'evidence': ['artifact://run-1/tables/comparison.csv'],
            },
            {
                'classification': 'methodological_limitation',
                'text': 'only single linkage evaluated',
            },
        ]
    )
    assessment = build_final_acceptance_assessment('turn-1', result, **RESOLVED_SETS)
    assert assessment.clean is False
    assert [entry.source for entry in assessment.unresolved] == [
        'agent',
        'agent',
    ]
    by_classification = {
        entry.classification: entry for entry in assessment.unresolved
    }
    contradiction = by_classification[
        FindingClassification.STRUCTURED_CONTRADICTION
    ]
    limitation = by_classification[
        FindingClassification.METHODOLOGICAL_LIMITATION
    ]
    assert contradiction.evidence == ['artifact://run-1/tables/comparison.csv']
    assert contradiction.source == 'agent'
    assert limitation.source == 'agent'


def test_message_to_other_agent_on_done_turn_is_advisory_signal() -> None:
    result = _result(message_to_other_agent='Beaker should note X.')
    assessment = build_final_acceptance_assessment('turn-1', result, **RESOLVED_SETS)
    assert assessment.clean is False
    assert (
        assessment.unresolved[0].classification
        == FindingClassification.ADVISORY_DISAGREEMENT
    )


def test_git_and_event_schemes_pass_without_resolution_v1() -> None:
    # Deferred scheme coverage (v1): these citations resolve by grammar only.
    result = _result(
        claims=[
            Claim(text='commit', evidence=['git://abc123']),
            Claim(text='event', evidence=['event://seq/7']),
        ]
    )
    assessment = build_final_acceptance_assessment('turn-1', result, **RESOLVED_SETS)
    assert assessment.clean is True


def test_assessment_is_deterministic_for_identical_inputs() -> None:
    claims = [
        Claim(text='zeta claim'),
        Claim(text='alpha claim'),
        Claim(text='mid claim'),
    ]
    first = build_final_acceptance_assessment('turn-1', _result(claims=claims), **RESOLVED_SETS)
    second = build_final_acceptance_assessment(
        'turn-1', _result(claims=list(reversed(claims))), **RESOLVED_SETS
    )
    assert first.model_dump_json() == second.model_dump_json()


@pytest.mark.parametrize(
    'cited',
    ['artifact://run-1/metrics.json', 'artifact://artifact://run-1/metrics.json'],
)
def test_artifact_double_prefix_tolerance(cited: str) -> None:
    result = _result(claims=[Claim(text='m', evidence=[cited])])
    assessment = build_final_acceptance_assessment('turn-1', result, **RESOLVED_SETS)
    assert assessment.clean is True
