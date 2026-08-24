"""Deterministic assessment of unresolved concerns in a verification turn.

Pure function of its explicit inputs: the accept_final_report idempotency key
folds the recorded assessment into its sha256, so the same turn and store
state must always produce byte-identical output.

v1 resolution scope is deliberately narrow to avoid false positives:
artifact, job, contract and knowledge citations resolve against store state;
empty citations and agent-declared findings are carried verbatim; git:// and
event:// citations resolve by grammar only until a real-run URI corpus
motivates mechanical coverage.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .schemas import (
    AgentTurnResult,
    FindingClassification,
    TurnFinding,
)


class UnresolvedFinding(BaseModel):
    model_config = ConfigDict(extra='forbid')

    classification: FindingClassification
    text: str = Field(min_length=1)
    source: Literal['agent', 'derived']
    evidence: list[str] = []


class VerificationAssessment(BaseModel):
    model_config = ConfigDict(extra='forbid')

    turn_id: str
    done: bool
    claim_count: int
    clean: bool
    unresolved: list[UnresolvedFinding]


_MAX_TEXT = 200


def _derived(
    classification: FindingClassification,
    text: str,
    evidence: list[str] | None = None,
) -> UnresolvedFinding:
    return UnresolvedFinding(
        classification=classification,
        text=text[:_MAX_TEXT],
        source='derived',
        evidence=evidence or [],
    )


def _agent(finding: TurnFinding) -> UnresolvedFinding:
    return UnresolvedFinding(
        classification=finding.classification,
        text=finding.text[:_MAX_TEXT],
        source='agent',
        evidence=list(finding.evidence),
    )


def assess_verification(
    turn_id: str,
    result: AgentTurnResult,
    *,
    artifact_uris: set[str],
    job_ids: set[str],
    knowledge_ids: set[str],
    evaluation_contract_id: str | None,
) -> VerificationAssessment:
    unresolved: list[UnresolvedFinding] = []

    for claim in result.claims:
        if not claim.evidence:
            unresolved.append(
                _derived(
                    FindingClassification.MISSING_EVIDENCE,
                    f'claim cites no durable evidence: "{claim.text}"',
                )
            )
            continue
        for uri in claim.evidence:
            if _resolves(uri, artifact_uris, job_ids, knowledge_ids, evaluation_contract_id):
                continue
            unresolved.append(
                _derived(
                    FindingClassification.MISSING_EVIDENCE,
                    f'cited evidence does not resolve: {uri}',
                    [uri],
                )
            )

    for finding in result.findings:
        unresolved.append(_agent(finding))

    if result.done and result.message_to_other_agent.strip():
        unresolved.append(
            _derived(
                FindingClassification.ADVISORY_DISAGREEMENT,
                f'verification left a message for the other agent: '
                f'"{result.message_to_other_agent.strip()}"',
            )
        )

    unresolved.sort(key=lambda entry: (entry.classification.value, entry.text))
    return VerificationAssessment(
        turn_id=turn_id,
        done=result.done,
        claim_count=len(result.claims),
        clean=not unresolved,
        unresolved=unresolved,
    )


def _resolves(
    uri: str,
    artifact_uris: set[str],
    job_ids: set[str],
    knowledge_ids: set[str],
    evaluation_contract_id: str | None,
) -> bool:
    scheme, separator, rest = uri.partition('://')
    if not separator:
        return False
    if scheme == 'artifact':
        # Tolerate a doubled scheme prefix seen in earlier citation drift.
        normalized = rest.removeprefix('artifact://')
        return f'artifact://{normalized}' in artifact_uris
    if scheme == 'job':
        return rest.split('/', 1)[0] in job_ids
    if scheme == 'contract':
        contract_id = evaluation_contract_id or ''
        return bool(contract_id) and rest.split('/', 1)[0] == contract_id
    if scheme == 'knowledge':
        return rest.split('/', 1)[0] in knowledge_ids
    # git:// and event:// are grammar-valid but mechanically unverifiable in
    # v1; they count as resolved rather than risk systematic false positives.
    return True
