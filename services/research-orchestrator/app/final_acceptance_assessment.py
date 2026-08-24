"""Deterministic final-acceptance assessment of unresolved concerns.

Pure function of its explicit inputs: the accept_final_report idempotency
key folds the recorded assessment into its sha256, so identical inputs must
always produce byte-identical output.

v1 mechanical resolution scope: artifact and job identifiers, contracts
cited as ``contract://<id>[/<version>][@<digest>]`` where any encoded
version or digest must match the run's bound contract exactly, knowledge
sources, and context packets (``knowledge://context:<packet-id>``).
Grammar-valid citations under git:// or event:// have no authoritative v1
projection to check against: they are disclosed as ``unchecked_citations``,
which neither satisfies a claim nor marks it unresolved.

The assessment spans the latest completed Honeydew verification turn and
the promoted final-report turn. Every finding records its source turn so a
rewritten report is never presented as re-verified.
"""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .schemas import (
    AgentTurnResult,
    FindingClassification,
)


class UnresolvedFinding(BaseModel):
    model_config = ConfigDict(extra='forbid')

    classification: FindingClassification
    text: str = Field(min_length=1)
    source: Literal['agent', 'derived']
    evidence: list[str] = []
    source_turn_id: str | None = None
    source_turn_kind: Literal['verification', 'final_report'] | None = None


class FinalAcceptanceAssessment(BaseModel):
    model_config = ConfigDict(extra='forbid')

    verification_turn_id: str
    final_report_turn_id: str
    done: bool
    claim_count: int
    clean: bool
    unresolved: list[UnresolvedFinding]
    unchecked_citations: list[str]
    assessment_digest: str


_RESOLUTION = Literal['resolved', 'unchecked', 'unresolved']

_UNCHECKED_SCHEMES = ('git', 'event')


def _resolve_citation(
    uri: str,
    *,
    artifact_uris: set[str],
    job_ids: set[str],
    knowledge_source_ids: set[str],
    context_packet_ids: set[str],
    evaluation_contract_id: str | None,
    evaluation_contract_version: str | None,
    evaluation_contract_digest: str | None,
) -> _RESOLUTION:
    scheme, separator, rest = uri.partition('://')
    if not separator:
        return 'unresolved'
    if scheme == 'artifact':
        normalized = rest.removeprefix('artifact://')
        return (
            'resolved'
            if f'artifact://{normalized}' in artifact_uris
            else 'unresolved'
        )
    if scheme == 'job':
        return (
            'resolved'
            if rest.split('/', 1)[0] in job_ids
            else 'unresolved'
        )
    if scheme == 'knowledge':
        packet_id = rest.removeprefix('context:')
        looked_up = (
            packet_id if rest.startswith('context:') else rest
        ).split('/', 1)[0]
        pool = context_packet_ids if rest.startswith('context:') else knowledge_source_ids
        return 'resolved' if looked_up in pool else 'unresolved'
    if scheme == 'contract':
        body, at_separator, digest_part = rest.rpartition('@')
        candidate = body if at_separator else rest
        if at_separator and (
            not evaluation_contract_digest
            or digest_part != evaluation_contract_digest
        ):
            return 'unresolved'
        segments = candidate.split('/')
        if segments[0] != (evaluation_contract_id or ''):
            return 'unresolved'
        if len(segments) > 1 and segments[1] != (
            evaluation_contract_version or ''
        ):
            return 'unresolved'
        return 'resolved'
    if scheme in _UNCHECKED_SCHEMES:
        return 'unchecked'
    return 'unresolved'


def _canonical_digest(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(canonical.encode()).hexdigest()


def build_final_acceptance_assessment(
    *,
    verification_turn_id: str,
    verification_result: AgentTurnResult | None,
    final_report_turn_id: str = '',
    final_report_result: AgentTurnResult | None = None,
    artifact_uris: set[str],
    job_ids: set[str],
    knowledge_source_ids: set[str],
    context_packet_ids: set[str],
    evaluation_contract_id: str | None,
    evaluation_contract_version: str | None,
    evaluation_contract_digest: str | None,
) -> FinalAcceptanceAssessment:
    spans: list[tuple[Literal['verification', 'final_report'], str, AgentTurnResult]] = [
        ('verification', verification_turn_id, verification_result),
        ('final_report', final_report_turn_id, final_report_result),
    ]
    unresolved: list[UnresolvedFinding] = []
    unchecked: set[str] = set()
    claim_count = 0

    resolution_inputs = dict(
        artifact_uris=artifact_uris,
        job_ids=job_ids,
        knowledge_source_ids=knowledge_source_ids,
        context_packet_ids=context_packet_ids,
        evaluation_contract_id=evaluation_contract_id,
        evaluation_contract_version=evaluation_contract_version,
        evaluation_contract_digest=evaluation_contract_digest,
    )

    for turn_kind, turn_id, result in spans:
        if result is None:
            continue
        claim_count += len(result.claims)
        for claim in result.claims:
            if not claim.evidence:
                unresolved.append(
                    UnresolvedFinding(
                        classification=(
                            FindingClassification.MISSING_EVIDENCE
                        ),
                        text=(
                            'claim cites no durable evidence: '
                            f'"{claim.text}"'
                        ),
                        source='derived',
                        source_turn_id=turn_id,
                        source_turn_kind=turn_kind,
                    )
                )
                continue
            for uri in claim.evidence:
                outcome = _resolve_citation(uri, **resolution_inputs)
                if outcome == 'resolved':
                    continue
                if outcome == 'unchecked':
                    unchecked.add(uri)
                    continue
                unresolved.append(
                    UnresolvedFinding(
                        classification=(
                            FindingClassification.MISSING_EVIDENCE
                        ),
                        text=f'cited evidence does not resolve: {uri}',
                        source='derived',
                        evidence=[uri],
                        source_turn_id=turn_id,
                        source_turn_kind=turn_kind,
                    )
                )
        for finding in result.findings:
            unresolved.append(
                UnresolvedFinding(
                    classification=finding.classification,
                    text=finding.text,
                    source='agent',
                    evidence=list(finding.evidence),
                    source_turn_id=turn_id,
                    source_turn_kind=turn_kind,
                )
            )

    unresolved.sort(key=lambda entry: (entry.classification.value, entry.text))
    ordered_unchecked = sorted(unchecked)
    verification_done = bool(
        verification_result.done if verification_result else False
    )
    assessment = FinalAcceptanceAssessment(
        verification_turn_id=verification_turn_id,
        final_report_turn_id=final_report_turn_id,
        done=verification_done,
        claim_count=claim_count,
        clean=not unresolved,
        unresolved=unresolved,
        unchecked_citations=ordered_unchecked,
        assessment_digest='',
    )
    digest_payload = {
        'verification_turn_id': assessment.verification_turn_id,
        'final_report_turn_id': assessment.final_report_turn_id,
        'done': assessment.done,
        'claim_count': assessment.claim_count,
        'unresolved': [
            entry.model_dump(mode='json') for entry in assessment.unresolved
        ],
        'unchecked_citations': assessment.unchecked_citations,
    }
    return assessment.model_copy(
        update={'assessment_digest': _canonical_digest(digest_payload)}
    )
