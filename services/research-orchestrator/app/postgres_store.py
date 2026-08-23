"""PostgreSQL durable store for the research orchestrator.

SQLite remains useful for isolated tests and the local smoke path.  This store
is the production counterpart: record payloads are JSONB while query and
integrity fields are relational columns.  A transaction-scoped advisory lock
serializes the small control plane's compound mutations (active-run checks and
per-run event sequence allocation) across replicas without relying on process
local locks.
"""

from __future__ import annotations

from collections.abc import Iterable
from contextlib import contextmanager
from datetime import datetime
import json
from types import ModuleType
from typing import Any, Iterator

from .schemas import (
    ActionRecord, AgentName, ApprovalStatus, ArtifactRecord, ContextPacket,
    EventRecord, IngestedDatasetRecord, JobRecord, JobStatus, KnowledgeChunk,
    KnowledgeSource, RunRecord, RunState, SourceType, TERMINAL_STATES,
    TurnKind, TurnRecord, utc_now,
)
from .state_machine import HUMAN_WAIT_STATES, validate_transition
from .storage import ConcurrencyConflict, RecordNotFound


def _import_psycopg() -> ModuleType:
    # Keep SQLite-only tests and local smoke runs importable without the
    # optional Postgres wheel. Production fails at construction, not later.
    import psycopg

    return psycopg


