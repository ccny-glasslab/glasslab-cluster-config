"""Deterministic final-acceptance assessment of unresolved concerns.

Pure function of its explicit inputs: the accept_final_report idempotency
key folds the recorded assessment into its sha256, so identical inputs must
always produce byte-identical output.

v1 mechanical resolution scope: artifact, job, contract (id/version/digest
as encoded in the citation), knowledge sources, and context packets. Claims
citing git:// or event:// are reported as unchecked_citations — explicitly
not resolved, and not counted against cleanliness — because v1 has no
authoritative projection to check them against.

The assessment spans two turns: the latest completed Honeydew verification
turn and the promoted final-report turn. Every finding records which turn it
came from; a rewritten report is mechanically re-assessed but never claimed
to be re-verified.
"""

from __future__ import annotations

import pytest

from app.final_acceptance_assessment import (
    build_final_acceptance_assessment,
)
from app.schemas import (
    AgentTurnResult,
    Claim,
    FindingClassification,
    TurnFinding,
    TurnKind,
)


RESOLVED = {
    'artifact_uris': {
        'artifact://run-1/metrics.json',
        'artifact://run-1/tables/comparison.csv',
    },
    'job_ids': {'job-1'},
    'knowledge_source_ids': {'src-1'},
    'context_packet_ids': {'pkt-7'},
    'evaluation_contract_id': 'wine-clustering-v1',
    'evaluation_contract_version': '1.0.0',
    'evaluation_contract_digest': 'a' * 64,
}


def _verification(**overrides) -> AgentTurnResult:
    defaults: dict = {
        'kind': TurnKind.VERIFICATION,
        'summary': 'verified',
        'done': True,
    }
    defaults.update(overrides)
    return AgentTurnResult(**defaults)


def _report(**overrides) -> AgentTurnResult:
    defaults: dict = {
        'kind': TurnKind.FINAL_REPORT,
        'summary': 'report written',
        'done': True,
    }
    defaults.update(overrides)
    return AgentTurnResult(**defaults)


def _build(verification=None, report=None, **overrides):
    sets = {**RESOLVED, **overrides}
    return build_final_acceptance_assessment(
        verification_turn_id='turn-v',
        verification_result=verification if verification is not None else _verification(),
        final_report_turn_id='turn-r',
        final_report_result=report,
        **sets,
    )


def classifications(assessment):
    return [entry.classification for entry in assessment.unresolved]


def test_clean_verification_yields_empty_unresolved_and_digest() -> None:
    verification = _verification(
        claims=[
            Claim(text='job ran', evidence=['job://job-1']),
            Claim(
                text='contract bound',
                evidence=['contract://wine-clustering-v1/1.0.0@' + 'a' * 64],
            ),
            Claim(text='packet', evidence=['knowledge://context:pkt-7']),
            Claim(text='source doc', evidence=['knowledge://src-1']),
        ]
    )
    assessment = _build(verification=verification)
    assert assessment.clean is True
    assert assessment.unresolved == []
    assert assessment.unchecked_citations == []
    assert len(assessment.assessment_digest) == 64


def test_full_contract_citation_mismatch_fails_each_component() -> None:
    wrong_version = _verification(
        claims=[
            Claim(
                text='bound',
                evidence=['contract://wine-clustering-v1/2.0.0@' + 'a' * 64],
            )
        ]
    )
    wrong_digest = _verification(
        claims=[
            Claim(
                text='bound',
                evidence=['contract://wine-clustering-v1/1.0.0@' + 'b' * 64],
            )
        ]
    )
    wrong_id = _verification(
        claims=[Claim(text='bound', evidence=['contract://other-contract'])]
    )
    exact_id_only = _verification(
        claims=[Claim(text='bound', evidence=['contract://wine-clustering-v1'])]
    )
    for broken in (wrong_version, wrong_digest, wrong_id):
        assessment = _build(verification=broken)
        assert (
            FindingClassification.MISSING_EVIDENCE
            in classifications(assessment)
        )
    assert _build(verification=exact_id_only).clean is True


