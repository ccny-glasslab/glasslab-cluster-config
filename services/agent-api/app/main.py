"""FastAPI application and the experiment control loop.

POST /experiments runs the full pipeline: persist an incoming request, call the
planner to normalize it into a PlannerSpec, validate the spec against the
registries, submit the bounded Kubernetes Job, and start a background monitor
thread that polls Job status until it reaches a terminal state. All reads of
experiment state go through the SQLite StateStore, so responses stay correct
across requests even while the monitor thread is mutating the record.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from .auth import authenticate_request
from .config import Settings, get_settings
from .job_status import JobStatusService
from .job_submitter import JobSubmitter
from .logging_utils import configure_logging
from .planner import PlannerError, plan_request
from .qwen_client import QwenClient
from .schemas import (
    ExperimentArtifactsResponse,
    ExperimentCreateRequest,
    ExperimentLogsResponse,
    ExperimentRecord,
)
from .state_store import StateStore
from .summarizer import ResultSummarizer
from .tools import list_datasets, list_pipelines, submit_job, validate_spec


TERMINAL_STATUSES = {'failed', 'rejected', 'succeeded'}
# A record in one of these states stops the monitor; 'rejected' covers specs
# that failed policy validation before any Job was submitted.


@dataclass
class RuntimeContext:
    settings: Settings
    state_store: StateStore
    qwen_client: QwenClient
    job_submitter: JobSubmitter
    job_status_service: JobStatusService
    summarizer: ResultSummarizer
    logger: logging.Logger
    active_monitors: set[str] = field(default_factory=set)
    monitor_lock: threading.Lock = field(default_factory=threading.Lock)


def build_runtime(settings: Settings) -> RuntimeContext:
    configure_logging(settings.log_level)
    logger = logging.getLogger(settings.app_name)
    state_store = StateStore(settings.state_db_path)
    qwen_client = QwenClient(settings)
    job_submitter = JobSubmitter(settings)
    job_status_service = JobStatusService(settings)
    summarizer = ResultSummarizer(settings, qwen_client)
    return RuntimeContext(
        settings=settings,
        state_store=state_store,
        qwen_client=qwen_client,
        job_submitter=job_submitter,
        job_status_service=job_status_service,
        summarizer=summarizer,
        logger=logger,
    )


def create_app(settings: Settings | None = None, runtime: RuntimeContext | None = None) -> FastAPI:
    settings = settings or get_settings()
    runtime = runtime or build_runtime(settings)

    app = FastAPI(title=settings.app_name, version=settings.app_version)
    app.state.runtime = runtime

    @app.middleware('http')
    async def authorize_requests(request: Request, call_next):
        if request.url.path != '/health':
            try:
                authenticate_request(request, settings.api_token)
            except HTTPException as exc:
                return JSONResponse(status_code=exc.status_code, content={'detail': exc.detail})
        return await call_next(request)

    @app.get('/health')
    def health() -> dict:
        return {
            'status': 'ok',
            'app': settings.app_name,
            'version': settings.app_version,
            'planner_model_name': settings.planner_model_name,
            'runner_namespace': settings.runner_namespace,
            'state_db_path': settings.state_db_path,
        }

    @app.get('/pipelines')
    def pipelines() -> list[dict]:
        return list_pipelines()

    @app.get('/datasets')
    def datasets() -> list[dict]:
        return list_datasets()

    @app.post('/experiments', response_model=ExperimentRecord)
    def create_experiment(request: ExperimentCreateRequest) -> ExperimentRecord:
        trace_id = request.trace_id or str(uuid.uuid4())
        store = runtime.state_store
        record = store.create_experiment(request_text=request.request_text, trace_id=trace_id)
        _log_event(runtime, record.id, 'INFO', 'incoming_request', {'request_id': record.id, 'trace_id': trace_id})

        try:
            planner_decision = plan_request(request.request_text, runtime.qwen_client)
            store.update_experiment(
                record.id,
                status='planned',
                planner_source=planner_decision.source,
                planner_raw_output=planner_decision.raw_output,
                normalized_spec=planner_decision.spec,
            )
            _log_event(
                runtime,
                record.id,
                'INFO',
                'planner_output',
                {
                    'source': planner_decision.source,
                    'warnings': planner_decision.warnings,
                    'spec': planner_decision.spec.model_dump(mode='json'),
                },
            )

            validation = validate_spec(planner_decision.spec)
            next_status = 'validated' if validation.valid else 'rejected'
            store.update_experiment(record.id, status=next_status, validation=validation)
            _log_event(
                runtime,
                record.id,
                'INFO',
                'validation_result',
                {'valid': validation.valid, 'errors': validation.errors},
            )
            if not validation.valid:
                return store.get_experiment(record.id)

            submission = submit_job(runtime.job_submitter, planner_decision.spec, record.id, trace_id)
            store.update_experiment(
                record.id,
                status='submitted',
                job_name=submission.job_name,
                submitted_at=datetime.now(timezone.utc),
            )
            _log_event(
                runtime,
                record.id,
                'INFO',
                'job_submitted',
                {
                    'job_name': submission.job_name,
                    'namespace': submission.namespace,
                    'manifest_name': submission.manifest_name,
                },
            )
            if settings.auto_monitor_submitted_jobs:
                _start_monitor(runtime, record.id)
            # Refresh once before returning so the response reflects the current
            # Job status even when monitoring is disabled.
            return _refresh_experiment(runtime, record.id)
        except PlannerError as exc:
            store.update_experiment(record.id, status='failed', error_message=str(exc))
            _log_event(runtime, record.id, 'ERROR', 'planner_failed', {'error': str(exc)})
            return store.get_experiment(record.id)
        except Exception as exc:
            store.update_experiment(record.id, status='failed', error_message=str(exc))
            _log_event(runtime, record.id, 'ERROR', 'submission_failed', {'error': str(exc)})
            return store.get_experiment(record.id)

    @app.get('/experiments/{experiment_id}', response_model=ExperimentRecord)
    def get_experiment(experiment_id: str) -> ExperimentRecord:
        record = runtime.state_store.get_experiment(experiment_id)
        if record is None:
            raise HTTPException(status_code=404, detail='experiment not found')
        return _refresh_experiment(runtime, experiment_id)

    @app.get('/experiments/{experiment_id}/logs', response_model=ExperimentLogsResponse)
    def get_logs(experiment_id: str) -> ExperimentLogsResponse:
        record = runtime.state_store.get_experiment(experiment_id)
        if record is None:
            raise HTTPException(status_code=404, detail='experiment not found')
        return ExperimentLogsResponse(experiment_id=experiment_id, logs=runtime.state_store.get_logs(experiment_id))

    @app.get('/experiments/{experiment_id}/artifacts', response_model=ExperimentArtifactsResponse)
    def get_artifacts(experiment_id: str) -> ExperimentArtifactsResponse:
        record = runtime.state_store.get_experiment(experiment_id)
        if record is None:
            raise HTTPException(status_code=404, detail='experiment not found')
        record = _refresh_experiment(runtime, experiment_id)
        return ExperimentArtifactsResponse(experiment_id=experiment_id, artifacts=record.artifact_refs)

    return app


def _start_monitor(runtime: RuntimeContext, experiment_id: str) -> None:
    # Only one monitor thread may run per experiment; the lock also prevents a
    # concurrent request from starting a duplicate.
    with runtime.monitor_lock:
        if experiment_id in runtime.active_monitors:
            return
        runtime.active_monitors.add(experiment_id)

    thread = threading.Thread(
        target=_monitor_experiment,
        args=(runtime, experiment_id),
        daemon=True,
        name=f'glasslab-monitor-{experiment_id[:8]}',
    )
    thread.start()


def _monitor_experiment(runtime: RuntimeContext, experiment_id: str) -> None:
    try:
        # Poll until the record reaches a terminal state or loses its Job; the
        # finally clause frees the experiment id so a later request can restart
        # monitoring for a stale record.
        while True:
            record = _refresh_experiment(runtime, experiment_id)
            if record.status in TERMINAL_STATUSES or record.job_name is None:
                return
            time.sleep(runtime.settings.poll_interval_seconds)
    finally:
        with runtime.monitor_lock:
            runtime.active_monitors.discard(experiment_id)


def _refresh_experiment(runtime: RuntimeContext, experiment_id: str) -> ExperimentRecord:
    record = runtime.state_store.get_experiment(experiment_id)
    if record is None:
        raise HTTPException(status_code=404, detail='experiment not found')
    if record.job_name is None or record.status in TERMINAL_STATUSES:
        return record

    status_payload = runtime.job_status_service.get_job_status(record.job_name)
    job_status = status_payload['status']
    if job_status != record.status:
        runtime.state_store.update_experiment(experiment_id, status=job_status)
        _log_event(runtime, experiment_id, 'INFO', 'job_status_change', status_payload)
        record = runtime.state_store.get_experiment(experiment_id)

    if job_status == 'succeeded':
        artifacts = runtime.job_status_service.list_artifacts(experiment_id)
        payload = runtime.job_status_service.read_result_payload(experiment_id)
        summary = (
            runtime.summarizer.summarize_result(payload)
            if payload is not None
            else 'Titanic baseline job succeeded, but no result payload was found.'
        )
        # A succeeded Job is finalized once here: artifacts, result summary, and
        # the terminal status are captured together in the record.
        runtime.state_store.update_experiment(
            experiment_id,
            status='succeeded',
            result_summary=summary,
            artifact_refs=artifacts,
        )
        _log_event(runtime, experiment_id, 'INFO', 'final_result_summary', {'summary': summary})
        record = runtime.state_store.get_experiment(experiment_id)
    elif job_status == 'failed':
        artifacts = runtime.job_status_service.list_artifacts(experiment_id)
        # The failure message is the tail of the runner's pod log (see
        # read_failure_message); artifacts are still captured so partial
        # evidence survives for diagnosis.
        error_message = runtime.job_status_service.read_failure_message(record.job_name) or 'Kubernetes Job failed.'
        runtime.state_store.update_experiment(
            experiment_id,
            status='failed',
            error_message=error_message,
            artifact_refs=artifacts,
        )
        _log_event(runtime, experiment_id, 'ERROR', 'job_failed', {'error': error_message})
        record = runtime.state_store.get_experiment(experiment_id)

    return record


def _log_event(
    runtime: RuntimeContext,
    experiment_id: str,
    level: str,
    message: str,
    payload: dict | None = None,
) -> None:
    runtime.state_store.append_log(experiment_id, level, message, payload)
    log_method = getattr(runtime.logger, level.lower(), runtime.logger.info)
    log_method(message, extra={'experiment_id': experiment_id, 'payload': payload})
