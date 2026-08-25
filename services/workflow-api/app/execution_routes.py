"""HTTP routes for run creation, artifact management, logs, and preflight.

Handles two distinct run-creation paths: generic experiments (from raw
GenericExperimentRunRequest) and designed validation runs (from session design
drafts). Supports terminal artifact bundle ingestion, live log retrieval,
multi-run metric comparisons, and execution preflight enriched with
interpretation context and method-spec readiness checks.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Any, Callable
from uuid import uuid4

from fastapi import FastAPI, HTTPException, status
from pydantic import ValidationError

from services.common.schemas import ArtifactIndexEntry, ArtifactsIndex, ExpectedArtifactsSpec, RunManifest, RunStatus

from .config import Settings
from .execution_preflight import build_execution_preflight_result
from .job_submission import JobSubmitter, LiveStatusUnavailableError, resolve_evaluation_contract
from .persistence import RunStore
from .registry import WorkflowRegistry
from .run_artifacts import (
    MEDIA_TYPES,
    load_artifacts_from_disk,
    load_logs_from_disk,
    load_terminal_bundle,
    resolve_run_status,
)
from .schemas import (
    ComparisonRecord,
    ExecutionPreflightResult,
    GenericExperimentCompareRequest,
    GenericExperimentResultIngestRequest,
    GenericExperimentRunRequest,
    InvestigationWorkspaceSpec,
    LogEntry,
    RunArtifactsResponse,
    RunCreateRequest,
    RunLogsResponse,
    RunRecord,
    WorkflowFamilySummary,
)


def register_execution_routes(
    app: FastAPI,
    *,
    settings: Settings,
    registry: WorkflowRegistry,
    store: RunStore,
    submitter: JobSubmitter,
    create_run_record: Callable[..., RunRecord],
) -> Callable[..., RunRecord]:
    def build_artifact_index(run_id: str, expected_artifacts: ExpectedArtifactsSpec) -> ArtifactsIndex:
        artifacts: list[ArtifactIndexEntry] = []
        declared = [(item, True) for item in expected_artifacts.required] + [
            (item, False) for item in expected_artifacts.optional
        ]
        for name, is_required in declared:
            suffix = '' if name.endswith('/') else name[name.rfind('.'):]
            artifacts.append(
                ArtifactIndexEntry(
                    name=name,
                    path=f'runs/{run_id}/{name}',
                    media_type='inode/directory' if name.endswith('/') else MEDIA_TYPES.get(suffix, 'application/octet-stream'),
                    required=is_required,
                    description='Declared by workflow registry',
                )
            )
        return ArtifactsIndex(run_id=run_id, artifacts=artifacts)

    def merge_artifact_refs(
        run_id: str,
        existing: ArtifactsIndex | None,
        refs: dict[str, str],
        expected_artifacts: ExpectedArtifactsSpec,
    ) -> ArtifactsIndex:
        base = existing or build_artifact_index(run_id, expected_artifacts)
        artifact_map = {entry.name: entry for entry in base.artifacts}
        required_names = set(expected_artifacts.required)
        for name, path in refs.items():
            suffix = '' if name.endswith('/') else name[name.rfind('.'):]
            artifact_map[name] = ArtifactIndexEntry(
                name=name,
                path=path,
                media_type='inode/directory' if name.endswith('/') else MEDIA_TYPES.get(suffix, 'application/octet-stream'),
                required=name in required_names,
                description='Reported by generic result ingest',
            )
        return ArtifactsIndex(run_id=run_id, artifacts=sorted(artifact_map.values(), key=lambda entry: entry.name))

    def build_generic_run_record(
        request: GenericExperimentRunRequest,
        *,
        investigation_id: str | None = None,
        source_plan_id: str | None = None,
        source_approval_id: str | None = None,
        source_execution_id: str | None = None,
        plan_sha256: str | None = None,
    ) -> RunRecord:
        workflow = registry.get_workflow(request.workload_id)
        if workflow is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='workload definition not found')
        if workflow.experiment_type and workflow.experiment_type != request.experiment_type:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"workload {workflow.workflow_id} requires experiment_type {workflow.experiment_type}",
            )
        preflight = build_execution_preflight_result(workflow, settings)
        if not preflight.ready:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    'message': 'execution preflight failed',
                    'workflow_id': workflow.workflow_id,
                    'blocking_issues': preflight.blocking_issues,
                },
            )
        if (
            workflow.schema_ref == 'glasslab-investigation-workspace-v1'
            and not all(
                [
                    investigation_id,
                    source_plan_id,
                    source_approval_id,
                    source_execution_id,
                    plan_sha256,
                ]
            )
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    'research workspace runs require an approved investigation '
                    'plan execution'
                ),
            )

        runner_image = workflow.runner_image
        entrypoint = list(workflow.default_entrypoint or [])
        if not entrypoint:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail='no entrypoint resolved for workload')

        config_payload = dict(request.config_payload)
        if workflow.schema_ref in {
            'glasslab-investigation-workspace-v1',
            'glasslab-benchmark-workspace-v1',
        }:
            try:
                workspace = InvestigationWorkspaceSpec.model_validate(
                    config_payload.get('workspace')
                )
            except ValidationError as exc:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail='invalid config_payload.workspace: ' + str(exc),
                ) from exc
            config_payload['workspace'] = workspace.model_dump(mode='json')

        if request.artifact_contract is None:
            expected_artifacts = workflow.expected_artifacts
        else:
            required = list(
                dict.fromkeys(
                    [
                        *workflow.expected_artifacts.required,
                        *request.artifact_contract.required,
                    ]
                )
            )
            optional = [
                name
                for name in dict.fromkeys(
                    [
                        *workflow.expected_artifacts.optional,
                        *request.artifact_contract.optional,
                    ]
                )
                if name not in required
            ]
            expected_artifacts = ExpectedArtifactsSpec(
                required=required,
                optional=optional,
            )
        metric_contract = dict(workflow.metric_contract or {})
        metric_contract.update(request.metric_contract)
        max_wallclock_minutes = request.budget.get('max_wallclock_minutes')
        if (
            not isinstance(max_wallclock_minutes, int)
            or isinstance(max_wallclock_minutes, bool)
            or max_wallclock_minutes < 1
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail='budget.max_wallclock_minutes is required and must be a positive integer',
            )
        if max_wallclock_minutes > workflow.max_wallclock_minutes:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    'budget.max_wallclock_minutes exceeds the registry ceiling '
                    f'of {workflow.max_wallclock_minutes}'
                ),
            )

        now = datetime.now(timezone.utc)
        run_id = uuid4().hex
        manifest = RunManifest(
            run_id=run_id,
            workflow_id=workflow.workflow_id,
            workflow_family=workflow.workflow_family,
            display_name=workflow.display_name,
            objective=request.objective,
            submitted_by=request.submitted_by or settings.default_submitted_by,
            submitted_at=now,
            run_priority=request.run_priority,
            inputs={
                'parent_run_id': request.parent_run_id,
                'campaign_id': request.campaign_id,
            },
            requested_models=workflow.allowed_models[:1],
            resource_profile=workflow.resource_profile.profile_name,
            resource_requests=workflow.resource_profile.requests,
            resource_limits=workflow.resource_profile.limits,
            node_selector=workflow.resource_profile.node_selector,
            runner_image=runner_image,
            runner_service_account_name=workflow.runner_service_account_name,
            maximum_wallclock_minutes=workflow.max_wallclock_minutes,
            evaluator_type=workflow.evaluator_type,
            approval_tier=workflow.approval_tier,
            expected_artifacts=expected_artifacts.model_dump(mode='json'),
            experiment_type=request.experiment_type,
            workload_id=request.workload_id,
            schema_ref=workflow.schema_ref,
            entrypoint=entrypoint,
            config_payload=config_payload,
            dataset_bindings=request.dataset_bindings,
            budget=request.budget,
            metric_contract=metric_contract,
        )
        try:
            resolve_evaluation_contract(manifest, settings)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(exc),
            ) from exc
        status_payload = RunStatus(
            run_id=run_id,
            status='accepted',
            updated_at=now,
            detail='Generic experiment accepted by workflow-api.',
        )
        submission = submitter.submit_run(manifest)
        record = RunRecord(
            run_id=run_id,
            workflow_id=workflow.workflow_id,
            created_at=now,
            updated_at=now,
            manifest=manifest,
            status=status_payload,
            job_submission=submission,
            run_purpose='generic-experiment',
            run_priority=request.run_priority,
            session_id=request.session_id,
            investigation_id=investigation_id,
            source_plan_id=source_plan_id,
            source_approval_id=source_approval_id,
            source_execution_id=source_execution_id,
            plan_sha256=plan_sha256,
        )
        store.save_run(record)
        store.save_artifacts(run_id, build_artifact_index(run_id, expected_artifacts))
        store.append_log(
            run_id,
            LogEntry(
                timestamp=now,
                level='INFO',
                message='generic experiment accepted',
                payload={
                    'workflow_id': workflow.workflow_id,
                    'workload_id': request.workload_id,
                    'job_name': submission.job_name,
                },
            ),
        )
        return record

    def build_comparison_record(request: GenericExperimentCompareRequest) -> ComparisonRecord:
        runs: list[RunRecord] = []
        for run_id in request.run_ids:
            record = store.get_run(run_id)
            if record is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f'run not found: {run_id}')
            runs.append(record)

        metric_name = request.metric_name
        if metric_name is None:
            common_metrics = None
            for record in runs:
                record_metrics = {
                    key
                    for key, value in record.reported_metrics.items()
                    if isinstance(value, (int, float)) and not isinstance(value, bool)
                }
                common_metrics = record_metrics if common_metrics is None else (common_metrics & record_metrics)
            common_metrics = common_metrics or set()
            if not common_metrics:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail='no common numeric metrics available for comparison')
            metric_name = sorted(common_metrics)[0]

        ranking: list[dict[str, Any]] = []
        for record in runs:
            value = record.reported_metrics.get(metric_name)
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f'run {record.run_id} does not have numeric metric {metric_name}',
                )
            ranking.append({'run_id': record.run_id, 'value': float(value)})
        ranking.sort(key=lambda item: item['value'], reverse=request.higher_is_better)
        best = ranking[0]
        now = datetime.now(timezone.utc)
        evaluator_type = request.evaluator_type or runs[0].manifest.evaluator_type
        return ComparisonRecord(
            comparison_id=uuid4().hex,
            created_at=now,
            updated_at=now,
            status='completed',
            comparison_type=request.comparison_type,
            evaluator_type=evaluator_type,
            session_id=request.session_id,
            campaign_id=request.campaign_id,
            workload_id=request.workload_id or runs[0].manifest.workload_id,
            workflow_id=request.workflow_id or runs[0].workflow_id,
            run_ids=[record.run_id for record in runs],
            baseline_run_id=request.baseline_run_id,
            candidate_run_ids=[record.run_id for record in runs if record.run_id != request.baseline_run_id],
            summary_metrics={
                'metric_name': metric_name,
                'higher_is_better': request.higher_is_better,
                'best_run_id': best['run_id'],
                'best_value': best['value'],
                'ranking': ranking,
            },
            notes=request.notes,
        )

    def filter_inputs_for_workflow(inputs: dict[str, Any], workflow) -> dict[str, Any]:
        allowed_inputs = {item.name for item in workflow.required_inputs}
        return {key: value for key, value in inputs.items() if key in allowed_inputs}

    def enrich_inputs_with_method_context(design, workflow) -> dict[str, Any]:
        method_spec = getattr(design, 'method_spec', None)
        inputs = dict(method_spec.execution_inputs) if method_spec is not None else dict(design.declared_inputs)
        if method_spec is None:
            return filter_inputs_for_workflow(inputs, workflow)
        if method_spec.candidate_models:
            inputs['technique_candidate_models'] = list(method_spec.candidate_models)
        if method_spec.baseline_models:
            inputs['technique_baseline_models'] = list(method_spec.baseline_models)
        if method_spec.loss_or_distance:
            inputs['technique_loss_or_distance'] = method_spec.loss_or_distance
        if method_spec.task_type:
            inputs['technique_task_type'] = method_spec.task_type
        if method_spec.metrics:
            inputs['technique_metrics'] = list(method_spec.metrics)
        return filter_inputs_for_workflow(inputs, workflow)

    def resolve_requested_models(requested_models: list[str], workflow) -> list[str]:
        allowed = list(workflow.allowed_models or [])
        requested = [str(model).strip() for model in requested_models if str(model).strip()]
        compatible = [model for model in requested if model in allowed]
        if compatible:
            return compatible
        if allowed:
            return allowed[:1]
        return requested[:1]

    def get_required_session_design(session_id: str):
        session = store.get_research_session(session_id)
        if session is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='research session not found')
        design = store.get_design_draft(session.latest_design_id or '')
        if design is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='research session has no design draft yet')
        return design

    def enrich_preflight_with_interpretation(
        preflight: ExecutionPreflightResult,
        *,
        design,
        session_id: str,
        intake_id: str,
        workflow_id: str,
        resource_profile: str,
    ) -> ExecutionPreflightResult:
        warnings = list(preflight.warnings)
        blocking_issues = list(preflight.blocking_issues)
        declared_inputs = dict(getattr(design, 'declared_inputs', {}) or {})

        train_uri = str(declared_inputs.get('train_uri', '')).strip()
        test_uri = str(declared_inputs.get('test_uri', '')).strip()
        validation_uri = str(declared_inputs.get('validation_uri', '')).strip()
        validation_strategy = str(declared_inputs.get('validation_strategy', '')).strip()
        validation_split = str(declared_inputs.get('validation_split', '')).strip()

        if train_uri and test_uri and train_uri == test_uri:
            blocking_issues.append('train_uri and test_uri resolve to the same dataset path; this risks direct overfitting')
        if not validation_uri and not validation_strategy and not validation_split:
            warnings.append(
                'no explicit validation split or validation strategy is declared; overfitting checks may be weak even if a test split exists'
            )
        elif validation_split:
            warnings.append(f'declared validation split: {validation_split}')
        elif validation_strategy:
            warnings.append(f'declared validation strategy: {validation_strategy}')
        elif validation_uri:
            warnings.append('declared dedicated validation dataset split is present')

        interpretation = store.get_latest_interpretation()
        if interpretation is None:
            return preflight.model_copy(update={'warnings': warnings, 'blocking_issues': blocking_issues})
        if interpretation.intake_id != intake_id:
            return preflight.model_copy(update={'warnings': warnings, 'blocking_issues': blocking_issues})
        if interpretation.session_id not in {None, session_id}:
            return preflight.model_copy(update={'warnings': warnings, 'blocking_issues': blocking_issues})
        required_python_packages = [
            str(item) for item in preflight.runtime_requirements.get('required_python_packages', [])
        ]
        recommended_python_packages = [str(item) for item in interpretation.recommended_python_packages]
        if required_python_packages and recommended_python_packages:
            missing_from_runtime = [
                package for package in recommended_python_packages if package not in required_python_packages
            ]
            if missing_from_runtime:
                warnings.append(
                    'interpretation recommends Python packages outside the declared workflow runtime: '
                    + ', '.join(missing_from_runtime)
                )
            else:
                warnings.append(
                    'interpretation-recommended Python packages fit the declared workflow runtime: '
                    + ', '.join(recommended_python_packages)
                )
        if interpretation.preferred_workflow_id and interpretation.preferred_workflow_id != workflow_id:
            warnings.append(
                f'interpretation preferred workflow {interpretation.preferred_workflow_id} '
                f'but session design selected {workflow_id}'
            )
        if interpretation.preferred_resource_profile and interpretation.preferred_resource_profile != resource_profile:
            warnings.append(
                f'interpretation preferred resource profile {interpretation.preferred_resource_profile} '
                f'but session design selected {resource_profile}'
            )
        workflow_requires_gpu = bool(preflight.runtime_requirements.get('gpu'))
        if interpretation.gpu_required and not workflow_requires_gpu:
            warnings.append('interpretation indicates GPU is required but selected workflow runtime does not declare GPU support')
        if interpretation.dataset_hints:
            warnings.append('interpretation dataset hints: ' + ', '.join(interpretation.dataset_hints[:3]))
        if interpretation.evaluation_targets:
            warnings.append('interpretation evaluation targets: ' + ', '.join(interpretation.evaluation_targets[:3]))
        method_spec = getattr(design, 'method_spec', None)
        if method_spec is not None:
            if method_spec.blocking_reasons:
                warnings.append('method-spec blockers: ' + '; '.join(method_spec.blocking_reasons[:3]))
            if method_spec.run_readiness != 'ready':
                blocking_issues.append('design method_spec is not ready for execution')
        return preflight.model_copy(update={'warnings': warnings, 'blocking_issues': blocking_issues})

    @app.get('/workflow-families', response_model=list[WorkflowFamilySummary])
    def list_workflow_families() -> list[WorkflowFamilySummary]:
        return [
            WorkflowFamilySummary(
                workflow_id=entry.workflow_id,
                display_name=entry.display_name,
                workflow_family=entry.workflow_family,
                description=entry.description,
                allowed_models=entry.allowed_models,
                resource_profile=entry.resource_profile.profile_name,
                approval_tier=entry.approval_tier,
                execution_status=entry.execution_status,
                submission_backend=entry.submission_backend,
                execution_blockers=entry.execution_blockers,
            )
            for entry in registry.list_workflows()
        ]

    @app.get('/workflow-families/{workflow_id}/execution-preflight', response_model=ExecutionPreflightResult)
    def get_workflow_execution_preflight(workflow_id: str) -> ExecutionPreflightResult:
        workflow = registry.get_workflow(workflow_id)
        if workflow is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='workflow not found')
        return build_execution_preflight_result(workflow, settings)

    @app.post('/experiments/runs', response_model=RunRecord, status_code=status.HTTP_201_CREATED)
    def create_generic_experiment_run(request: GenericExperimentRunRequest) -> RunRecord:
        return build_generic_run_record(request)

    @app.post('/experiments/runs/{run_id}/results', response_model=RunRecord)
    def ingest_generic_experiment_results(run_id: str, request: GenericExperimentResultIngestRequest) -> RunRecord:
        record = store.get_run(run_id)
        if record is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='run not found')

        expected_artifacts = ExpectedArtifactsSpec.model_validate(record.manifest.expected_artifacts)
        reported_artifact_refs = {
            **record.artifact_refs,
            **request.artifact_refs,
        }
        if request.terminal_status == 'succeeded':
            missing_required = sorted(
                set(expected_artifacts.required) - set(reported_artifact_refs)
            )
            if missing_required:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={
                        'message': 'successful result is missing required artifacts',
                        'missing_artifacts': missing_required,
                    },
                )

        now = datetime.now(timezone.utc)
        updated = record.model_copy(
            update={
                'updated_at': now,
                'status': RunStatus(
                    run_id=run_id,
                    status=request.terminal_status,
                    updated_at=now,
                    detail=request.detail or f'Generic result ingest marked run {request.terminal_status}.',
                ),
                'reported_metrics': dict(request.metrics),
                'artifact_refs': reported_artifact_refs,
                'runtime_summary': dict(request.runtime),
            }
        )
        store.save_run(updated)

        artifacts = merge_artifact_refs(run_id, store.get_artifacts(run_id), request.artifact_refs, expected_artifacts)
        store.save_artifacts(run_id, artifacts)
        store.append_log(
            run_id,
            LogEntry(
                timestamp=now,
                level='INFO',
                message='generic experiment results ingested',
                payload={'terminal_status': request.terminal_status, 'artifact_count': len(request.artifact_refs)},
            ),
        )
        return updated

    @app.post('/experiments/compare', response_model=ComparisonRecord, status_code=status.HTTP_201_CREATED)
    def compare_generic_experiment_runs(request: GenericExperimentCompareRequest) -> ComparisonRecord:
        record = build_comparison_record(request)
        store.save_comparison(record)
        return record

    @app.post('/runs', response_model=RunRecord, status_code=status.HTTP_201_CREATED)
    def create_run(request: RunCreateRequest) -> RunRecord:
        workflow = registry.get_workflow(request.workflow_id)
        if workflow is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=[{'field': 'workflow_id', 'message': f'unsupported workflow family: {request.workflow_id}'}],
        )
        return create_run_record(request, workflow, settings, submitter, store)

    @app.get('/research-sessions/latest/execution-preflight', response_model=ExecutionPreflightResult)
    def get_latest_session_execution_preflight() -> ExecutionPreflightResult:
        session = store.get_latest_research_session()
        if session is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='no research session has been created yet')
        return get_session_execution_preflight(session.session_id)

    @app.post('/research-sessions/latest/runs/from-design', response_model=RunRecord, status_code=status.HTTP_201_CREATED)
    def create_run_from_latest_session_design() -> RunRecord:
        session = store.get_latest_research_session()
        if session is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='no research session has been created yet')
        return create_run_from_session_design(session.session_id)

    @app.get('/research-sessions/{session_id}/execution-preflight', response_model=ExecutionPreflightResult)
    def get_session_execution_preflight(session_id: str) -> ExecutionPreflightResult:
        design = get_required_session_design(session_id)
        workflow = registry.get_workflow(design.workflow_id)
        if workflow is None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail='workflow registry entry not found')
        preflight = build_execution_preflight_result(workflow, settings)
        return enrich_preflight_with_interpretation(
            preflight,
            design=design,
            session_id=session_id,
            intake_id=design.intake_id,
            workflow_id=design.workflow_id,
            resource_profile=design.resource_profile,
        )

    @app.post('/research-sessions/{session_id}/runs/from-design', response_model=RunRecord, status_code=status.HTTP_201_CREATED)
    def create_run_from_session_design(session_id: str) -> RunRecord:
        design = get_required_session_design(session_id)
        method_spec = getattr(design, 'method_spec', None)
        if method_spec is not None and method_spec.run_readiness != 'ready':
            detail = 'design method_spec is not ready_for_run'
            if method_spec.blocking_reasons:
                detail += ': ' + '; '.join(method_spec.blocking_reasons[:2])
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)
        if design.status != 'ready_for_run':
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail='design draft is not ready_for_run')
        workflow = registry.get_workflow(design.workflow_id)
        if workflow is None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail='workflow registry entry not found')
        request = RunCreateRequest(
            workflow_id=design.workflow_id,
            objective=design.objective,
            inputs=enrich_inputs_with_method_context(design, workflow),
            models=resolve_requested_models(
                (method_spec.candidate_models if method_spec is not None and method_spec.candidate_models else design.candidate_models)
                or workflow.allowed_models[:1],
                workflow,
            ),
            resource_profile=design.resource_profile,
            submitted_by=design.submitted_by,
        )
        return create_run_record(
            request,
            workflow,
            settings,
            submitter,
            store,
            source_design_id=design.design_id,
            source_intake_id=design.intake_id,
            run_purpose='validation',
            session_id=design.session_id,
        )

    @app.post('/runs/from-latest-design-draft', response_model=RunRecord, status_code=status.HTTP_201_CREATED)
    def create_run_from_latest_design_draft() -> RunRecord:
        design = store.get_latest_design_draft()
        if design is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='design draft not found')
        method_spec = getattr(design, 'method_spec', None)
        if method_spec is not None and method_spec.run_readiness != 'ready':
            detail = 'design method_spec is not ready_for_run'
            if method_spec.blocking_reasons:
                detail += ': ' + '; '.join(method_spec.blocking_reasons[:2])
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)
        if design.status != 'ready_for_run':
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail='design draft is not ready_for_run')
        workflow = registry.get_workflow(design.workflow_id)
        if workflow is None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail='workflow registry entry not found')
        request = RunCreateRequest(
            workflow_id=design.workflow_id,
            objective=design.objective,
            inputs=enrich_inputs_with_method_context(design, workflow),
            models=resolve_requested_models(
                (method_spec.candidate_models if method_spec is not None and method_spec.candidate_models else design.candidate_models)
                or workflow.allowed_models[:1],
                workflow,
            ),
            resource_profile=design.resource_profile,
            submitted_by=design.submitted_by,
        )
        return create_run_record(
            request,
            workflow,
            settings,
            submitter,
            store,
            source_design_id=design.design_id,
            source_intake_id=design.intake_id,
            run_purpose='validation',
            session_id=design.session_id,
        )

    @app.get('/runs/{run_id}', response_model=RunRecord)
    def get_run(run_id: str) -> RunRecord:
        record = store.get_run(run_id)
        if record is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='run not found')
        return record.model_copy(update={'status': resolve_run_status(record, settings, submitter)})

    @app.post('/runs/{run_id}/cancel', response_model=RunRecord)
    def cancel_run(run_id: str) -> RunRecord:
        record = store.get_run(run_id)
        if record is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='run not found')
        if record.status.status == 'cancelled':
            return record
        if record.status.status in {'succeeded', 'failed', 'rejected'}:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail='terminal run cannot be cancelled')
        try:
            submitter.cancel_run(record)
        except (LiveStatusUnavailableError, NotImplementedError) as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail='workload cancellation could not be confirmed',
            ) from exc
        now = datetime.now(timezone.utc)
        updated = record.model_copy(
            update={
                'updated_at': now,
                'status': RunStatus(
                    run_id=run_id,
                    status='cancelled',
                    updated_at=now,
                    detail='Workload cancellation confirmed.',
                ),
            }
        )
        store.save_run(updated)
        return updated

    @app.get('/runs/{run_id}/artifacts', response_model=RunArtifactsResponse)
    def get_run_artifacts(run_id: str) -> RunArtifactsResponse:
        record = store.get_run(run_id)
        if record is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='run not found')
        artifacts = load_artifacts_from_disk(settings, run_id) or store.get_artifacts(run_id)
        if artifacts is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='artifacts not found')
        return RunArtifactsResponse(run_id=run_id, artifacts=artifacts)

    @app.post('/runs/{run_id}/artifacts/ingest', response_model=RunRecord)
    def ingest_run_artifact_bundle(run_id: str) -> RunRecord:
        record = store.get_run(run_id)
        if record is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='run not found')
        if record.artifact_bundle_verified:
            return record
        try:
            terminal_status, metrics, artifact_refs, artifacts = load_terminal_bundle(
                settings,
                record,
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(exc),
            ) from exc

        updated = record.model_copy(
            update={
                'updated_at': terminal_status.updated_at,
                'status': terminal_status,
                'reported_metrics': metrics,
                'artifact_refs': artifact_refs,
                'artifact_bundle_verified': True,
            }
        )
        store.save_run(updated)
        store.save_artifacts(run_id, artifacts)
        store.append_log(
            run_id,
            LogEntry(
                timestamp=terminal_status.updated_at,
                level='INFO',
                message='terminal artifact bundle ingested',
                payload={
                    'terminal_status': terminal_status.status,
                    'artifact_count': len(artifact_refs),
                },
            ),
        )
        return updated

    @app.get('/runs/{run_id}/logs', response_model=RunLogsResponse)
    def get_run_logs(run_id: str) -> RunLogsResponse:
        record = store.get_run(run_id)
        if record is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='run not found')
        logs = load_logs_from_disk(settings, run_id)
        if not logs:
            logs = submitter.get_live_logs(record) or store.get_logs(run_id)
        return RunLogsResponse(run_id=run_id, logs=logs)

    return build_generic_run_record
