"""HTTP routes for the structured scientific investigation lifecycle.

Models a formal hypothesis-driven workflow: create an investigation with
research question and hypotheses, add plans with bounded execution specs,
approve a plan (freezing its snapshot), launch runs against the approved plan
with dependency ordering and data-access scoping, and record evidence-backed
claims. Confirmatory investigations freeze hypotheses after plan approval and
reject plan replacement after execution begins. Claim recording validates the
full evidence chain: run membership, terminal state, artifact digest integrity,
and evaluator contract conformance.
"""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
from typing import Any, Callable
from uuid import uuid4

from fastapi import FastAPI, HTTPException, status

from .config import Settings
from .execution_preflight import build_execution_preflight_result
from .persistence import RunStore
from .registry import WorkflowRegistry
from .run_artifacts import artifact_run_dir, file_sha256
from .schemas import (
    GenericExperimentRunRequest,
    InvestigationClaimCreateRequest,
    InvestigationClaimRecord,
    InvestigationContextResponse,
    InvestigationCreateRequest,
    InvestigationEvidenceReference,
    InvestigationExecutionSpec,
    InvestigationHypothesisCreateRequest,
    InvestigationHypothesisRecord,
    InvestigationPlanApprovalRecord,
    InvestigationPlanApproveRequest,
    InvestigationPlanCreateRequest,
    InvestigationPlanRecord,
    InvestigationPlanSnapshot,
    InvestigationRecord,
    InvestigationRunCreateRequest,
    InvestigationRunResponse,
    RunRecord,
)


TERMINAL_RUN_STATES = {'succeeded', 'failed', 'rejected'}


def evaluator_contract_issues(
    metrics: dict[str, Any],
    execution: InvestigationExecutionSpec,
) -> list[str]:
    contract = execution.evaluator_contract
    issues: list[str] = []
    if contract.primary_metric is not None:
        name = contract.primary_metric.name
        value = metrics.get(name)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            issues.append(f'primary metric is missing or non-numeric: {name}')
    for guardrail in contract.guardrails:
        value = metrics.get(guardrail.name)
        if value is None:
            if guardrail.required:
                issues.append(f'required guardrail metric is missing: {guardrail.name}')
            continue
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            issues.append(f'guardrail metric is non-numeric: {guardrail.name}')
            continue
        numeric_value = float(value)
        if guardrail.minimum is not None and numeric_value < guardrail.minimum:
            issues.append(
                f'guardrail {guardrail.name} is below minimum {guardrail.minimum}'
            )
        if guardrail.maximum is not None and numeric_value > guardrail.maximum:
            issues.append(
                f'guardrail {guardrail.name} is above maximum {guardrail.maximum}'
            )
    return issues


def build_plan_snapshot(
    investigation: InvestigationRecord,
    plan: InvestigationPlanRecord,
) -> InvestigationPlanSnapshot:
    return InvestigationPlanSnapshot(
        investigation_id=investigation.investigation_id,
        research_mode=investigation.research_mode,
        research_question=investigation.research_question,
        hypotheses=list(investigation.hypotheses),
        plan=plan,
    )


def build_plan_sha256(
    investigation: InvestigationRecord,
    plan: InvestigationPlanRecord,
) -> str:
    snapshot = build_plan_snapshot(investigation, plan)
    canonical = json.dumps(
        snapshot.model_dump(mode='json'),
        sort_keys=True,
        separators=(',', ':'),
    ).encode('utf-8')
    return sha256(canonical).hexdigest()


