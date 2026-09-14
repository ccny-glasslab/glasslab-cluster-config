"""Deterministic action policy.

Every agent-requested action is classified here before it can reach a human,
and the classification (plus an idempotency key) is stored with the action so
approval decisions are replayable and auditable.
"""

from __future__ import annotations

from hashlib import sha256

from pydantic import ValidationError

from .contracts import ContractIntegrityError, reject_contract_overrides
from .matrix import canonical_json
from .schemas import (
    ActionRecord,
    AgentName,
    ApprovalStatus,
    ContractCandidateRequest,
    ExperimentMatrix,
    PolicyClassification,
    RequestedAction,
)


class ActionPolicy:
    # Actions with no cluster or external side effects need no human gate.
    AUTOMATIC_ACTIONS = {
        'read_workspace',
        'edit_workspace',
        'run_local_tests',
        'commit_experiment_branch',
    }
    # Externally visible or irreversible outcomes are gated on a human.
    HUMAN_ACTIONS = {
        'approve_protocol',
        'accept_final_report',
        'push_git_branch',
        'open_pull_request',
        'publish_report',
        'publish_external_artifact',
    }
    # Hard denies regardless of who proposes them.
    DENIED_ACTIONS = {
        'modify_evaluation_contract',
        'read_secrets',
        'delete_namespace',
        'delete_shared_resources',
        'raw_kubectl',
        'unrestricted_ssh',
        'registry_publish',
    }

    def __init__(
        self,
        *,
        permitted_images: list[str],
        maximum_cpu: float,
        maximum_memory_gib: float,
        maximum_gpus: int,
        maximum_parallel_jobs: int,
    ) -> None:
        self.permitted_images = set(permitted_images)
        self.maximum_cpu = maximum_cpu
        self.maximum_memory_gib = maximum_memory_gib
        self.maximum_gpus = maximum_gpus
        self.maximum_parallel_jobs = maximum_parallel_jobs

    def classify(
        self,
        *,
        proposed_by: AgentName,
        action: RequestedAction,
    ) -> PolicyClassification:
        return self.evaluate(
            proposed_by=proposed_by,
            action=action,
        )[0]

    def evaluate(
        self,
        *,
        proposed_by: AgentName,
        action: RequestedAction,
    ) -> tuple[PolicyClassification, str | None]:
        if action.type in self.DENIED_ACTIONS:
            return PolicyClassification.DENY, (
                f'action type {action.type!r} is denied'
            )
        if action.type == 'propose_evaluation_contract':
            if proposed_by != AgentName.BEAKER:
                return PolicyClassification.DENY, (
                    'only Beaker may propose an evaluation contract candidate'
                )
            try:
                ContractCandidateRequest.model_validate(action.arguments)
            except ValidationError as exc:
                return PolicyClassification.DENY, (
                    f'contract candidate schema validation failed: {exc}'
                )
            # Contract proposals are the one action type that legitimately names
            # contract files, so reject_contract_overrides is deliberately
            # bypassed only here; the candidate still goes through schema
            # validation, sealing, and Honeydew review before promotion.
            return PolicyClassification.HONEYDEW_AND_HUMAN_APPROVAL, None
        try:
            reject_contract_overrides(action.arguments)
        except ContractIntegrityError as exc:
            return PolicyClassification.DENY, str(exc)
        if action.type in self.AUTOMATIC_ACTIONS:
            return PolicyClassification.AUTOMATIC, None
        if action.type == 'draft_protocol':
            if proposed_by == AgentName.HONEYDEW:
                return PolicyClassification.AUTOMATIC, None
            return PolicyClassification.DENY, (
                'only Honeydew may draft or update program.md'
            )
        if action.type == 'submit_validation_job':
            # No executor branch exists for this type in
            # _resume_approved_action, so an approval would be recorded in the
            # authoritative log while nothing ran. Deny until one exists.
            return PolicyClassification.DENY, (
                'submit_validation_job has no executor and is denied until '
                'one exists'
            )
        if action.type == 'submit_experiment_matrix':
            try:
                matrix = ExperimentMatrix.model_validate(action.arguments)
            except ValidationError as exc:
                return PolicyClassification.DENY, (
                    f'experiment matrix schema validation failed: {exc}'
                )
            if matrix.runner_image not in self.permitted_images:
                allowed = ', '.join(sorted(self.permitted_images))
                return PolicyClassification.DENY, (
                    f'runner image {matrix.runner_image!r} is not permitted; '
                    f'allowed images: {allowed}'
                )
            resources = matrix.resources
            # Image allowlist and resource ceilings are enforced at classify
            # time, before anything is shown for approval, so a human can only
            # approve a matrix that already complies with the global limits
            # (the per-contract ceiling is enforced separately in matrix.py).
            if (
                resources.cpu > self.maximum_cpu
                or resources.memory_gib > self.maximum_memory_gib
                or resources.gpus > self.maximum_gpus
                or matrix.maximum_parallel_jobs > self.maximum_parallel_jobs
            ):
                return PolicyClassification.DENY, (
                    'requested resources exceed policy ceilings: '
                    f'cpu<={self.maximum_cpu}, '
                    f'memory_gib<={self.maximum_memory_gib}, '
                    f'gpus<={self.maximum_gpus}, '
                    f'maximum_parallel_jobs<={self.maximum_parallel_jobs}'
                )
            return PolicyClassification.HONEYDEW_AND_HUMAN_APPROVAL, None
        if action.type in self.HUMAN_ACTIONS:
            return PolicyClassification.HUMAN_APPROVAL, None
        return PolicyClassification.DENY, (
            f'action type {action.type!r} is not recognized'
        )

    def build_record(
        self,
        *,
        run_id: str,
        proposed_by: AgentName,
        action: RequestedAction,
        ordinal: int,
    ) -> ActionRecord:
        classification, policy_reason = self.evaluate(
            proposed_by=proposed_by,
            action=action,
        )
        status = (
            ApprovalStatus.AUTOMATICALLY_APPROVED
            if classification == PolicyClassification.AUTOMATIC
            else ApprovalStatus.DENIED
            if classification == PolicyClassification.DENY
            else ApprovalStatus.PENDING
        )
        idempotency_key = sha256(
            canonical_json(
                {
                    'run_id': run_id,
                    'proposed_by': proposed_by.value,
                    'type': action.type,
                    'arguments': action.arguments,
                    'ordinal': ordinal,
                }
            ).encode()
        ).hexdigest()
        # The ordinal is part of the idempotency key so the same action
        # proposed again in a later turn is a distinct record, not a deduped
        # replay of the earlier one.
        return ActionRecord(
            run_id=run_id,
            proposed_by=proposed_by,
            type=action.type,
            arguments=action.arguments,
            policy_classification=classification,
            approval_status=status,
            reason=(
                f'{action.reason} Policy denial: {policy_reason}'
                if policy_reason
                else action.reason
            ),
            idempotency_key=idempotency_key,
        )
