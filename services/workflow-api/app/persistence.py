"""Abstract store interface with three backends: in-memory, JSON file, PostgreSQL.

The RunStore ABC defines the full read/write surface for all record types.
InMemoryRunStore provides the shared implementation; JsonFileRunStore and
PostgresRunStore layer persistence on top of it. Every mutating save method in
the durable backends calls super().save_*(record) then _flush(), so the
in-memory cache is always written first and the durable write is an append.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
import json
from pathlib import Path
from types import ModuleType
from threading import Lock
from typing import Any, TypeVar

from services.common.schemas import ArtifactsIndex

from .schemas import (
    AutoresearchCampaignRecord,
    AutoresearchDecisionRecord,
    AutoresearchIterationRecord,
    ComparisonRecord,
    DatasetRecord,
    DesignDraftRecord,
    IntakeRecord,
    InvestigationRecord,
    InterpretationRecord,
    LogEntry,
    MethodologyDraftRecord,
    OperationRecord,
    PaperIntakeQueueRecord,
    ResearchSessionRecord,
    ResearchProblemRecord,
    ReplicabilityAssessmentRecord,
    RunRecord,
    ScheduledExecutionRecord,
    ScheduledOperationRecord,
    SourceDocumentRecord,
    TechniqueCatalogRecord,
)

ModelT = TypeVar('ModelT')


class IdempotencyConflict(RuntimeError):
    """A run with the same idempotency key already exists in the store.

    Raised by save_run when a caller tries to persist a second run under a
    key that is already bound to a different run_id. Callers catch this and
    return the existing run instead of creating a duplicate Job.
    """


def _import_psycopg() -> ModuleType:
    import psycopg

    return psycopg


class RunStore(ABC):
    @abstractmethod
    def save_dataset_record(self, record: DatasetRecord) -> None:
        raise NotImplementedError

    @abstractmethod
    def get_dataset_record(self, dataset_id: str) -> DatasetRecord | None:
        raise NotImplementedError

    @abstractmethod
    def list_dataset_records(self) -> list[DatasetRecord]:
        raise NotImplementedError

    @abstractmethod
    def save_technique_catalog_record(self, record: TechniqueCatalogRecord) -> None:
        raise NotImplementedError

    @abstractmethod
    def get_technique_catalog_record(self, technique_id: str) -> TechniqueCatalogRecord | None:
        raise NotImplementedError

    @abstractmethod
    def list_technique_catalog_records(self) -> list[TechniqueCatalogRecord]:
        raise NotImplementedError

    @abstractmethod
    def save_methodology_draft(self, record: MethodologyDraftRecord) -> None:
        raise NotImplementedError

    @abstractmethod
    def get_methodology_draft(self, methodology_draft_id: str) -> MethodologyDraftRecord | None:
        raise NotImplementedError

    @abstractmethod
    def get_latest_methodology_draft(self) -> MethodologyDraftRecord | None:
        raise NotImplementedError

    @abstractmethod
    def list_methodology_drafts(self, campaign_id: str | None = None) -> list[MethodologyDraftRecord]:
        raise NotImplementedError

    @abstractmethod
    def save_autoresearch_campaign(self, record: AutoresearchCampaignRecord) -> None:
        raise NotImplementedError

    @abstractmethod
    def get_autoresearch_campaign(self, campaign_id: str) -> AutoresearchCampaignRecord | None:
        raise NotImplementedError

    @abstractmethod
    def get_latest_autoresearch_campaign(self) -> AutoresearchCampaignRecord | None:
        raise NotImplementedError

    @abstractmethod
    def list_autoresearch_campaigns(self) -> list[AutoresearchCampaignRecord]:
        raise NotImplementedError

    @abstractmethod
    def save_autoresearch_iteration(self, record: AutoresearchIterationRecord) -> None:
        raise NotImplementedError

    @abstractmethod
    def get_autoresearch_iteration(self, iteration_id: str) -> AutoresearchIterationRecord | None:
        raise NotImplementedError

    @abstractmethod
    def get_latest_autoresearch_iteration(self) -> AutoresearchIterationRecord | None:
        raise NotImplementedError

    @abstractmethod
    def list_autoresearch_iterations(self, campaign_id: str | None = None) -> list[AutoresearchIterationRecord]:
        raise NotImplementedError

    @abstractmethod
    def save_autoresearch_decision(self, record: AutoresearchDecisionRecord) -> None:
        raise NotImplementedError

    @abstractmethod
    def get_autoresearch_decision(self, decision_id: str) -> AutoresearchDecisionRecord | None:
        raise NotImplementedError

    @abstractmethod
    def get_latest_autoresearch_decision(self) -> AutoresearchDecisionRecord | None:
        raise NotImplementedError

    @abstractmethod
    def list_autoresearch_decisions(self, campaign_id: str | None = None) -> list[AutoresearchDecisionRecord]:
        raise NotImplementedError

    @abstractmethod
    def save_comparison(self, record: ComparisonRecord) -> None:
        raise NotImplementedError

    @abstractmethod
    def get_comparison(self, comparison_id: str) -> ComparisonRecord | None:
        raise NotImplementedError

    @abstractmethod
    def get_latest_comparison(self) -> ComparisonRecord | None:
        raise NotImplementedError

    @abstractmethod
    def list_comparisons(
        self,
        *,
        session_id: str | None = None,
        campaign_id: str | None = None,
    ) -> list[ComparisonRecord]:
        raise NotImplementedError

    @abstractmethod
    def save_investigation(self, record: InvestigationRecord) -> None:
        raise NotImplementedError

    @abstractmethod
    def get_investigation(self, investigation_id: str) -> InvestigationRecord | None:
        raise NotImplementedError

    @abstractmethod
    def get_latest_investigation(self) -> InvestigationRecord | None:
        raise NotImplementedError

    @abstractmethod
    def list_investigations(self) -> list[InvestigationRecord]:
        raise NotImplementedError

    @abstractmethod
    def save_research_session(self, record: ResearchSessionRecord) -> None:
        raise NotImplementedError

    @abstractmethod
    def get_research_session(self, session_id: str) -> ResearchSessionRecord | None:
        raise NotImplementedError

    @abstractmethod
    def get_latest_research_session(self) -> ResearchSessionRecord | None:
        raise NotImplementedError

    @abstractmethod
    def list_research_sessions(self) -> list[ResearchSessionRecord]:
        raise NotImplementedError

    @abstractmethod
    def save_intake(self, record: IntakeRecord) -> None:
        raise NotImplementedError

    @abstractmethod
    def get_intake(self, intake_id: str) -> IntakeRecord | None:
        raise NotImplementedError

    @abstractmethod
    def get_latest_intake(self) -> IntakeRecord | None:
        raise NotImplementedError

    @abstractmethod
    def save_design_draft(self, record: DesignDraftRecord) -> None:
        raise NotImplementedError

    @abstractmethod
    def get_design_draft(self, design_id: str) -> DesignDraftRecord | None:
        raise NotImplementedError

    @abstractmethod
    def get_latest_design_draft(self) -> DesignDraftRecord | None:
        raise NotImplementedError

    @abstractmethod
    def save_interpretation(self, record: InterpretationRecord) -> None:
        raise NotImplementedError

    @abstractmethod
    def get_interpretation(self, interpretation_id: str) -> InterpretationRecord | None:
        raise NotImplementedError

    @abstractmethod
    def get_latest_interpretation(self) -> InterpretationRecord | None:
        raise NotImplementedError

    @abstractmethod
    def save_replicability_assessment(self, record: ReplicabilityAssessmentRecord) -> None:
        raise NotImplementedError

    @abstractmethod
    def get_replicability_assessment(self, assessment_id: str) -> ReplicabilityAssessmentRecord | None:
        raise NotImplementedError

    @abstractmethod
    def get_latest_replicability_assessment(self) -> ReplicabilityAssessmentRecord | None:
        raise NotImplementedError

    @abstractmethod
    def save_run(self, record: RunRecord) -> None:
        raise NotImplementedError

    @abstractmethod
    def save_research_problem(self, record: ResearchProblemRecord) -> None:
        raise NotImplementedError

    @abstractmethod
    def get_research_problem(self, problem_id: str) -> ResearchProblemRecord | None:
        raise NotImplementedError

    @abstractmethod
    def get_latest_research_problem(self) -> ResearchProblemRecord | None:
        raise NotImplementedError

    @abstractmethod
    def save_paper_intake_queue(self, record: PaperIntakeQueueRecord) -> None:
        raise NotImplementedError

    @abstractmethod
    def get_paper_intake_queue(self, queue_id: str) -> PaperIntakeQueueRecord | None:
        raise NotImplementedError

    @abstractmethod
    def get_latest_paper_intake_queue(self) -> PaperIntakeQueueRecord | None:
        raise NotImplementedError

    @abstractmethod
    def list_paper_intake_queues(self) -> list[PaperIntakeQueueRecord]:
        raise NotImplementedError

    @abstractmethod
    def save_source_document(self, record: SourceDocumentRecord) -> None:
        raise NotImplementedError

    @abstractmethod
    def get_source_document(self, document_id: str) -> SourceDocumentRecord | None:
        raise NotImplementedError

    @abstractmethod
    def get_latest_source_document(self) -> SourceDocumentRecord | None:
        raise NotImplementedError

    @abstractmethod
    def list_source_documents(self) -> list[SourceDocumentRecord]:
        raise NotImplementedError

    @abstractmethod
    def get_run(self, run_id: str) -> RunRecord | None:
        raise NotImplementedError

    @abstractmethod
    def get_run_by_idempotency_key(self, idempotency_key: str) -> RunRecord | None:
        raise NotImplementedError

    @abstractmethod
    def get_latest_run(self) -> RunRecord | None:
        raise NotImplementedError

    @abstractmethod
    def list_runs(self) -> list[RunRecord]:
        raise NotImplementedError

    @abstractmethod
    def save_schedule(self, record: ScheduledOperationRecord) -> None:
        raise NotImplementedError

    @abstractmethod
    def get_schedule(self, schedule_id: str) -> ScheduledOperationRecord | None:
        raise NotImplementedError

    @abstractmethod
    def list_schedules(self, operation_type: str | None = None) -> list[ScheduledOperationRecord]:
        raise NotImplementedError

    @abstractmethod
    def save_execution(self, record: ScheduledExecutionRecord) -> None:
        raise NotImplementedError

    @abstractmethod
    def list_executions(self, schedule_id: str | None = None) -> list[ScheduledExecutionRecord]:
        raise NotImplementedError

    @abstractmethod
    def save_artifacts(self, run_id: str, artifacts: ArtifactsIndex) -> None:
        raise NotImplementedError

    @abstractmethod
    def get_artifacts(self, run_id: str) -> ArtifactsIndex | None:
        raise NotImplementedError

    @abstractmethod
    def save_operation(self, record: OperationRecord) -> None:
        raise NotImplementedError

    @abstractmethod
    def get_operation(self, operation_id: str) -> OperationRecord | None:
        raise NotImplementedError

    @abstractmethod
    def get_latest_operation(self) -> OperationRecord | None:
        raise NotImplementedError

    @abstractmethod
    def list_operations(self, operation_type: str | None = None) -> list[OperationRecord]:
        raise NotImplementedError

    @abstractmethod
    def append_log(self, run_id: str, entry: LogEntry) -> None:
        raise NotImplementedError

    @abstractmethod
    def get_logs(self, run_id: str) -> list[LogEntry]:
        raise NotImplementedError


def _parse_record_map(items: dict[str, Any], model_type: type[ModelT]) -> dict[str, ModelT]:
    return {record_id: model_type.model_validate(payload) for record_id, payload in items.items()}


def _parse_artifacts_map(items: dict[str, Any]) -> dict[str, ArtifactsIndex]:
    return {run_id: ArtifactsIndex.model_validate(payload) for run_id, payload in items.items()}


def _parse_logs_map(items: dict[str, Any]) -> dict[str, list[LogEntry]]:
    return {
        run_id: [LogEntry.model_validate(entry) for entry in entries]
        for run_id, entries in items.items()
    }


class InMemoryRunStore(RunStore):
    # Every dict is protected by self._lock (a threading.Lock). Durable
    # subclass save methods call super().save_* (which acquires and releases
    # the lock) then _flush() (which acquires it separately), so the two
    # operations are sequential, not reentrant.
    def __init__(self) -> None:
        self._datasets: dict[str, DatasetRecord] = {}
        self._technique_catalog: dict[str, TechniqueCatalogRecord] = {}
        self._methodology_drafts: dict[str, MethodologyDraftRecord] = {}
        self._latest_methodology_draft_id: str | None = None
        self._autoresearch_campaigns: dict[str, AutoresearchCampaignRecord] = {}
        self._latest_autoresearch_campaign_id: str | None = None
        self._autoresearch_iterations: dict[str, AutoresearchIterationRecord] = {}
        self._latest_autoresearch_iteration_id: str | None = None
        self._autoresearch_decisions: dict[str, AutoresearchDecisionRecord] = {}
        self._latest_autoresearch_decision_id: str | None = None
        self._comparisons: dict[str, ComparisonRecord] = {}
        self._latest_comparison_id: str | None = None
        self._investigations: dict[str, InvestigationRecord] = {}
        self._latest_investigation_id: str | None = None
        self._research_sessions: dict[str, ResearchSessionRecord] = {}
        self._latest_research_session_id: str | None = None
        self._intakes: dict[str, IntakeRecord] = {}
        self._latest_intake_id: str | None = None
        self._interpretations: dict[str, InterpretationRecord] = {}
        self._latest_interpretation_id: str | None = None
        self._replicability_assessments: dict[str, ReplicabilityAssessmentRecord] = {}
        self._latest_replicability_assessment_id: str | None = None
        self._design_drafts: dict[str, DesignDraftRecord] = {}
        self._latest_design_draft_id: str | None = None
        self._research_problems: dict[str, ResearchProblemRecord] = {}
        self._latest_research_problem_id: str | None = None
        self._paper_intake_queues: dict[str, PaperIntakeQueueRecord] = {}
        self._latest_paper_intake_queue_id: str | None = None
        self._source_documents: dict[str, SourceDocumentRecord] = {}
        self._latest_source_document_id: str | None = None
        self._runs: dict[str, RunRecord] = {}
        self._latest_run_id: str | None = None
        self._schedules: dict[str, ScheduledOperationRecord] = {}
        self._executions: dict[str, ScheduledExecutionRecord] = {}
        self._artifacts: dict[str, ArtifactsIndex] = {}
        self._logs: dict[str, list[LogEntry]] = {}
        self._operations: dict[str, OperationRecord] = {}
        self._latest_operation_id: str | None = None
        self._lock = Lock()

    def save_dataset_record(self, record: DatasetRecord) -> None:
        with self._lock:
            self._datasets[record.dataset_id] = record

    def get_dataset_record(self, dataset_id: str) -> DatasetRecord | None:
        with self._lock:
            return self._datasets.get(dataset_id)

    def list_dataset_records(self) -> list[DatasetRecord]:
        with self._lock:
            records = list(self._datasets.values())
        return sorted(records, key=lambda record: record.created_at)

    def save_technique_catalog_record(self, record: TechniqueCatalogRecord) -> None:
        with self._lock:
            self._technique_catalog[record.technique_id] = record

    def get_technique_catalog_record(self, technique_id: str) -> TechniqueCatalogRecord | None:
        with self._lock:
            return self._technique_catalog.get(technique_id)

    def list_technique_catalog_records(self) -> list[TechniqueCatalogRecord]:
        with self._lock:
            records = list(self._technique_catalog.values())
        return sorted(records, key=lambda record: record.created_at)

    def save_methodology_draft(self, record: MethodologyDraftRecord) -> None:
        with self._lock:
            self._methodology_drafts[record.methodology_draft_id] = record
            self._latest_methodology_draft_id = record.methodology_draft_id

    def get_methodology_draft(self, methodology_draft_id: str) -> MethodologyDraftRecord | None:
        with self._lock:
            return self._methodology_drafts.get(methodology_draft_id)

    def get_latest_methodology_draft(self) -> MethodologyDraftRecord | None:
        with self._lock:
            if self._latest_methodology_draft_id is None:
                return None
            return self._methodology_drafts.get(self._latest_methodology_draft_id)

    def list_methodology_drafts(self, campaign_id: str | None = None) -> list[MethodologyDraftRecord]:
        with self._lock:
            records = list(self._methodology_drafts.values())
        if campaign_id is not None:
            records = [record for record in records if record.campaign_id == campaign_id]
        return sorted(records, key=lambda record: record.created_at)

    def save_autoresearch_campaign(self, record: AutoresearchCampaignRecord) -> None:
        with self._lock:
            self._autoresearch_campaigns[record.campaign_id] = record
            self._latest_autoresearch_campaign_id = record.campaign_id

    def get_autoresearch_campaign(self, campaign_id: str) -> AutoresearchCampaignRecord | None:
        with self._lock:
            return self._autoresearch_campaigns.get(campaign_id)

    def get_latest_autoresearch_campaign(self) -> AutoresearchCampaignRecord | None:
        with self._lock:
            if self._latest_autoresearch_campaign_id is None:
                return None
            return self._autoresearch_campaigns.get(self._latest_autoresearch_campaign_id)

    def list_autoresearch_campaigns(self) -> list[AutoresearchCampaignRecord]:
        with self._lock:
            records = list(self._autoresearch_campaigns.values())
        return sorted(records, key=lambda record: record.created_at)

    def save_autoresearch_iteration(self, record: AutoresearchIterationRecord) -> None:
        with self._lock:
            self._autoresearch_iterations[record.iteration_id] = record
            self._latest_autoresearch_iteration_id = record.iteration_id

    def get_autoresearch_iteration(self, iteration_id: str) -> AutoresearchIterationRecord | None:
        with self._lock:
            return self._autoresearch_iterations.get(iteration_id)

    def get_latest_autoresearch_iteration(self) -> AutoresearchIterationRecord | None:
        with self._lock:
            if self._latest_autoresearch_iteration_id is None:
                return None
            return self._autoresearch_iterations.get(self._latest_autoresearch_iteration_id)

    def list_autoresearch_iterations(self, campaign_id: str | None = None) -> list[AutoresearchIterationRecord]:
        with self._lock:
            records = list(self._autoresearch_iterations.values())
        if campaign_id is not None:
            records = [record for record in records if record.campaign_id == campaign_id]
        return sorted(records, key=lambda record: record.created_at)

    def save_autoresearch_decision(self, record: AutoresearchDecisionRecord) -> None:
        with self._lock:
            self._autoresearch_decisions[record.decision_id] = record
            self._latest_autoresearch_decision_id = record.decision_id

    def get_autoresearch_decision(self, decision_id: str) -> AutoresearchDecisionRecord | None:
        with self._lock:
            return self._autoresearch_decisions.get(decision_id)

    def get_latest_autoresearch_decision(self) -> AutoresearchDecisionRecord | None:
        with self._lock:
            if self._latest_autoresearch_decision_id is None:
                return None
            return self._autoresearch_decisions.get(self._latest_autoresearch_decision_id)

    def list_autoresearch_decisions(self, campaign_id: str | None = None) -> list[AutoresearchDecisionRecord]:
        with self._lock:
            records = list(self._autoresearch_decisions.values())
        if campaign_id is not None:
            records = [record for record in records if record.campaign_id == campaign_id]
        return sorted(records, key=lambda record: record.created_at)

    def save_comparison(self, record: ComparisonRecord) -> None:
        with self._lock:
            self._comparisons[record.comparison_id] = record
            self._latest_comparison_id = record.comparison_id

    def get_comparison(self, comparison_id: str) -> ComparisonRecord | None:
        with self._lock:
            return self._comparisons.get(comparison_id)

    def get_latest_comparison(self) -> ComparisonRecord | None:
        with self._lock:
            if self._latest_comparison_id is None:
                return None
            return self._comparisons.get(self._latest_comparison_id)

    def list_comparisons(
        self,
        *,
        session_id: str | None = None,
        campaign_id: str | None = None,
    ) -> list[ComparisonRecord]:
        with self._lock:
            records = list(self._comparisons.values())
        if session_id is not None:
            records = [record for record in records if record.session_id == session_id]
        if campaign_id is not None:
            records = [record for record in records if record.campaign_id == campaign_id]
        return sorted(records, key=lambda record: record.created_at)

    def save_investigation(self, record: InvestigationRecord) -> None:
        with self._lock:
            self._investigations[record.investigation_id] = record
            self._latest_investigation_id = record.investigation_id

    def get_investigation(self, investigation_id: str) -> InvestigationRecord | None:
        with self._lock:
            return self._investigations.get(investigation_id)

    def get_latest_investigation(self) -> InvestigationRecord | None:
        with self._lock:
            if self._latest_investigation_id is None:
                return None
            return self._investigations.get(self._latest_investigation_id)

    def list_investigations(self) -> list[InvestigationRecord]:
        with self._lock:
            records = list(self._investigations.values())
        return sorted(records, key=lambda record: record.created_at)

    def save_research_session(self, record: ResearchSessionRecord) -> None:
        with self._lock:
            self._research_sessions[record.session_id] = record
            self._latest_research_session_id = record.session_id

    def get_research_session(self, session_id: str) -> ResearchSessionRecord | None:
        with self._lock:
            return self._research_sessions.get(session_id)

    def get_latest_research_session(self) -> ResearchSessionRecord | None:
        with self._lock:
            if self._latest_research_session_id is None:
                return None
            return self._research_sessions.get(self._latest_research_session_id)

    def list_research_sessions(self) -> list[ResearchSessionRecord]:
        with self._lock:
            records = list(self._research_sessions.values())
        return sorted(records, key=lambda record: record.created_at)

    def save_intake(self, record: IntakeRecord) -> None:
        with self._lock:
            self._intakes[record.intake_id] = record
            self._latest_intake_id = record.intake_id

    def get_intake(self, intake_id: str) -> IntakeRecord | None:
        with self._lock:
            return self._intakes.get(intake_id)

    def get_latest_intake(self) -> IntakeRecord | None:
        with self._lock:
            if self._latest_intake_id is None:
                return None
            return self._intakes.get(self._latest_intake_id)

    def save_interpretation(self, record: InterpretationRecord) -> None:
        with self._lock:
            self._interpretations[record.interpretation_id] = record
            self._latest_interpretation_id = record.interpretation_id

    def get_interpretation(self, interpretation_id: str) -> InterpretationRecord | None:
        with self._lock:
            return self._interpretations.get(interpretation_id)

    def get_latest_interpretation(self) -> InterpretationRecord | None:
        with self._lock:
            if self._latest_interpretation_id is None:
                return None
            return self._interpretations.get(self._latest_interpretation_id)

    def save_replicability_assessment(self, record: ReplicabilityAssessmentRecord) -> None:
        with self._lock:
            self._replicability_assessments[record.assessment_id] = record
            self._latest_replicability_assessment_id = record.assessment_id

    def get_replicability_assessment(self, assessment_id: str) -> ReplicabilityAssessmentRecord | None:
        with self._lock:
            return self._replicability_assessments.get(assessment_id)

    def get_latest_replicability_assessment(self) -> ReplicabilityAssessmentRecord | None:
        with self._lock:
            if self._latest_replicability_assessment_id is None:
                return None
            return self._replicability_assessments.get(self._latest_replicability_assessment_id)

    def save_design_draft(self, record: DesignDraftRecord) -> None:
        with self._lock:
            self._design_drafts[record.design_id] = record
            self._latest_design_draft_id = record.design_id

    def get_design_draft(self, design_id: str) -> DesignDraftRecord | None:
        with self._lock:
            return self._design_drafts.get(design_id)

    def get_latest_design_draft(self) -> DesignDraftRecord | None:
        with self._lock:
            if self._latest_design_draft_id is None:
                return None
            return self._design_drafts.get(self._latest_design_draft_id)

    def save_run(self, record: RunRecord) -> None:
        with self._lock:
            if record.idempotency_key:
                for existing in self._runs.values():
                    if (
                        existing.run_id != record.run_id
                        and existing.idempotency_key == record.idempotency_key
                    ):
                        raise IdempotencyConflict(
                            f'idempotency key already used: {record.idempotency_key}'
                        )
            self._runs[record.run_id] = record
            self._latest_run_id = record.run_id

    def save_research_problem(self, record: ResearchProblemRecord) -> None:
        with self._lock:
            self._research_problems[record.problem_id] = record
            self._latest_research_problem_id = record.problem_id

    def get_research_problem(self, problem_id: str) -> ResearchProblemRecord | None:
        with self._lock:
            return self._research_problems.get(problem_id)

    def get_latest_research_problem(self) -> ResearchProblemRecord | None:
        with self._lock:
            if self._latest_research_problem_id is None:
                return None
            return self._research_problems.get(self._latest_research_problem_id)

    def save_paper_intake_queue(self, record: PaperIntakeQueueRecord) -> None:
        with self._lock:
            self._paper_intake_queues[record.queue_id] = record
            self._latest_paper_intake_queue_id = record.queue_id

    def get_paper_intake_queue(self, queue_id: str) -> PaperIntakeQueueRecord | None:
        with self._lock:
            return self._paper_intake_queues.get(queue_id)

    def get_latest_paper_intake_queue(self) -> PaperIntakeQueueRecord | None:
        with self._lock:
            if self._latest_paper_intake_queue_id is None:
                return None
            return self._paper_intake_queues.get(self._latest_paper_intake_queue_id)

    def list_paper_intake_queues(self) -> list[PaperIntakeQueueRecord]:
        with self._lock:
            records = list(self._paper_intake_queues.values())
        return sorted(records, key=lambda record: record.created_at)

    def save_source_document(self, record: SourceDocumentRecord) -> None:
        with self._lock:
            self._source_documents[record.document_id] = record
            self._latest_source_document_id = record.document_id

    def get_source_document(self, document_id: str) -> SourceDocumentRecord | None:
        with self._lock:
            return self._source_documents.get(document_id)

    def get_latest_source_document(self) -> SourceDocumentRecord | None:
        with self._lock:
            if self._latest_source_document_id is None:
                return None
            return self._source_documents.get(self._latest_source_document_id)

    def list_source_documents(self) -> list[SourceDocumentRecord]:
        with self._lock:
            records = list(self._source_documents.values())
        return sorted(records, key=lambda record: record.created_at)

    def get_run(self, run_id: str) -> RunRecord | None:
        with self._lock:
            return self._runs.get(run_id)

    def get_run_by_idempotency_key(self, idempotency_key: str) -> RunRecord | None:
        with self._lock:
            for record in self._runs.values():
                if record.idempotency_key == idempotency_key:
                    return record
            return None

    def get_latest_run(self) -> RunRecord | None:
        with self._lock:
            if self._latest_run_id is None:
                return None
            return self._runs.get(self._latest_run_id)

    def list_runs(self) -> list[RunRecord]:
        with self._lock:
            records = list(self._runs.values())
        return sorted(records, key=lambda record: record.created_at)

    def save_schedule(self, record: ScheduledOperationRecord) -> None:
        with self._lock:
            self._schedules[record.schedule_id] = record

    def get_schedule(self, schedule_id: str) -> ScheduledOperationRecord | None:
        with self._lock:
            return self._schedules.get(schedule_id)

    def list_schedules(self, operation_type: str | None = None) -> list[ScheduledOperationRecord]:
        with self._lock:
            records = list(self._schedules.values())
        if operation_type is not None:
            records = [record for record in records if record.operation_type == operation_type]
        return sorted(records, key=lambda record: record.created_at)

    def save_execution(self, record: ScheduledExecutionRecord) -> None:
        with self._lock:
            self._executions[record.execution_id] = record

    def list_executions(self, schedule_id: str | None = None) -> list[ScheduledExecutionRecord]:
        with self._lock:
            records = list(self._executions.values())
        if schedule_id is not None:
            records = [record for record in records if record.schedule_id == schedule_id]
        return sorted(records, key=lambda record: record.started_at)

    def save_artifacts(self, run_id: str, artifacts: ArtifactsIndex) -> None:
        with self._lock:
            self._artifacts[run_id] = artifacts

    def get_artifacts(self, run_id: str) -> ArtifactsIndex | None:
        with self._lock:
            return self._artifacts.get(run_id)

    def save_operation(self, record: OperationRecord) -> None:
        with self._lock:
            self._operations[record.operation_id] = record
            self._latest_operation_id = record.operation_id

    def get_operation(self, operation_id: str) -> OperationRecord | None:
        with self._lock:
            return self._operations.get(operation_id)

    def get_latest_operation(self) -> OperationRecord | None:
        with self._lock:
            if self._latest_operation_id is None:
                return None
            return self._operations.get(self._latest_operation_id)

    def list_operations(self, operation_type: str | None = None) -> list[OperationRecord]:
        with self._lock:
            records = list(self._operations.values())
        if operation_type is not None:
            records = [record for record in records if record.operation_type == operation_type]
        return sorted(records, key=lambda record: record.started_at)

    def append_log(self, run_id: str, entry: LogEntry) -> None:
        with self._lock:
            self._logs.setdefault(run_id, []).append(entry)

    def get_logs(self, run_id: str) -> list[LogEntry]:
        with self._lock:
            return list(self._logs.get(run_id, []))


class JsonFileRunStore(InMemoryRunStore):
    def __init__(self, state_path: str | Path) -> None:
        self._state_path = Path(state_path)
        super().__init__()
        self._load()

    def _load(self) -> None:
        if not self._state_path.exists():
            return
        payload = json.loads(self._state_path.read_text(encoding='utf-8'))
        with self._lock:
            self._datasets = _parse_record_map(payload.get('datasets', {}), DatasetRecord)
            self._technique_catalog = _parse_record_map(payload.get('technique_catalog', {}), TechniqueCatalogRecord)
            self._methodology_drafts = _parse_record_map(payload.get('methodology_drafts', {}), MethodologyDraftRecord)
            self._latest_methodology_draft_id = payload.get('latest_methodology_draft_id')
            self._autoresearch_campaigns = _parse_record_map(
                payload.get('autoresearch_campaigns', {}),
                AutoresearchCampaignRecord,
            )
            self._latest_autoresearch_campaign_id = payload.get('latest_autoresearch_campaign_id')
            self._autoresearch_iterations = _parse_record_map(
                payload.get('autoresearch_iterations', {}),
                AutoresearchIterationRecord,
            )
            self._latest_autoresearch_iteration_id = payload.get('latest_autoresearch_iteration_id')
            self._autoresearch_decisions = _parse_record_map(
                payload.get('autoresearch_decisions', {}),
                AutoresearchDecisionRecord,
            )
            self._latest_autoresearch_decision_id = payload.get('latest_autoresearch_decision_id')
            self._comparisons = _parse_record_map(payload.get('comparisons', {}), ComparisonRecord)
            self._latest_comparison_id = payload.get('latest_comparison_id')
            self._investigations = _parse_record_map(
                payload.get('investigations', {}),
                InvestigationRecord,
            )
            self._latest_investigation_id = payload.get('latest_investigation_id')
            self._research_sessions = _parse_record_map(
                payload.get('research_sessions', {}),
                ResearchSessionRecord,
            )
            self._latest_research_session_id = payload.get('latest_research_session_id')
            self._intakes = _parse_record_map(payload.get('intakes', {}), IntakeRecord)
            self._latest_intake_id = payload.get('latest_intake_id')
            self._interpretations = _parse_record_map(payload.get('interpretations', {}), InterpretationRecord)
            self._latest_interpretation_id = payload.get('latest_interpretation_id')
            self._replicability_assessments = _parse_record_map(
                payload.get('replicability_assessments', {}),
                ReplicabilityAssessmentRecord,
            )
            self._latest_replicability_assessment_id = payload.get('latest_replicability_assessment_id')
            self._design_drafts = _parse_record_map(payload.get('design_drafts', {}), DesignDraftRecord)
            self._latest_design_draft_id = payload.get('latest_design_draft_id')
            self._research_problems = _parse_record_map(payload.get('research_problems', {}), ResearchProblemRecord)
            self._latest_research_problem_id = payload.get('latest_research_problem_id')
            self._paper_intake_queues = _parse_record_map(
                payload.get('paper_intake_queues', {}),
                PaperIntakeQueueRecord,
            )
            self._latest_paper_intake_queue_id = payload.get('latest_paper_intake_queue_id')
            self._source_documents = _parse_record_map(payload.get('source_documents', {}), SourceDocumentRecord)
            self._latest_source_document_id = payload.get('latest_source_document_id')
            self._runs = _parse_record_map(payload.get('runs', {}), RunRecord)
            self._latest_run_id = payload.get('latest_run_id')
            self._schedules = _parse_record_map(payload.get('schedules', {}), ScheduledOperationRecord)
            self._executions = _parse_record_map(payload.get('executions', {}), ScheduledExecutionRecord)
            self._artifacts = _parse_artifacts_map(payload.get('artifacts', {}))
            self._logs = _parse_logs_map(payload.get('logs', {}))
            self._operations = _parse_record_map(payload.get('operations', {}), OperationRecord)
            self._latest_operation_id = payload.get('latest_operation_id')

    def _flush(self) -> None:
        with self._lock:
            payload = {
                'datasets': {
                    key: record.model_dump(mode='json') for key, record in self._datasets.items()
                },
                'technique_catalog': {
                    key: record.model_dump(mode='json') for key, record in self._technique_catalog.items()
                },
                'methodology_drafts': {
                    key: record.model_dump(mode='json') for key, record in self._methodology_drafts.items()
                },
                'latest_methodology_draft_id': self._latest_methodology_draft_id,
                'autoresearch_campaigns': {
                    key: record.model_dump(mode='json') for key, record in self._autoresearch_campaigns.items()
                },
                'latest_autoresearch_campaign_id': self._latest_autoresearch_campaign_id,
                'autoresearch_iterations': {
                    key: record.model_dump(mode='json') for key, record in self._autoresearch_iterations.items()
                },
                'latest_autoresearch_iteration_id': self._latest_autoresearch_iteration_id,
                'autoresearch_decisions': {
                    key: record.model_dump(mode='json') for key, record in self._autoresearch_decisions.items()
                },
                'latest_autoresearch_decision_id': self._latest_autoresearch_decision_id,
                'comparisons': {
                    key: record.model_dump(mode='json') for key, record in self._comparisons.items()
                },
                'latest_comparison_id': self._latest_comparison_id,
                'investigations': {
                    key: record.model_dump(mode='json') for key, record in self._investigations.items()
                },
                'latest_investigation_id': self._latest_investigation_id,
                'research_sessions': {key: record.model_dump(mode='json') for key, record in self._research_sessions.items()},
                'latest_research_session_id': self._latest_research_session_id,
                'intakes': {key: record.model_dump(mode='json') for key, record in self._intakes.items()},
                'latest_intake_id': self._latest_intake_id,
                'interpretations': {key: record.model_dump(mode='json') for key, record in self._interpretations.items()},
                'latest_interpretation_id': self._latest_interpretation_id,
                'replicability_assessments': {
                    key: record.model_dump(mode='json') for key, record in self._replicability_assessments.items()
                },
                'latest_replicability_assessment_id': self._latest_replicability_assessment_id,
                'design_drafts': {key: record.model_dump(mode='json') for key, record in self._design_drafts.items()},
                'latest_design_draft_id': self._latest_design_draft_id,
                'research_problems': {
                    key: record.model_dump(mode='json') for key, record in self._research_problems.items()
                },
                'latest_research_problem_id': self._latest_research_problem_id,
                'paper_intake_queues': {
                    key: record.model_dump(mode='json') for key, record in self._paper_intake_queues.items()
                },
                'latest_paper_intake_queue_id': self._latest_paper_intake_queue_id,
                'source_documents': {
                    key: record.model_dump(mode='json') for key, record in self._source_documents.items()
                },
                'latest_source_document_id': self._latest_source_document_id,
                'runs': {key: record.model_dump(mode='json') for key, record in self._runs.items()},
                'latest_run_id': self._latest_run_id,
                'schedules': {key: record.model_dump(mode='json') for key, record in self._schedules.items()},
                'executions': {key: record.model_dump(mode='json') for key, record in self._executions.items()},
                'artifacts': {key: record.model_dump(mode='json') for key, record in self._artifacts.items()},
                'logs': {key: [entry.model_dump(mode='json') for entry in entries] for key, entries in self._logs.items()},
                'operations': {key: record.model_dump(mode='json') for key, record in self._operations.items()},
                'latest_operation_id': self._latest_operation_id,
            }
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        self._state_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding='utf-8')

    def save_research_session(self, record: ResearchSessionRecord) -> None:
        super().save_research_session(record)
        self._flush()

    def save_dataset_record(self, record: DatasetRecord) -> None:
        super().save_dataset_record(record)
        self._flush()

    def save_technique_catalog_record(self, record: TechniqueCatalogRecord) -> None:
        super().save_technique_catalog_record(record)
        self._flush()

    def save_methodology_draft(self, record: MethodologyDraftRecord) -> None:
        super().save_methodology_draft(record)
        self._flush()

    def save_autoresearch_campaign(self, record: AutoresearchCampaignRecord) -> None:
        super().save_autoresearch_campaign(record)
        self._flush()

    def save_autoresearch_iteration(self, record: AutoresearchIterationRecord) -> None:
        # model_copy(update=...) does not re-validate, so a caller can hand us
        # a record whose status violates the schema Literal. Re-validate before
        # flushing so contract violations fail at the mutation site instead of
        # poisoning the store and crash-looping on reload.
        validated = AutoresearchIterationRecord.model_validate(record.model_dump())
        super().save_autoresearch_iteration(validated)
        self._flush()

    def save_autoresearch_decision(self, record: AutoresearchDecisionRecord) -> None:
        super().save_autoresearch_decision(record)
        self._flush()

    def save_comparison(self, record: ComparisonRecord) -> None:
        super().save_comparison(record)
        self._flush()

    def save_investigation(self, record: InvestigationRecord) -> None:
        super().save_investigation(record)
        self._flush()

    def save_intake(self, record: IntakeRecord) -> None:
        super().save_intake(record)
        self._flush()

    def save_design_draft(self, record: DesignDraftRecord) -> None:
        super().save_design_draft(record)
        self._flush()

    def save_interpretation(self, record: InterpretationRecord) -> None:
        super().save_interpretation(record)
        self._flush()

    def save_replicability_assessment(self, record: ReplicabilityAssessmentRecord) -> None:
        super().save_replicability_assessment(record)
        self._flush()

    def save_run(self, record: RunRecord) -> None:
        super().save_run(record)
        self._flush()

    def save_research_problem(self, record: ResearchProblemRecord) -> None:
        super().save_research_problem(record)
        self._flush()

    def save_paper_intake_queue(self, record: PaperIntakeQueueRecord) -> None:
        super().save_paper_intake_queue(record)
        self._flush()

    def save_source_document(self, record: SourceDocumentRecord) -> None:
        super().save_source_document(record)
        self._flush()

    def save_schedule(self, record: ScheduledOperationRecord) -> None:
        super().save_schedule(record)
        self._flush()

    def save_execution(self, record: ScheduledExecutionRecord) -> None:
        super().save_execution(record)
        self._flush()

    def save_artifacts(self, run_id: str, artifacts: ArtifactsIndex) -> None:
        super().save_artifacts(run_id, artifacts)
        self._flush()

    def append_log(self, run_id: str, entry: LogEntry) -> None:
        super().append_log(run_id, entry)
        self._flush()

    def save_operation(self, record: OperationRecord) -> None:
        super().save_operation(record)
        self._flush()


class PostgresRunStore(InMemoryRunStore):
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        super().__init__()
        self._ensure_schema()
        self._load()

    def _connect(self):
        psycopg = _import_psycopg()
        return psycopg.connect(self._dsn)

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    '''
                    CREATE TABLE IF NOT EXISTS workflow_state (
                        store_key TEXT PRIMARY KEY,
                        payload JSONB NOT NULL,
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                    '''
                )
                cur.execute('CREATE EXTENSION IF NOT EXISTS vector')
                cur.execute(
                    '''
                    CREATE TABLE IF NOT EXISTS vector_index_items (
                        item_id TEXT PRIMARY KEY,
                        collection TEXT NOT NULL,
                        owner_type TEXT NOT NULL,
                        owner_id TEXT NOT NULL,
                        text_hash TEXT NOT NULL,
                        embedding vector(1536),
                        payload JSONB NOT NULL DEFAULT '{}'::jsonb,
                        artifact_uri TEXT,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                    '''
                )
                cur.execute(
                    '''
                    CREATE INDEX IF NOT EXISTS vector_index_items_owner_idx
                    ON vector_index_items (owner_type, owner_id)
                    '''
                )
                cur.execute(
                    '''
                    CREATE INDEX IF NOT EXISTS vector_index_items_collection_idx
                    ON vector_index_items (collection)
                    '''
                )
            conn.commit()

    def _load(self) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    'SELECT payload FROM workflow_state WHERE store_key = %s',
                    ('default',),
                )
                row = cur.fetchone()
        if not row:
            return
        payload = row[0]
        if isinstance(payload, str):
            payload = json.loads(payload)
        with self._lock:
            self._datasets = _parse_record_map(payload.get('datasets', {}), DatasetRecord)
            self._technique_catalog = _parse_record_map(payload.get('technique_catalog', {}), TechniqueCatalogRecord)
            self._methodology_drafts = _parse_record_map(payload.get('methodology_drafts', {}), MethodologyDraftRecord)
            self._latest_methodology_draft_id = payload.get('latest_methodology_draft_id')
            self._autoresearch_campaigns = _parse_record_map(
                payload.get('autoresearch_campaigns', {}),
                AutoresearchCampaignRecord,
            )
            self._latest_autoresearch_campaign_id = payload.get('latest_autoresearch_campaign_id')
            self._autoresearch_iterations = _parse_record_map(
                payload.get('autoresearch_iterations', {}),
                AutoresearchIterationRecord,
            )
            self._latest_autoresearch_iteration_id = payload.get('latest_autoresearch_iteration_id')
            self._autoresearch_decisions = _parse_record_map(
                payload.get('autoresearch_decisions', {}),
                AutoresearchDecisionRecord,
            )
            self._latest_autoresearch_decision_id = payload.get('latest_autoresearch_decision_id')
            self._comparisons = _parse_record_map(payload.get('comparisons', {}), ComparisonRecord)
            self._latest_comparison_id = payload.get('latest_comparison_id')
            self._investigations = _parse_record_map(
                payload.get('investigations', {}),
                InvestigationRecord,
            )
            self._latest_investigation_id = payload.get('latest_investigation_id')
            self._research_sessions = _parse_record_map(
                payload.get('research_sessions', {}),
                ResearchSessionRecord,
            )
            self._latest_research_session_id = payload.get('latest_research_session_id')
            self._intakes = _parse_record_map(payload.get('intakes', {}), IntakeRecord)
            self._latest_intake_id = payload.get('latest_intake_id')
            self._interpretations = _parse_record_map(payload.get('interpretations', {}), InterpretationRecord)
            self._latest_interpretation_id = payload.get('latest_interpretation_id')
            self._replicability_assessments = _parse_record_map(
                payload.get('replicability_assessments', {}),
                ReplicabilityAssessmentRecord,
            )
            self._latest_replicability_assessment_id = payload.get('latest_replicability_assessment_id')
            self._design_drafts = _parse_record_map(payload.get('design_drafts', {}), DesignDraftRecord)
            self._latest_design_draft_id = payload.get('latest_design_draft_id')
            self._research_problems = _parse_record_map(payload.get('research_problems', {}), ResearchProblemRecord)
            self._latest_research_problem_id = payload.get('latest_research_problem_id')
            self._paper_intake_queues = _parse_record_map(
                payload.get('paper_intake_queues', {}),
                PaperIntakeQueueRecord,
            )
            self._latest_paper_intake_queue_id = payload.get('latest_paper_intake_queue_id')
            self._source_documents = _parse_record_map(payload.get('source_documents', {}), SourceDocumentRecord)
            self._latest_source_document_id = payload.get('latest_source_document_id')
            self._runs = _parse_record_map(payload.get('runs', {}), RunRecord)
            self._latest_run_id = payload.get('latest_run_id')
            self._schedules = _parse_record_map(payload.get('schedules', {}), ScheduledOperationRecord)
            self._executions = _parse_record_map(payload.get('executions', {}), ScheduledExecutionRecord)
            self._artifacts = _parse_artifacts_map(payload.get('artifacts', {}))
            self._logs = _parse_logs_map(payload.get('logs', {}))
            self._operations = _parse_record_map(payload.get('operations', {}), OperationRecord)
            self._latest_operation_id = payload.get('latest_operation_id')

    def _flush(self) -> None:
        with self._lock:
            payload = {
                'datasets': {key: record.model_dump(mode='json') for key, record in self._datasets.items()},
                'technique_catalog': {
                    key: record.model_dump(mode='json') for key, record in self._technique_catalog.items()
                },
                'methodology_drafts': {
                    key: record.model_dump(mode='json') for key, record in self._methodology_drafts.items()
                },
                'latest_methodology_draft_id': self._latest_methodology_draft_id,
                'autoresearch_campaigns': {
                    key: record.model_dump(mode='json') for key, record in self._autoresearch_campaigns.items()
                },
                'latest_autoresearch_campaign_id': self._latest_autoresearch_campaign_id,
                'autoresearch_iterations': {
                    key: record.model_dump(mode='json') for key, record in self._autoresearch_iterations.items()
                },
                'latest_autoresearch_iteration_id': self._latest_autoresearch_iteration_id,
                'autoresearch_decisions': {
                    key: record.model_dump(mode='json') for key, record in self._autoresearch_decisions.items()
                },
                'latest_autoresearch_decision_id': self._latest_autoresearch_decision_id,
                'comparisons': {
                    key: record.model_dump(mode='json') for key, record in self._comparisons.items()
                },
                'latest_comparison_id': self._latest_comparison_id,
                'investigations': {
                    key: record.model_dump(mode='json') for key, record in self._investigations.items()
                },
                'latest_investigation_id': self._latest_investigation_id,
                'research_sessions': {
                    key: record.model_dump(mode='json') for key, record in self._research_sessions.items()
                },
                'latest_research_session_id': self._latest_research_session_id,
                'intakes': {key: record.model_dump(mode='json') for key, record in self._intakes.items()},
                'latest_intake_id': self._latest_intake_id,
                'interpretations': {
                    key: record.model_dump(mode='json') for key, record in self._interpretations.items()
                },
                'latest_interpretation_id': self._latest_interpretation_id,
                'replicability_assessments': {
                    key: record.model_dump(mode='json') for key, record in self._replicability_assessments.items()
                },
                'latest_replicability_assessment_id': self._latest_replicability_assessment_id,
                'design_drafts': {key: record.model_dump(mode='json') for key, record in self._design_drafts.items()},
                'latest_design_draft_id': self._latest_design_draft_id,
                'research_problems': {
                    key: record.model_dump(mode='json') for key, record in self._research_problems.items()
                },
                'latest_research_problem_id': self._latest_research_problem_id,
                'paper_intake_queues': {
                    key: record.model_dump(mode='json') for key, record in self._paper_intake_queues.items()
                },
                'latest_paper_intake_queue_id': self._latest_paper_intake_queue_id,
                'source_documents': {
                    key: record.model_dump(mode='json') for key, record in self._source_documents.items()
                },
                'latest_source_document_id': self._latest_source_document_id,
                'runs': {key: record.model_dump(mode='json') for key, record in self._runs.items()},
                'latest_run_id': self._latest_run_id,
                'schedules': {key: record.model_dump(mode='json') for key, record in self._schedules.items()},
                'executions': {key: record.model_dump(mode='json') for key, record in self._executions.items()},
                'artifacts': {key: record.model_dump(mode='json') for key, record in self._artifacts.items()},
                'logs': {key: [entry.model_dump(mode='json') for entry in entries] for key, entries in self._logs.items()},
                'operations': {key: record.model_dump(mode='json') for key, record in self._operations.items()},
                'latest_operation_id': self._latest_operation_id,
            }
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    '''
                    INSERT INTO workflow_state (store_key, payload, updated_at)
                    VALUES (%s, %s::jsonb, NOW())
                    ON CONFLICT (store_key) DO UPDATE
                    SET payload = EXCLUDED.payload,
                        updated_at = EXCLUDED.updated_at
                    ''',
                    ('default', json.dumps(payload)),
                )
            conn.commit()

    def save_research_session(self, record: ResearchSessionRecord) -> None:
        super().save_research_session(record)
        self._flush()

    def save_dataset_record(self, record: DatasetRecord) -> None:
        super().save_dataset_record(record)
        self._flush()

    def save_technique_catalog_record(self, record: TechniqueCatalogRecord) -> None:
        super().save_technique_catalog_record(record)
        self._flush()

    def save_methodology_draft(self, record: MethodologyDraftRecord) -> None:
        super().save_methodology_draft(record)
        self._flush()

    def save_autoresearch_campaign(self, record: AutoresearchCampaignRecord) -> None:
        super().save_autoresearch_campaign(record)
        self._flush()

    def save_autoresearch_iteration(self, record: AutoresearchIterationRecord) -> None:
        # model_copy(update=...) does not re-validate, so a caller can hand us
        # a record whose status violates the schema Literal. Re-validate before
        # flushing so contract violations fail at the mutation site instead of
        # poisoning the store and crash-looping on reload.
        validated = AutoresearchIterationRecord.model_validate(record.model_dump())
        super().save_autoresearch_iteration(validated)
        self._flush()

    def save_autoresearch_decision(self, record: AutoresearchDecisionRecord) -> None:
        super().save_autoresearch_decision(record)
        self._flush()

    def save_comparison(self, record: ComparisonRecord) -> None:
        super().save_comparison(record)
        self._flush()

    def save_investigation(self, record: InvestigationRecord) -> None:
        super().save_investigation(record)
        self._flush()

    def save_intake(self, record: IntakeRecord) -> None:
        super().save_intake(record)
        self._flush()

    def save_design_draft(self, record: DesignDraftRecord) -> None:
        super().save_design_draft(record)
        self._flush()

    def save_interpretation(self, record: InterpretationRecord) -> None:
        super().save_interpretation(record)
        self._flush()

    def save_replicability_assessment(self, record: ReplicabilityAssessmentRecord) -> None:
        super().save_replicability_assessment(record)
        self._flush()

    def save_run(self, record: RunRecord) -> None:
        super().save_run(record)
        self._flush()

    def save_research_problem(self, record: ResearchProblemRecord) -> None:
        super().save_research_problem(record)
        self._flush()

    def save_paper_intake_queue(self, record: PaperIntakeQueueRecord) -> None:
        super().save_paper_intake_queue(record)
        self._flush()

    def save_source_document(self, record: SourceDocumentRecord) -> None:
        super().save_source_document(record)
        self._flush()

    def save_schedule(self, record: ScheduledOperationRecord) -> None:
        super().save_schedule(record)
        self._flush()

    def save_execution(self, record: ScheduledExecutionRecord) -> None:
        super().save_execution(record)
        self._flush()

    def save_artifacts(self, run_id: str, artifacts: ArtifactsIndex) -> None:
        super().save_artifacts(run_id, artifacts)
        self._flush()

    def append_log(self, run_id: str, entry: LogEntry) -> None:
        super().append_log(run_id, entry)
        self._flush()

    def save_operation(self, record: OperationRecord) -> None:
        super().save_operation(record)
        self._flush()


def create_run_store(
    store_backend: str,
    *,
    state_path: str | Path | None = None,
    postgres_dsn: str | None = None,
) -> RunStore:
    # Factory dispatches to the correct backend. The in-memory store is the
    # default for local development; json and postgres are the durable options.
    if store_backend == 'memory':
        return InMemoryRunStore()
    if store_backend == 'json':
        if state_path is None:
            raise ValueError('json store backend requires state_path')
        return JsonFileRunStore(state_path)
    if store_backend == 'postgres':
        if not postgres_dsn:
            raise ValueError('postgres store backend requires postgres_dsn')
        return PostgresRunStore(postgres_dsn)
    raise ValueError(f'unsupported store backend: {store_backend}')