def register_investigation_routes(
    app: FastAPI,
    *,
    settings: Settings,
    registry: WorkflowRegistry,
    store: RunStore,
    launch_experiment_run: Callable[..., RunRecord],
) -> None:
    def get_required_investigation(investigation_id: str) -> InvestigationRecord:
        record = store.get_investigation(investigation_id)
        if record is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail='investigation not found',
            )
        return record

    def get_plan(
        investigation: InvestigationRecord,
        plan_id: str,
    ) -> InvestigationPlanRecord:
        plan = next(
            (candidate for candidate in investigation.plans if candidate.plan_id == plan_id),
            None,
        )
        if plan is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail='investigation plan not found',
            )
        return plan

    def get_approval(
        investigation: InvestigationRecord,
        approval_id: str,
    ) -> InvestigationPlanApprovalRecord:
        approval = next(
            (
                candidate
                for candidate in investigation.plan_approvals
                if candidate.approval_id == approval_id
            ),
            None,
        )
        if approval is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail='investigation plan approval not found',
            )
        return approval

    def get_execution(
        plan: InvestigationPlanRecord,
        execution_id: str,
    ) -> InvestigationExecutionSpec:
        execution = next(
            (
                candidate
                for candidate in plan.executions
                if candidate.execution_id == execution_id
            ),
            None,
        )
        if execution is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail='investigation plan execution not found',
            )
        return execution

    def get_active_approval(
        investigation: InvestigationRecord,
    ) -> InvestigationPlanApprovalRecord | None:
        if investigation.active_plan_approval_id is None:
            return None
        return next(
            (
                approval
                for approval in investigation.plan_approvals
                if approval.approval_id == investigation.active_plan_approval_id
            ),
            None,
        )

    def validate_plan_hypotheses(
        investigation: InvestigationRecord,
        hypothesis_ids: list[str],
    ) -> None:
        known_hypothesis_ids = {
            hypothesis.hypothesis_id
            for hypothesis in investigation.hypotheses
        }
        unknown = [
            hypothesis_id
            for hypothesis_id in hypothesis_ids
            if hypothesis_id not in known_hypothesis_ids
        ]
        if unknown:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    'message': 'plan references hypotheses outside this investigation',
                    'hypothesis_ids': unknown,
                },
            )

    def validate_execution_contract(execution: InvestigationExecutionSpec) -> None:
        workflow = registry.get_workflow(execution.workload_id)
        if workflow is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail='plan workload definition not found',
            )
        if workflow.experiment_type and workflow.experiment_type != execution.experiment_type:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f'workload {workflow.workflow_id} requires experiment_type '
                    f'{workflow.experiment_type}'
                ),
            )
        preflight = build_execution_preflight_result(workflow, settings)
        if not preflight.ready:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    'message': 'plan execution preflight failed',
                    'workload_id': execution.workload_id,
                    'blocking_issues': preflight.blocking_issues,
                    'warnings': preflight.warnings,
                },
            )
        registry_required = set(workflow.expected_artifacts.required)
        plan_required = set(execution.artifact_contract.required)
        missing = sorted(registry_required - plan_required)
        if missing:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    'message': 'plan artifact contract omits workload-required artifacts',
                    'missing_artifacts': missing,
                },
            )

    @app.post(
        '/investigations',
        response_model=InvestigationRecord,
        status_code=status.HTTP_201_CREATED,
    )
    def create_investigation(request: InvestigationCreateRequest) -> InvestigationRecord:
        submitted_by = request.submitted_by or settings.default_submitted_by
        now = datetime.now(timezone.utc)
        title = request.title or request.research_question[:80].rstrip(' .,:;')
        record = InvestigationRecord(
            investigation_id=uuid4().hex,
            created_at=now,
            updated_at=now,
            status='planning',
            title=title,
            research_question=request.research_question,
            research_mode=request.research_mode,
            priorities=request.priorities,
            hypotheses=[
                InvestigationHypothesisRecord(
                    hypothesis_id=uuid4().hex,
                    statement=statement,
                    created_at=now,
                    submitted_by=submitted_by,
                )
                for statement in request.hypotheses
            ],
            submitted_by=submitted_by,
        )
        store.save_investigation(record)
        return record

    @app.get('/investigations', response_model=list[InvestigationRecord])
    def list_investigations() -> list[InvestigationRecord]:
        return store.list_investigations()

    @app.get('/investigations/latest', response_model=InvestigationRecord)
    def get_latest_investigation() -> InvestigationRecord:
        record = store.get_latest_investigation()
        if record is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail='no investigation has been created yet',
            )
        return record

    @app.get('/investigations/{investigation_id}', response_model=InvestigationRecord)
    def get_investigation(investigation_id: str) -> InvestigationRecord:
        return get_required_investigation(investigation_id)

    @app.get(
        '/investigations/{investigation_id}/context',
        response_model=InvestigationContextResponse,
    )
    def get_investigation_context(
        investigation_id: str,
    ) -> InvestigationContextResponse:
        investigation = get_required_investigation(investigation_id)
        approval = get_active_approval(investigation)
        runs = [
            run
            for run_id in investigation.run_ids
            if (run := store.get_run(run_id)) is not None
        ]
        return InvestigationContextResponse(
            investigation=investigation,
            current_plan=investigation.plans[-1] if investigation.plans else None,
            approved_plan=approval.plan_snapshot.plan if approval is not None else None,
            runs=runs,
        )

    @app.post(
        '/investigations/{investigation_id}/hypotheses',
        response_model=InvestigationRecord,
        status_code=status.HTTP_201_CREATED,
    )
    def add_investigation_hypothesis(
        investigation_id: str,
        request: InvestigationHypothesisCreateRequest,
    ) -> InvestigationRecord:
        investigation = get_required_investigation(investigation_id)
        if (
            investigation.research_mode == 'confirmatory'
            and investigation.active_plan_approval_id is not None
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail='confirmatory hypotheses are frozen after plan approval',
            )
        if any(
            hypothesis.statement == request.statement
            for hypothesis in investigation.hypotheses
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail='hypothesis already exists in this investigation',
            )

        now = datetime.now(timezone.utc)
        updated = investigation.model_copy(
            update={
                'updated_at': now,
                'status': 'planning',
                'hypotheses': [
                    *investigation.hypotheses,
                    InvestigationHypothesisRecord(
                        hypothesis_id=uuid4().hex,
                        statement=request.statement,
                        created_at=now,
                        submitted_by=(
                            request.submitted_by
                            or settings.default_submitted_by
                        ),
                    ),
                ],
                'active_plan_approval_id': None,
            }
        )
        store.save_investigation(updated)
        return updated

    @app.post(
        '/investigations/{investigation_id}/plans',
        response_model=InvestigationPlanRecord,
        status_code=status.HTTP_201_CREATED,
    )
    def create_investigation_plan(
        investigation_id: str,
        request: InvestigationPlanCreateRequest,
    ) -> InvestigationPlanRecord:
        investigation = get_required_investigation(investigation_id)
        validate_plan_hypotheses(investigation, request.hypothesis_ids)
        now = datetime.now(timezone.utc)
        plan = InvestigationPlanRecord(
            plan_id=uuid4().hex,
            revision=len(investigation.plans) + 1,
            created_at=now,
            submitted_by=request.submitted_by or settings.default_submitted_by,
            title=request.title,
            rationale=request.rationale,
            hypothesis_ids=request.hypothesis_ids,
            executions=request.executions,
        )
        updated = investigation.model_copy(
            update={
                'updated_at': now,
                'status': 'planning',
                'plans': [*investigation.plans, plan],
            }
        )
        store.save_investigation(updated)
        return plan

    @app.post(
        '/investigations/{investigation_id}/plan-approvals',
        response_model=InvestigationRecord,
    )
    def approve_investigation_plan(
        investigation_id: str,
        request: InvestigationPlanApproveRequest,
    ) -> InvestigationRecord:
        investigation = get_required_investigation(investigation_id)
        if not investigation.hypotheses:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail='investigation needs at least one hypothesis before plan approval',
            )
        if investigation.research_mode == 'confirmatory' and investigation.run_ids:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail='a confirmatory plan cannot be replaced after execution begins',
            )

        plan = get_plan(investigation, request.plan_id)
        validate_plan_hypotheses(investigation, plan.hypothesis_ids)
        for execution in plan.executions:
            validate_execution_contract(execution)
        plan_snapshot = build_plan_snapshot(investigation, plan)
        plan_sha256 = build_plan_sha256(investigation, plan)
        active_approval = get_active_approval(investigation)
        if (
            active_approval is not None
            and active_approval.plan_id == plan.plan_id
            and active_approval.plan_sha256 == plan_sha256
        ):
            # Re-approving an unchanged plan is a no-op; the existing
            # approval record is the authoritative snapshot.
            return investigation

        now = datetime.now(timezone.utc)
        approval = InvestigationPlanApprovalRecord(
            approval_id=uuid4().hex,
            plan_id=plan.plan_id,
            plan_sha256=plan_sha256,
            approved_at=now,
            approved_by=request.approved_by or settings.default_submitted_by,
            hypothesis_ids=list(plan.hypothesis_ids),
            research_mode=investigation.research_mode,
            plan_snapshot=plan_snapshot,
            note=request.note,
        )
        updated = investigation.model_copy(
            update={
                'updated_at': now,
                'status': 'approved',
                'plan_approvals': [*investigation.plan_approvals, approval],
                'active_plan_approval_id': approval.approval_id,
            }
        )
        store.save_investigation(updated)
        return updated

    @app.post(
        '/investigations/{investigation_id}/runs',
        response_model=InvestigationRunResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def launch_investigation_run(
        investigation_id: str,
        request: InvestigationRunCreateRequest,
    ) -> InvestigationRunResponse:
        investigation = get_required_investigation(investigation_id)
        if investigation.active_plan_approval_id is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail='investigation has no active plan approval',
            )
        if request.approval_id != investigation.active_plan_approval_id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail='requested approval is not the active investigation plan',
            )
        approval = get_approval(investigation, request.approval_id)
        plan = get_plan(investigation, approval.plan_id)
        execution = get_execution(plan, request.execution_id)
        current_sha256 = build_plan_sha256(investigation, plan)
        if current_sha256 != approval.plan_sha256:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail='approved plan snapshot no longer matches the current investigation state',
            )

        investigation_runs = [
            store.get_run(run_id)
            for run_id in investigation.run_ids
        ]
        unsatisfied_dependencies: list[str] = []
        for dependency_id in execution.depends_on:
            if not any(
                run is not None
                and run.source_approval_id == approval.approval_id
                and run.source_execution_id == dependency_id
                and run.status.status == 'succeeded'
                for run in investigation_runs
            ):
                unsatisfied_dependencies.append(dependency_id)
        if unsatisfied_dependencies:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    'message': 'plan execution dependencies are not satisfied',
                    'execution_id': execution.execution_id,
                    'dependencies': unsatisfied_dependencies,
                },
            )

        visible_bindings = [
            binding
            for binding in execution.dataset_bindings
            if execution.data_access_scope in binding.access_scopes
        ]
        run_request = GenericExperimentRunRequest(
            objective=execution.objective,
            experiment_type=execution.experiment_type,
            workload_id=execution.workload_id,
            config_payload={
                **execution.config_payload,
                'workspace': execution.workspace.model_dump(mode='json'),
                'dataset_contracts': [
                    binding.model_dump(mode='json')
                    for binding in visible_bindings
                ],
                'investigation': {
                    'investigation_id': investigation.investigation_id,
                    'plan_id': plan.plan_id,
                    'approval_id': approval.approval_id,
                    'execution_id': execution.execution_id,
                    'plan_sha256': approval.plan_sha256,
                },
            },
            dataset_bindings={
                binding.name: binding.asset.uri
                for binding in visible_bindings
            },
            budget=execution.budget.model_dump(mode='json', exclude_none=True),
            artifact_contract=execution.artifact_contract,
            metric_contract=execution.evaluator_contract.model_dump(
                mode='json',
                exclude_none=True,
            ),
            submitted_by=plan.submitted_by,
        )
        run = launch_experiment_run(
            run_request,
            investigation_id=investigation.investigation_id,
            source_plan_id=plan.plan_id,
            source_approval_id=approval.approval_id,
            source_execution_id=execution.execution_id,
            plan_sha256=approval.plan_sha256,
            idempotency_key=(
                f'investigation:{approval.approval_id}:{execution.execution_id}'
            ),
        )
        now = datetime.now(timezone.utc)
        run_ids = (
            [*investigation.run_ids, run.run_id]
            if run.run_id not in investigation.run_ids
            else list(investigation.run_ids)
        )
        updated = investigation.model_copy(
            update={
                'updated_at': now,
                'status': 'running',
                'run_ids': run_ids,
            }
        )
        store.save_investigation(updated)
        return InvestigationRunResponse(
            investigation=updated,
            approval=approval,
            plan=plan,
            execution=execution,
            run=run,
        )

    @app.post(
        '/investigations/{investigation_id}/claims',
        response_model=InvestigationClaimRecord,
        status_code=status.HTTP_201_CREATED,
    )
    def record_investigation_claim(
        investigation_id: str,
        request: InvestigationClaimCreateRequest,
    ) -> InvestigationClaimRecord:
        investigation = get_required_investigation(investigation_id)
        known_hypothesis_ids = {
            hypothesis.hypothesis_id
            for hypothesis in investigation.hypotheses
        }
        unknown_hypothesis_ids = [
            hypothesis_id
            for hypothesis_id in request.hypothesis_ids
            if hypothesis_id not in known_hypothesis_ids
        ]
        if unknown_hypothesis_ids:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    'message': 'claim references hypotheses outside this investigation',
                    'hypothesis_ids': unknown_hypothesis_ids,
                },
            )

        evidence_keys = [
            (item.run_id, item.artifact_name)
            for item in request.evidence
        ]
        if len(set(evidence_keys)) != len(evidence_keys):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail='claim evidence entries must be unique',
            )

        evidence: list[InvestigationEvidenceReference] = []
        for item in request.evidence:
            if item.run_id not in investigation.run_ids:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f'run is not part of this investigation: {item.run_id}',
                )
            run = store.get_run(item.run_id)
            if run is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f'run not found: {item.run_id}',
                )
            if run.status.status not in TERMINAL_RUN_STATES:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f'run is not terminal: {item.run_id}',
                )
            if (
                request.assessment in {'supported', 'refuted'}
                and run.status.status != 'succeeded'
            ):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail='supported or refuted claims require successful runs',
                )
            if request.assessment in {'supported', 'refuted'}:
                if not run.source_approval_id or not run.source_execution_id:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail='claim run is missing approved execution lineage',
                    )
                run_approval = get_approval(
                    investigation,
                    run.source_approval_id,
                )
                run_execution = get_execution(
                    run_approval.plan_snapshot.plan,
                    run.source_execution_id,
                )
                contract_issues = evaluator_contract_issues(
                    run.reported_metrics,
                    run_execution,
                )
                if contract_issues:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail={
                            'message': (
                                'run does not satisfy its approved evaluator '
                                'contract'
                            ),
                            'issues': contract_issues,
                        },
                    )
            if not run.artifact_bundle_verified:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        f'run {item.run_id} does not have a verified terminal '
                        'artifact bundle'
                    ),
                )
            artifact_ref = run.artifact_refs.get(item.artifact_name)
            if artifact_ref is None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        f'run {item.run_id} has no ingested artifact named '
                        f'{item.artifact_name}'
                    ),
                )
            artifact_index = store.get_artifacts(item.run_id)
            artifact_entry = next(
                (
                    entry
                    for entry in (artifact_index.artifacts if artifact_index else [])
                    if entry.name == item.artifact_name
                ),
                None,
            )
            if artifact_entry is None or artifact_entry.sha256 is None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        f'run {item.run_id} artifact {item.artifact_name} '
                        'does not have a verified content digest'
                    ),
                )
            artifact_path = (
                artifact_run_dir(settings, item.run_id)
                / item.artifact_name
            )
            if (
                artifact_path.is_symlink()
                or not artifact_path.is_file()
                or file_sha256(artifact_path) != artifact_entry.sha256
            ):
                # The ingested digest is the trust root; any on-disk
                # mismatch means the evidence chain is broken.
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        f'run {item.run_id} artifact {item.artifact_name} '
                        'no longer matches its ingested content digest'
                    ),
                )
            evidence.append(
                InvestigationEvidenceReference(
                    run_id=item.run_id,
                    artifact_name=item.artifact_name,
                    artifact_ref=artifact_ref,
                    artifact_sha256=artifact_entry.sha256,
                )
            )

        now = datetime.now(timezone.utc)
        claim = InvestigationClaimRecord(
            claim_id=uuid4().hex,
            statement=request.statement,
            assessment=request.assessment,
            hypothesis_ids=request.hypothesis_ids,
            evidence=evidence,
            created_at=now,
            submitted_by=request.submitted_by or settings.default_submitted_by,
            note=request.note,
        )
        updated = investigation.model_copy(
            update={
                'updated_at': now,
                'status': 'evaluating',
                'claims': [*investigation.claims, claim],
            }
        )
        store.save_investigation(updated)
        return claim