def test_knowledge_context_packet_resolution() -> None:
    valid_packet = _verification(
        claims=[Claim(text='grounding', evidence=['knowledge://context:pkt-7'])]
    )
    missing_packet = _verification(
        claims=[
            Claim(text='grounding', evidence=['knowledge://context:pkt-404'])
        ]
    )
    missing_source = _verification(
        claims=[Claim(text='doc', evidence=['knowledge://src-404'])]
    )
    assert _build(verification=valid_packet).clean is True
    for broken in (missing_packet, missing_source):
        assessment = _build(verification=broken)
        assert (
            FindingClassification.MISSING_EVIDENCE
            in classifications(assessment)
        )


def test_git_and_event_citations_are_disclosed_as_unchecked() -> None:
    verification = _verification(
        claims=[
            Claim(text='commit', evidence=['git://abc123']),
            Claim(text='event', evidence=['event://seq/7']),
        ]
    )
    assessment = _build(verification=verification)
    # v1 has no authoritative projection for these schemes: grammar-valid
    # citations are neither called resolved nor marked unresolved. They are
    # disclosed so readers know what was not mechanically checked.
    assert assessment.clean is True
    assert assessment.unresolved == []
    assert set(assessment.unchecked_citations) == {
        'git://abc123',
        'event://seq/7',
    }


def test_claim_without_evidence_is_derived_missing_evidence() -> None:
    assessment = _build(
        verification=_verification(claims=[Claim(text='the clusters are stable')])
    )
    entry = assessment.unresolved[0]
    assert entry.classification == FindingClassification.MISSING_EVIDENCE
    assert entry.source == 'derived'
    assert entry.source_turn_kind == 'verification'
    assert entry.source_turn_id == 'turn-v'


def test_agent_findings_pass_through_with_turn_provenance() -> None:
    verification = _verification(
        findings=[
            {
                'classification': 'structured_contradiction',
                'text': 'csv says 0, metrics say 147',
                'evidence': ['artifact://run-1/tables/comparison.csv'],
            }
        ]
    )
    report = _report(
        claims=[
            Claim(
                text='report cites metrics',
                evidence=['artifact://run-1/metrics.json'],
            )
        ],
        findings=[
            {
                'classification': 'methodological_limitation',
                'text': 'single exploratory execution only',
            }
        ],
    )
    assessment = _build(verification=verification, report=report)
    by_text = {entry.text: entry for entry in assessment.unresolved}
    contradiction = by_text['csv says 0, metrics say 147']
    limitation = by_text['single exploratory execution only']
    assert contradiction.source == 'agent'
    assert contradiction.source_turn_id == 'turn-v'
    assert contradiction.source_turn_kind == 'verification'
    assert limitation.source_turn_id == 'turn-r'
    assert limitation.source_turn_kind == 'final_report'
    assert assessment.verification_turn_id == 'turn-v'
    assert assessment.final_report_turn_id == 'turn-r'


def test_mechanical_checks_cover_final_report_claims_too() -> None:
    report = _report(
        claims=[Claim(text='dangling report claim', evidence=[])]
    )
    assessment = _build(report=report)
    entry = assessment.unresolved[0]
    assert entry.classification == FindingClassification.MISSING_EVIDENCE
    assert entry.source_turn_kind == 'final_report'


def test_message_to_other_agent_never_creates_findings() -> None:
    result = _verification(message_to_other_agent='Looks good; include X.')
    assessment = _build(verification=result)
    assert assessment.clean is True


def test_long_finding_text_is_preserved_in_full() -> None:
    long_text = 'x' * 500
    verification = _verification(
        findings=[
            {'classification': 'advisory_disagreement', 'text': long_text}
        ]
    )
    assessment = _build(verification=verification)
    assert assessment.unresolved[0].text == long_text


def test_assessment_is_deterministic_for_identical_inputs() -> None:
    def make(claim_order):
        return _build(
            verification=_verification(
                claims=[Claim(text=t) for t in claim_order]
            ),
            report=_report(),
        )

    first = make(('zeta claim', 'alpha claim'))
    second = make(('alpha claim', 'zeta claim'))
    assert first.model_dump_json() == second.model_dump_json()