class PostgresStore:
    """Transactional PostgreSQL store with the SqliteStore public surface."""

    def __init__(self, dsn: str | None) -> None:
        if not (dsn or '').strip():
            raise ValueError('postgres store requires a non-empty dsn')
        self._dsn = str(dsn)
        self._psycopg = _import_psycopg()
        self._ensure_schema()

    def _connect(self):
        from psycopg.rows import dict_row

        return self._psycopg.connect(self._dsn, row_factory=dict_row)

    @staticmethod
    def _payload(record: Any):
        from psycopg.types.json import Jsonb

        return Jsonb(record.model_dump(mode='json'))

    @contextmanager
    def transaction(self) -> Iterator[Any]:
        with self._connect() as conn:
            with conn.transaction():
                # A short control-plane lock is intentional. It makes the
                # one-active-run policy and per-run event sequences correct
                # across replicas; jobs/model turns remain outside it.
                conn.execute('SELECT pg_advisory_xact_lock(734882001)')
                yield conn

    def _ensure_schema(self) -> None:
        statements = '''
        CREATE TABLE IF NOT EXISTS orchestrator_runs (
          run_id TEXT PRIMARY KEY, state TEXT NOT NULL, version INTEGER NOT NULL,
          payload JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL);
        CREATE INDEX IF NOT EXISTS orchestrator_runs_state_idx ON orchestrator_runs(state);
        CREATE TABLE IF NOT EXISTS orchestrator_turns (
          turn_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES orchestrator_runs(run_id),
          status TEXT NOT NULL, payload JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL);
        CREATE INDEX IF NOT EXISTS orchestrator_turns_run_idx ON orchestrator_turns(run_id, created_at);
        CREATE TABLE IF NOT EXISTS orchestrator_actions (
          action_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES orchestrator_runs(run_id),
          approval_status TEXT NOT NULL, idempotency_key TEXT NOT NULL UNIQUE,
          payload JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL);
        CREATE INDEX IF NOT EXISTS orchestrator_actions_run_idx ON orchestrator_actions(run_id, created_at);
        CREATE TABLE IF NOT EXISTS orchestrator_jobs (
          job_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES orchestrator_runs(run_id),
          action_id TEXT NOT NULL REFERENCES orchestrator_actions(action_id), status TEXT NOT NULL,
          idempotency_key TEXT NOT NULL UNIQUE, payload JSONB NOT NULL,
          created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL);
        CREATE INDEX IF NOT EXISTS orchestrator_jobs_run_idx ON orchestrator_jobs(run_id, created_at);
        CREATE TABLE IF NOT EXISTS orchestrator_artifacts (
          artifact_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES orchestrator_runs(run_id), job_id TEXT,
          payload JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL);
        CREATE INDEX IF NOT EXISTS orchestrator_artifacts_run_idx ON orchestrator_artifacts(run_id, created_at);
        CREATE TABLE IF NOT EXISTS orchestrator_datasets (
          dataset_id TEXT PRIMARY KEY, payload JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL);
        CREATE TABLE IF NOT EXISTS orchestrator_events (
          event_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES orchestrator_runs(run_id),
          sequence_number INTEGER NOT NULL, source TEXT NOT NULL, event_type TEXT NOT NULL,
          payload JSONB NOT NULL, timestamp TIMESTAMPTZ NOT NULL, UNIQUE(run_id, sequence_number));
        CREATE INDEX IF NOT EXISTS orchestrator_events_run_idx ON orchestrator_events(run_id, sequence_number);
        CREATE TABLE IF NOT EXISTS orchestrator_terminal_run_retries (
          parent_run_id TEXT PRIMARY KEY REFERENCES orchestrator_runs(run_id),
          child_run_id TEXT NOT NULL UNIQUE REFERENCES orchestrator_runs(run_id),
          retry_key TEXT NOT NULL UNIQUE, checkpoint_digest TEXT NOT NULL,
          created_at TIMESTAMPTZ NOT NULL);
        CREATE TABLE IF NOT EXISTS orchestrator_knowledge_sources (
          source_id TEXT PRIMARY KEY, source_type TEXT NOT NULL, canonical_uri TEXT NOT NULL,
          run_scope TEXT, digest TEXT NOT NULL, payload JSONB NOT NULL, ingested_at TIMESTAMPTZ NOT NULL,
          UNIQUE(digest, canonical_uri));
        CREATE INDEX IF NOT EXISTS orchestrator_knowledge_sources_scope_idx ON orchestrator_knowledge_sources(run_scope);
        CREATE TABLE IF NOT EXISTS orchestrator_knowledge_chunks (
          chunk_id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES orchestrator_knowledge_sources(source_id) ON DELETE CASCADE,
          chunk_index INTEGER NOT NULL, text TEXT NOT NULL, payload JSONB NOT NULL);
        CREATE INDEX IF NOT EXISTS orchestrator_knowledge_chunks_source_idx ON orchestrator_knowledge_chunks(source_id, chunk_index);
        CREATE INDEX IF NOT EXISTS orchestrator_knowledge_chunks_fts_idx ON orchestrator_knowledge_chunks USING GIN (to_tsvector('simple', text));
        CREATE TABLE IF NOT EXISTS orchestrator_context_packets (
          packet_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES orchestrator_runs(run_id),
          payload JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL);
        CREATE INDEX IF NOT EXISTS orchestrator_context_packets_run_idx ON orchestrator_context_packets(run_id, created_at);
        '''
        with self._connect() as conn:
            with conn.cursor() as cur:
                for statement in statements.split(';'):
                    if statement.strip():
                        cur.execute(statement)
            conn.commit()

    def ping(self) -> bool:
        with self._connect() as conn:
            return conn.execute('SELECT 1 AS value').fetchone()['value'] == 1

    def _append_event_conn(self, conn: Any, *, run_id: str, source: str, event_type: str, payload: dict[str, Any]) -> EventRecord:
        row = conn.execute('SELECT COALESCE(MAX(sequence_number), 0) + 1 AS sequence_number FROM orchestrator_events WHERE run_id = %s', (run_id,)).fetchone()
        event = EventRecord(sequence_number=int(row['sequence_number']), run_id=run_id, source=source, event_type=event_type, payload=payload)
        conn.execute('INSERT INTO orchestrator_events (event_id, run_id, sequence_number, source, event_type, payload, timestamp) VALUES (%s, %s, %s, %s, %s, %s, %s)', (event.event_id, event.run_id, event.sequence_number, event.source, event.event_type, self._payload(event), event.timestamp))
        return event

    def append_event(self, *, run_id: str, source: str, event_type: str, payload: dict[str, Any] | None = None) -> EventRecord:
        with self.transaction() as conn:
            return self._append_event_conn(conn, run_id=run_id, source=source, event_type=event_type, payload=payload or {})

    def create_run(self, record: RunRecord, *, one_active_run: bool) -> RunRecord:
        with self.transaction() as conn:
            if one_active_run:
                states = [state.value for state in TERMINAL_STATES]
                active = conn.execute('SELECT run_id FROM orchestrator_runs WHERE state <> ALL(%s) LIMIT 1', (states,)).fetchone()
                if active:
                    raise ConcurrencyConflict(f"active run already exists: {active['run_id']}")
            conn.execute('INSERT INTO orchestrator_runs (run_id, state, version, payload, created_at, updated_at) VALUES (%s, %s, %s, %s, %s, %s)', (record.run_id, record.state.value, record.version, self._payload(record), record.created_at, record.updated_at))
            self._append_event_conn(conn, run_id=record.run_id, source='orchestrator', event_type='run.created', payload={'objective': record.objective, 'state': record.state.value})
        return record

    def create_terminal_retry(self, record: RunRecord, *, parent_run_id: str, retry_key: str, checkpoint_digest: str, one_active_run: bool) -> tuple[RunRecord, bool]:
        terminal = {state.value for state in TERMINAL_STATES}
        with self.transaction() as conn:
            existing = conn.execute('SELECT r.child_run_id, r.retry_key, child.state AS child_state FROM orchestrator_terminal_run_retries r JOIN orchestrator_runs child ON child.run_id = r.child_run_id WHERE r.parent_run_id=%s FOR UPDATE OF r', (parent_run_id,)).fetchone()
            if existing and (existing['child_state'] not in terminal or existing['retry_key'] == retry_key):
                row = conn.execute('SELECT payload FROM orchestrator_runs WHERE run_id=%s', (existing['child_run_id'],)).fetchone()
                return self._run(row, str(existing['child_run_id'])), False
            parent = conn.execute('SELECT state FROM orchestrator_runs WHERE run_id=%s FOR UPDATE', (parent_run_id,)).fetchone()
            if parent is None: raise RecordNotFound(parent_run_id)
            if parent['state'] not in terminal:
                raise ConcurrencyConflict('terminal retry source is not terminal')
            if one_active_run:
                active = conn.execute('SELECT run_id FROM orchestrator_runs WHERE state <> ALL(%s) LIMIT 1', ([state.value for state in TERMINAL_STATES],)).fetchone()
                if active: raise ConcurrencyConflict(f"active run already exists: {active['run_id']}")
            conn.execute('INSERT INTO orchestrator_runs (run_id, state, version, payload, created_at, updated_at) VALUES (%s,%s,%s,%s,%s,%s)', (record.run_id, record.state.value, record.version, self._payload(record), record.created_at, record.updated_at))
            if existing is None:
                conn.execute('INSERT INTO orchestrator_terminal_run_retries (parent_run_id, child_run_id, retry_key, checkpoint_digest, created_at) VALUES (%s,%s,%s,%s,%s)', (parent_run_id, record.run_id, retry_key, checkpoint_digest, record.created_at))
            else:
                conn.execute('UPDATE orchestrator_terminal_run_retries SET child_run_id=%s, retry_key=%s, checkpoint_digest=%s, created_at=%s WHERE parent_run_id=%s', (record.run_id, retry_key, checkpoint_digest, record.created_at, parent_run_id))
                superseded_payload = {'parent_run_id': parent_run_id, 'child_run_id': str(existing['child_run_id']), 'superseded_by': record.run_id}
                self._append_event_conn(conn, run_id=str(existing['child_run_id']), source='orchestrator', event_type='run.retry_superseded', payload=superseded_payload)
            payload = {'parent_run_id': parent_run_id, 'child_run_id': record.run_id, 'checkpoint_digest': checkpoint_digest}
            self._append_event_conn(conn, run_id=parent_run_id, source='orchestrator', event_type='run.retry_created', payload=payload)
            self._append_event_conn(conn, run_id=record.run_id, source='orchestrator', event_type='run.created', payload={'objective': record.objective, 'state': record.state.value, 'parent_run_id': parent_run_id})
            self._append_event_conn(conn, run_id=record.run_id, source='orchestrator', event_type='run.retry_created', payload=payload)
        return record, True

    def get_terminal_retry_child(self, parent_run_id: str) -> RunRecord | None:
        with self._connect() as conn:
            row = conn.execute('SELECT orchestrator_runs.payload FROM orchestrator_terminal_run_retries JOIN orchestrator_runs ON orchestrator_runs.run_id = orchestrator_terminal_run_retries.child_run_id WHERE parent_run_id=%s', (parent_run_id,)).fetchone()
        return RunRecord.model_validate(row['payload']) if row else None

    def _run(self, row: dict[str, Any] | None, run_id: str) -> RunRecord:
        if row is None: raise RecordNotFound(run_id)
        return RunRecord.model_validate(row['payload'])

    def get_run(self, run_id: str) -> RunRecord:
        with self._connect() as conn: return self._run(conn.execute('SELECT payload FROM orchestrator_runs WHERE run_id=%s', (run_id,)).fetchone(), run_id)

    def list_runs(self) -> list[RunRecord]:
        with self._connect() as conn: return [RunRecord.model_validate(r['payload']) for r in conn.execute('SELECT payload FROM orchestrator_runs ORDER BY created_at DESC').fetchall()]

    def list_active_runs(self) -> list[RunRecord]:
        with self._connect() as conn:
            return [RunRecord.model_validate(r['payload']) for r in conn.execute('SELECT payload FROM orchestrator_runs WHERE state <> ALL(%s) ORDER BY created_at', ([s.value for s in TERMINAL_STATES],)).fetchall()]

    def _write_run(self, conn: Any, current: RunRecord, updated: RunRecord, expected_version: int) -> None:
        result = conn.execute('UPDATE orchestrator_runs SET state=%s, version=%s, payload=%s, updated_at=%s WHERE run_id=%s AND version=%s', (updated.state.value, updated.version, self._payload(updated), updated.updated_at, updated.run_id, expected_version))
        if result.rowcount != 1: raise ConcurrencyConflict(f'run was updated concurrently: {updated.run_id}')

    def replace_run(self, record: RunRecord, *, expected_version: int) -> RunRecord:
        updated = record.model_copy(update={'version': expected_version + 1, 'updated_at': utc_now()})
        with self.transaction() as conn: self._write_run(conn, record, updated, expected_version)
        return updated

    def reset_methodology_revision_budget(self, run_id: str, *, reason: str) -> RunRecord:
        with self.transaction() as conn:
            row = conn.execute('SELECT payload, version FROM orchestrator_runs WHERE run_id=%s FOR UPDATE', (run_id,)).fetchone(); current = self._run(row, run_id)
            updated = current.model_copy(update={'methodology_revision_count': 0, 'version': int(row['version']) + 1, 'updated_at': utc_now()})
            self._write_run(conn, current, updated, int(row['version']))
            self._append_event_conn(conn, run_id=run_id, source='orchestrator', event_type='methodology.revision_budget_reset', payload={'previous_revision_count': current.methodology_revision_count, 'revision_count': 0, 'reason': reason})
        return updated

    def transition_run(self, run_id: str, target: RunState, *, source: str = 'orchestrator', payload: dict[str, Any] | None = None, updates: dict[str, Any] | None = None) -> RunRecord:
        with self.transaction() as conn:
            row = conn.execute('SELECT payload, version FROM orchestrator_runs WHERE run_id=%s FOR UPDATE', (run_id,)).fetchone(); current = self._run(row, run_id); validate_transition(current.state, target); now = utc_now()
            runtime_updates: dict[str, Any] = {}
            if target in HUMAN_WAIT_STATES and current.state not in HUMAN_WAIT_STATES:
                runtime_updates = {'active_runtime_seconds': current.active_runtime_seconds + (max(0.0, (now - current.active_since).total_seconds()) if current.active_since else 0.0), 'active_since': None}
            elif current.state in HUMAN_WAIT_STATES and target not in HUMAN_WAIT_STATES and target not in TERMINAL_STATES and target != RunState.PAUSED:
                runtime_updates = {'active_since': now}
            updated = current.model_copy(update={**(updates or {}), **runtime_updates, 'state': target, 'version': int(row['version']) + 1, 'updated_at': now})
            self._write_run(conn, current, updated, int(row['version']))
            self._append_event_conn(conn, run_id=run_id, source=source, event_type='run.state_changed', payload={'from': current.state.value, 'to': target.value, **(payload or {})})
        return updated

    def _save_payload(self, table: str, id_column: str, record: Any, *, columns: dict[str, Any], conflict: str = 'update') -> Any:
        keys = [id_column, *columns.keys(), 'payload']; values = [getattr(record, id_column), *columns.values(), self._payload(record)]
        assignments = ', '.join(f'{key}=EXCLUDED.{key}' for key in keys[1:])
        with self.transaction() as conn:
            if conflict == 'return_existing':
                row = conn.execute(f'SELECT payload FROM {table} WHERE idempotency_key=%s', (columns['idempotency_key'],)).fetchone()
                if row: return type(record).model_validate(row['payload'])
            conn.execute(f'INSERT INTO {table} ({", ".join(keys)}) VALUES ({", ".join(["%s"] * len(keys))}) ON CONFLICT ({id_column}) DO UPDATE SET {assignments}', values)
        return record

    def save_turn(self, record: TurnRecord) -> TurnRecord:
        return self._save_payload('orchestrator_turns', 'turn_id', record, columns={'run_id': record.run_id, 'status': record.status, 'created_at': record.created_at, 'updated_at': record.updated_at})
    def list_turns(self, run_id: str) -> list[TurnRecord]:
        with self._connect() as conn: return [TurnRecord.model_validate(r['payload']) for r in conn.execute('SELECT payload FROM orchestrator_turns WHERE run_id=%s ORDER BY created_at', (run_id,)).fetchall()]
    def mark_running_turns_interrupted(self, run_id: str) -> int:
        changed = 0
        for turn in self.list_turns(run_id):
            if turn.status == 'running': self.save_turn(turn.model_copy(update={'status': 'failed', 'error': 'orchestrator restarted during active agent turn', 'updated_at': utc_now()})); changed += 1
        return changed

    def save_action(self, record: ActionRecord) -> ActionRecord:
        return self._save_payload('orchestrator_actions', 'action_id', record, columns={'run_id': record.run_id, 'approval_status': record.approval_status.value, 'idempotency_key': record.idempotency_key, 'created_at': record.created_at, 'updated_at': record.updated_at}, conflict='return_existing')
    def save_action_with_event(self, record: ActionRecord, *, source: str, payload: dict[str, Any]) -> tuple[ActionRecord, bool]:
        with self.transaction() as conn:
            existing = conn.execute('SELECT payload FROM orchestrator_actions WHERE idempotency_key=%s', (record.idempotency_key,)).fetchone()
            if existing:
                return ActionRecord.model_validate(existing['payload']), False
            conn.execute('INSERT INTO orchestrator_actions (action_id, run_id, approval_status, idempotency_key, payload, created_at, updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s)', (record.action_id, record.run_id, record.approval_status.value, record.idempotency_key, self._payload(record), record.created_at, record.updated_at))
            self._append_event_conn(conn, run_id=record.run_id, source=source, event_type='action.proposed', payload=payload)
        return record, True
    def get_action(self, action_id: str) -> ActionRecord:
        with self._connect() as conn:
            row = conn.execute('SELECT payload FROM orchestrator_actions WHERE action_id=%s', (action_id,)).fetchone()
        if not row: raise RecordNotFound(action_id)
        return ActionRecord.model_validate(row['payload'])
    def list_actions(self, run_id: str) -> list[ActionRecord]:
        with self._connect() as conn: return [ActionRecord.model_validate(r['payload']) for r in conn.execute('SELECT payload FROM orchestrator_actions WHERE run_id=%s ORDER BY created_at', (run_id,)).fetchall()]
    def update_action(self, action_id: str, *, approval_status: ApprovalStatus, reviewer: str, reason: str) -> ActionRecord:
        with self.transaction() as conn:
            action = self._action_for_update(conn, action_id)
            if action.approval_status not in {ApprovalStatus.PENDING, ApprovalStatus.AUTOMATICALLY_APPROVED}: raise ConcurrencyConflict(f'action is already terminal: {action.approval_status}')
            updated = action.model_copy(update={'approval_status': approval_status, 'reviewer': reviewer, 'reason': reason, 'updated_at': utc_now()}); self._update_action_conn(conn, updated)
        return updated
    def _action_for_update(self, conn: Any, action_id: str) -> ActionRecord:
        row = conn.execute('SELECT payload FROM orchestrator_actions WHERE action_id=%s FOR UPDATE', (action_id,)).fetchone()
        if not row: raise RecordNotFound(action_id)
        return ActionRecord.model_validate(row['payload'])
    def _update_action_conn(self, conn: Any, record: ActionRecord) -> None:
        conn.execute('UPDATE orchestrator_actions SET approval_status=%s, payload=%s, updated_at=%s WHERE action_id=%s', (record.approval_status.value, self._payload(record), record.updated_at, record.action_id))
    def mark_action_honeydew_approved(self, action_id: str, *, review_turn_id: str) -> ActionRecord:
        with self.transaction() as conn:
            action = self._action_for_update(conn, action_id)
            if action.approval_status != ApprovalStatus.PENDING: raise ConcurrencyConflict(f'action is not pending: {action.approval_status}')
            updated = action.model_copy(update={'honeydew_approved': True, 'honeydew_review_turn_id': review_turn_id, 'updated_at': utc_now()}); self._update_action_conn(conn, updated)
        return updated
    def mark_action_execution_failed(self, action_id: str, *, reason: str) -> ActionRecord:
        with self.transaction() as conn:
            action = self._action_for_update(conn, action_id)
            if action.approval_status != ApprovalStatus.APPROVED: raise ConcurrencyConflict(f'action is not approved: {action.approval_status}')
            updated = action.model_copy(update={'approval_status': ApprovalStatus.EXECUTION_FAILED, 'reason': reason, 'updated_at': utc_now()}); self._update_action_conn(conn, updated)
        return updated

    def create_job_if_absent(self, record: JobRecord) -> tuple[JobRecord, bool]:
        with self.transaction() as conn:
            row = conn.execute('SELECT payload FROM orchestrator_jobs WHERE idempotency_key=%s', (record.idempotency_key,)).fetchone()
            if row: return JobRecord.model_validate(row['payload']), False
            conn.execute('INSERT INTO orchestrator_jobs (job_id, run_id, action_id, status, idempotency_key, payload, created_at, updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)', (record.job_id, record.run_id, record.action_id, record.status.value, record.idempotency_key, self._payload(record), record.created_at, record.updated_at))
        return record, True
    def update_job(self, record: JobRecord) -> JobRecord:
        with self.transaction() as conn:
            result = conn.execute('UPDATE orchestrator_jobs SET status=%s, payload=%s, updated_at=%s WHERE job_id=%s', (record.status.value, self._payload(record), record.updated_at, record.job_id))
            if result.rowcount != 1: raise RecordNotFound(record.job_id)
        return record
    def get_job(self, job_id: str) -> JobRecord:
        with self._connect() as conn: row = conn.execute('SELECT payload FROM orchestrator_jobs WHERE job_id=%s', (job_id,)).fetchone()
        if not row: raise RecordNotFound(job_id)
        return JobRecord.model_validate(row['payload'])
    def list_jobs(self, run_id: str, *, statuses: Iterable[JobStatus] | None = None) -> list[JobRecord]:
        query = 'SELECT payload FROM orchestrator_jobs WHERE run_id=%s'
        params: list[Any] = [run_id]
        if statuses: query += ' AND status = ANY(%s)'; params.append([s.value for s in statuses])
        query += ' ORDER BY created_at'
        with self._connect() as conn: return [JobRecord.model_validate(r['payload']) for r in conn.execute(query, params).fetchall()]

    def save_artifact(self, record: ArtifactRecord) -> ArtifactRecord:
        return self._save_payload('orchestrator_artifacts', 'artifact_id', record, columns={'run_id': record.run_id, 'job_id': record.job_id, 'created_at': record.created_at})
    def list_artifacts(self, run_id: str) -> list[ArtifactRecord]:
        with self._connect() as conn: return [ArtifactRecord.model_validate(r['payload']) for r in conn.execute('SELECT payload FROM orchestrator_artifacts WHERE run_id=%s ORDER BY created_at', (run_id,)).fetchall()]
    def save_dataset(self, record: IngestedDatasetRecord) -> IngestedDatasetRecord:
        return self._save_payload('orchestrator_datasets', 'dataset_id', record, columns={'created_at': record.created_at})
    def get_dataset(self, dataset_id: str) -> IngestedDatasetRecord:
        with self._connect() as conn: row = conn.execute('SELECT payload FROM orchestrator_datasets WHERE dataset_id=%s', (dataset_id,)).fetchone()
        if not row: raise RecordNotFound(dataset_id)
        return IngestedDatasetRecord.model_validate(row['payload'])
    def list_datasets(self) -> list[IngestedDatasetRecord]:
        with self._connect() as conn: return [IngestedDatasetRecord.model_validate(r['payload']) for r in conn.execute('SELECT payload FROM orchestrator_datasets ORDER BY created_at').fetchall()]
    def list_events(self, run_id: str, *, after_sequence: int = 0) -> list[EventRecord]:
        with self._connect() as conn: return [EventRecord.model_validate(r['payload']) for r in conn.execute('SELECT payload FROM orchestrator_events WHERE run_id=%s AND sequence_number>%s ORDER BY sequence_number', (run_id, after_sequence)).fetchall()]

    def save_knowledge_source(self, record: KnowledgeSource) -> KnowledgeSource:
        with self.transaction() as conn:
            conn.execute('INSERT INTO orchestrator_knowledge_sources (source_id, source_type, canonical_uri, run_scope, digest, payload, ingested_at) VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (source_id) DO UPDATE SET source_type=EXCLUDED.source_type, canonical_uri=EXCLUDED.canonical_uri, run_scope=EXCLUDED.run_scope, digest=EXCLUDED.digest, payload=EXCLUDED.payload, ingested_at=EXCLUDED.ingested_at', (record.source_id, record.source_type.value, record.canonical_uri, record.run_scope, record.digest, self._payload(record), record.ingested_at))
        return record
    def get_knowledge_source(self, source_id: str) -> KnowledgeSource:
        with self._connect() as conn: row = conn.execute('SELECT payload FROM orchestrator_knowledge_sources WHERE source_id=%s', (source_id,)).fetchone()
        if not row: raise RecordNotFound(source_id)
        return KnowledgeSource.model_validate(row['payload'])
    def find_knowledge_source(self, *, digest: str, canonical_uri: str) -> KnowledgeSource | None:
        with self._connect() as conn: row = conn.execute('SELECT payload FROM orchestrator_knowledge_sources WHERE digest=%s AND canonical_uri=%s', (digest, canonical_uri)).fetchone()
        return KnowledgeSource.model_validate(row['payload']) if row else None
    def list_knowledge_sources(self, *, source_types: Iterable[SourceType] | None = None, run_scope: str | None = None) -> list[KnowledgeSource]:
        query = 'SELECT payload FROM orchestrator_knowledge_sources'
        params: list[Any] = []
        clauses: list[str] = []
        if run_scope is not None: clauses.append('(run_scope=%s OR run_scope IS NULL)'); params.append(run_scope)
        if source_types: clauses.append('source_type = ANY(%s)'); params.append([x.value for x in source_types])
        if clauses: query += ' WHERE ' + ' AND '.join(clauses)
        query += ' ORDER BY ingested_at, source_id'
        with self._connect() as conn: return [KnowledgeSource.model_validate(r['payload']) for r in conn.execute(query, params).fetchall()]
    def delete_knowledge_source(self, source_id: str) -> bool:
        with self.transaction() as conn: return conn.execute('DELETE FROM orchestrator_knowledge_sources WHERE source_id=%s', (source_id,)).rowcount == 1
    def delete_knowledge_sources_by_digest(self, digest: str) -> int:
        with self.transaction() as conn: return conn.execute('DELETE FROM orchestrator_knowledge_sources WHERE digest=%s', (digest,)).rowcount
    def replace_knowledge_chunks(self, source_id: str, chunks: list[KnowledgeChunk]) -> list[KnowledgeChunk]:
        with self.transaction() as conn:
            conn.execute('DELETE FROM orchestrator_knowledge_chunks WHERE source_id=%s', (source_id,))
            for chunk in chunks: conn.execute('INSERT INTO orchestrator_knowledge_chunks (chunk_id, source_id, chunk_index, text, payload) VALUES (%s,%s,%s,%s,%s)', (chunk.chunk_id, chunk.source_id, chunk.chunk_index, chunk.text, self._payload(chunk)))
        return chunks
    def list_knowledge_chunks(self, source_id: str) -> list[KnowledgeChunk]:
        with self._connect() as conn: return [KnowledgeChunk.model_validate(r['payload']) for r in conn.execute('SELECT payload FROM orchestrator_knowledge_chunks WHERE source_id=%s ORDER BY chunk_index', (source_id,)).fetchall()]
    def search_knowledge_chunks(self, query: str, *, source_ids: list[str] | None = None, limit: int = 10) -> list[dict[str, Any]]:
        # The rank expression and the WHERE predicate each bind the search
        # query. Keep both parameters explicit; psycopg does not reuse a
        # positional placeholder automatically.
        params: list[Any] = [query, query]
        clause = "to_tsvector('simple', text) @@ websearch_to_tsquery('simple', %s)"
        if source_ids: clause += ' AND source_id = ANY(%s)'; params.append(source_ids)
        params.append(limit)
        sql = "SELECT payload, ts_rank_cd(to_tsvector('simple', text), websearch_to_tsquery('simple', %s)) AS rank FROM orchestrator_knowledge_chunks WHERE " + clause + ' ORDER BY rank DESC, chunk_index LIMIT %s'
        with self._connect() as conn: rows = conn.execute(sql, params).fetchall()
        return [{**KnowledgeChunk.model_validate(row['payload']).model_dump(mode='json'), 'rank': float(row['rank'])} for row in rows]
    def save_context_packet(self, packet: ContextPacket) -> ContextPacket:
        return self._save_payload('orchestrator_context_packets', 'packet_id', packet, columns={'run_id': packet.run_id, 'created_at': packet.created_at})
    def get_context_packet(self, packet_id: str) -> ContextPacket:
        with self._connect() as conn: row = conn.execute('SELECT payload FROM orchestrator_context_packets WHERE packet_id=%s', (packet_id,)).fetchone()
        if not row: raise RecordNotFound(packet_id)
        return ContextPacket.model_validate(row['payload'])
    def list_context_packets(self, run_id: str) -> list[ContextPacket]:
        with self._connect() as conn: return [ContextPacket.model_validate(r['payload']) for r in conn.execute('SELECT payload FROM orchestrator_context_packets WHERE run_id=%s ORDER BY created_at, packet_id', (run_id,)).fetchall()]
