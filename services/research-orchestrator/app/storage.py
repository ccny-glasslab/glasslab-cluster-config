"""Durable SQLite store for the research orchestrator.

The store is the single source of truth behind the append-only event log.
Every run state change, action approval, and job write is emitted as an
event in the same transaction that performs the write, so the event log and
the relational rows can never diverge. Records are stored as one JSON
payload column plus a few typed columns that mirror only the fields needed
for queries, ordering, and optimistic-versioning guards.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from contextlib import contextmanager
from datetime import datetime
import json
from pathlib import Path
import sqlite3
from threading import RLock
from typing import Any, Iterator

from .corpus_rag import (
    ChunkVectorMeta,
    CorpusRecord,
    RagChunkRecord,
    RagDocumentRecord,
    RagSectionRecord,
)
from .knowledge_search import or_query
from .schemas import (
    ActionRecord,
    AgentName,
    ApprovalStatus,
    ArtifactRecord,
    ContextPacket,
    ConversationSourceBinding,
    EventRecord,
    IngestedDatasetRecord,
    JobRecord,
    JobStatus,
    KnowledgeChunk,
    KnowledgeSource,
    RunRecord,
    RunState,
    SourceType,
    TERMINAL_STATES,
    TurnKind,
    TurnRecord,
    utc_now,
)
from .state_machine import HUMAN_WAIT_STATES, validate_transition


class RecordNotFound(KeyError):
    pass


class ConcurrencyConflict(RuntimeError):
    pass


def _dump(record: Any) -> str:
    # The full validated record is stored as JSON in the payload column;
    # typed columns exist only where the SQL needs to query, filter, or order.
    return record.model_dump_json()


class SqliteStore:
    """Single-replica durable store with transactional state and event writes."""

    def __init__(self, database_path: str) -> None:
        self.database_path = database_path
        self._lock = RLock()
        path = Path(database_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        # isolation_level=None puts the connection in autocommit mode so the
        # transaction() context manager controls BEGIN/COMMIT explicitly.
        connection = sqlite3.connect(
            self.database_path,
            timeout=30,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        # Enforced per-connection because PRAGMA foreign_keys is not a
        # persistent database property in SQLite.
        connection.execute('PRAGMA foreign_keys=ON')
        # Backstop for cross-process contention; the in-process RLock already
        # serializes writes within one replica.
        connection.execute('PRAGMA busy_timeout=30000')
        return connection

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        # BEGIN IMMEDIATE takes the write lock up front: read-then-write
        # methods (sequence MAX+1, the one-active-run check, run version
        # bumps) never upgrade a read lock to a write lock mid-transaction,
        # which is what avoids deadlocks and makes those check-then-act
        # patterns atomic under the RLock.
        with self._lock:
            connection = self._connect()
            try:
                connection.execute('BEGIN IMMEDIATE')
                yield connection
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    def _ensure_schema(self) -> None:
        with self._connect() as connection:
            # WAL is persistent for the database file; it lets the read-only
            # API connections proceed while a writer holds the transaction.
            connection.execute('PRAGMA journal_mode=WAL')
            # Additive, idempotent DDL doubles as the migration mechanism:
            # every CREATE/INDEX uses IF NOT EXISTS, so a new deployment
            # upgrades an existing file by creating only the missing objects.
            connection.executescript(
                '''
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS runs_state_idx ON runs(state);

                CREATE TABLE IF NOT EXISTS turns (
                    turn_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(run_id),
                    status TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS turns_run_idx ON turns(run_id, created_at);

                CREATE TABLE IF NOT EXISTS actions (
                    action_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(run_id),
                    approval_status TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS actions_run_idx ON actions(run_id, created_at);

                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(run_id),
                    action_id TEXT NOT NULL REFERENCES actions(action_id),
                    status TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS jobs_run_idx ON jobs(run_id, created_at);

                CREATE TABLE IF NOT EXISTS artifacts (
                    artifact_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(run_id),
                    job_id TEXT,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS artifacts_run_idx
                ON artifacts(run_id, created_at);

                CREATE TABLE IF NOT EXISTS datasets (
                    dataset_id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS datasets_created_idx
                ON datasets(created_at);

                CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(run_id),
                    sequence_number INTEGER NOT NULL,
                    source TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    -- Sequence numbers must be gap-free and increasing per
                    -- run; they are allocated under BEGIN IMMEDIATE so the
                    -- UNIQUE constraint only ever catches programmer error.
                    UNIQUE(run_id, sequence_number)
                );
                CREATE INDEX IF NOT EXISTS events_run_idx
                ON events(run_id, sequence_number);

                CREATE TABLE IF NOT EXISTS terminal_run_retries (
                    parent_run_id TEXT PRIMARY KEY REFERENCES runs(run_id),
                    child_run_id TEXT NOT NULL UNIQUE REFERENCES runs(run_id),
                    retry_key TEXT NOT NULL UNIQUE,
                    checkpoint_digest TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                -- Knowledge store tables. The orchestrator has no formal
                -- migration framework; schema drift is handled by additive
                -- CREATE TABLE IF NOT EXISTS statements that run on every
                -- startup, so a new deployment upgrades an existing SQLite
                -- file by creating the new tables and leaving old rows intact.
                -- knowledge_sources is content-addressed: digest is the
                -- natural key for dedup, and source_id remains the stable
                -- surrogate used by the FTS chunk rows.
                CREATE TABLE IF NOT EXISTS knowledge_sources (
                    source_id TEXT PRIMARY KEY,
                    source_type TEXT NOT NULL,
                    canonical_uri TEXT NOT NULL,
                    run_scope TEXT,
                    access_policy TEXT NOT NULL,
                    source_version TEXT,
                    digest TEXT NOT NULL,
                    ingested_at TEXT NOT NULL,
                    index_version TEXT NOT NULL,
                    title TEXT,
                    metadata TEXT NOT NULL,
                    parent_source_id TEXT
                );
                CREATE INDEX IF NOT EXISTS knowledge_sources_type_idx
                ON knowledge_sources(source_type);
                CREATE INDEX IF NOT EXISTS knowledge_sources_scope_idx
                ON knowledge_sources(run_scope);
                CREATE INDEX IF NOT EXISTS knowledge_sources_digest_idx
                ON knowledge_sources(digest);
                -- Dedup guard: identical content re-ingested from the same
                -- canonical URI must resolve to one source row so the
                -- knowledge:// URI stays stable. Distinct URIs with equal
                -- content are deliberately allowed (provenance preserved).
                CREATE UNIQUE INDEX IF NOT EXISTS
                knowledge_sources_digest_uri_uniq
                ON knowledge_sources(digest, canonical_uri);

                CREATE TABLE IF NOT EXISTS knowledge_chunks (
                    chunk_id TEXT PRIMARY KEY,
                    source_id TEXT NOT NULL
                        REFERENCES knowledge_sources(source_id)
                        ON DELETE CASCADE,
                    chunk_index INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    digest TEXT NOT NULL,
                    token_count INTEGER NOT NULL,
                    index_version TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS knowledge_chunks_source_idx
                ON knowledge_chunks(source_id);

                -- Production dense-retrieval vectors. They attach to the
                -- EXISTING knowledge_chunks identity: no secondary chunk
                -- namespace exists for embeddings.
                CREATE TABLE IF NOT EXISTS knowledge_chunk_vectors (
                    chunk_id TEXT PRIMARY KEY REFERENCES knowledge_chunks(chunk_id) ON DELETE CASCADE,
                    vec BLOB NOT NULL,
                    model_id TEXT NOT NULL,
                    revision TEXT NOT NULL,
                    dims INTEGER NOT NULL,
                    index_version TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS knowledge_chunk_vectors_model_idx
                ON knowledge_chunk_vectors(model_id);

                -- Conversation source bindings: operator-bound knowledge
                -- sources per research-chat conversation (Phase 3). JSON
                -- payload row, additive and durable like the other record
                -- tables.
                CREATE TABLE IF NOT EXISTS conversation_bindings (
                    conversation_id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL
                );

                -- FTS5 is the lexical half of hybrid retrieval. chunk_id is
                -- the rowid-style key linking to knowledge_chunks; the rank is
                -- read back to feed the combined lexical/BM25 score in the
                -- knowledge manager.
                CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_chunks_fts
                USING fts5(chunk_id, text);

                CREATE TABLE IF NOT EXISTS context_packets (
                    packet_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(run_id),
                    agent TEXT NOT NULL,
                    turn_number INTEGER NOT NULL,
                    turn_kind TEXT NOT NULL,
                    query TEXT NOT NULL,
                    index_version TEXT NOT NULL,
                    ranked_sources TEXT NOT NULL,
                    exact_text_supplied TEXT,
                    token_budget INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                -- Per-turn context packets are durable so a report can cite
                -- exactly which retrieval grounded a claim (knowledge://context
                -- URIs) even long after the turn finished.
                CREATE INDEX IF NOT EXISTS context_packets_run_idx
                ON context_packets(run_id, created_at);

                -- Corpus-RAG prototype tables (additive; frozen record shapes
                -- live in app/corpus_rag/contracts.py). Documents, sections,
                -- and chunks keep the full validated record in payload JSON;
                -- typed columns exist only for foreign keys, filtering, and
                -- ordering. rag_documents.source_id is UNIQUE because one
                -- source extracts to exactly one document.
                CREATE TABLE IF NOT EXISTS rag_corpora (
                    corpus_id TEXT PRIMARY KEY,
                    slug TEXT NOT NULL UNIQUE,
                    title TEXT,
                    created_at TEXT NOT NULL,
                    metadata TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS rag_corpus_sources (
                    corpus_id TEXT NOT NULL
                        REFERENCES rag_corpora(corpus_id)
                        ON DELETE CASCADE,
                    source_id TEXT NOT NULL
                        REFERENCES knowledge_sources(source_id)
                        ON DELETE CASCADE,
                    added_at TEXT NOT NULL,
                    UNIQUE(corpus_id, source_id)
                );
                CREATE INDEX IF NOT EXISTS rag_corpus_sources_corpus_idx
                ON rag_corpus_sources(corpus_id);

                CREATE TABLE IF NOT EXISTS rag_documents (
                    doc_id TEXT PRIMARY KEY,
                    source_id TEXT NOT NULL UNIQUE
                        REFERENCES knowledge_sources(source_id)
                        ON DELETE CASCADE,
                    payload TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS rag_sections (
                    section_id TEXT PRIMARY KEY,
                    doc_id TEXT NOT NULL
                        REFERENCES rag_documents(doc_id)
                        ON DELETE CASCADE,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS rag_sections_doc_idx
                ON rag_sections(doc_id);

                CREATE TABLE IF NOT EXISTS rag_chunks (
                    chunk_id TEXT PRIMARY KEY,
                    source_id TEXT NOT NULL
                        REFERENCES knowledge_sources(source_id)
                        ON DELETE CASCADE,
                    kind TEXT NOT NULL CHECK(kind IN ('evidence_span', 'section_unit')),
                    chunk_index INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS rag_chunks_source_idx
                ON rag_chunks(source_id, chunk_index);

                -- Lexical half of hybrid retrieval over RAG chunks; mirrors
                -- knowledge_chunks_fts mechanics. chunk_id is UNINDEXED so it
                -- is stored but never tokenized into the matchable text.
                CREATE VIRTUAL TABLE IF NOT EXISTS rag_chunks_fts
                USING fts5(chunk_id UNINDEXED, text);

                CREATE TABLE IF NOT EXISTS rag_chunk_vectors (
                    chunk_id TEXT PRIMARY KEY
                        REFERENCES rag_chunks(chunk_id)
                        ON DELETE CASCADE,
                    vec BLOB NOT NULL,
                    model_id TEXT NOT NULL,
                    revision TEXT NOT NULL,
                    dims INTEGER NOT NULL,
                    index_version TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS rag_chunk_vectors_model_idx
                ON rag_chunk_vectors(model_id);
                '''
            )

    def ping(self) -> bool:
        with self._connect() as connection:
            return connection.execute('SELECT 1').fetchone()[0] == 1

    def _append_event_conn(
        self,
        connection: sqlite3.Connection,
        *,
        run_id: str,
        source: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> EventRecord:
        # Sequence allocation and the insert share one write transaction:
        # with BEGIN IMMEDIATE serializing writers, MAX+1 is authoritative and
        # a rolled-back transaction consumes no sequence number, keeping the
        # per-run log contiguous. The payload column stores the whole event
        # record as JSON, so consumers read it back with no column mapping.
        row = connection.execute(
            '''
            SELECT COALESCE(MAX(sequence_number), 0) + 1
            FROM events WHERE run_id = ?
            ''',
            (run_id,),
        ).fetchone()
        event = EventRecord(
            sequence_number=int(row[0]),
            run_id=run_id,
            source=source,
            event_type=event_type,
            payload=payload,
        )
        connection.execute(
            '''
            INSERT INTO events (
                event_id, run_id, sequence_number, source, event_type,
                payload, timestamp
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ''',
            (
                event.event_id,
                event.run_id,
                event.sequence_number,
                event.source,
                event.event_type,
                _dump(event),
                event.timestamp.isoformat(),
            ),
        )
        return event

    def append_event(
        self,
        *,
        run_id: str,
        source: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
    ) -> EventRecord:
        with self.transaction() as connection:
            return self._append_event_conn(
                connection,
                run_id=run_id,
                source=source,
                event_type=event_type,
                payload=payload or {},
            )

    def create_run(self, record: RunRecord, *, one_active_run: bool) -> RunRecord:
        terminal = tuple(state.value for state in TERMINAL_STATES)
        placeholders = ','.join('?' for _ in terminal)
        with self.transaction() as connection:
            # The one-active-run invariant is enforced here by ordering the
            # check and the INSERT inside one write transaction (BEGIN
            # IMMEDIATE), not by any database constraint. It therefore holds
            # only when callers pass one_active_run=True, and only across
            # paths that create runs through this store.
            if one_active_run:
                active = connection.execute(
                    f'SELECT run_id FROM runs WHERE state NOT IN ({placeholders}) LIMIT 1',
                    terminal,
                ).fetchone()
                if active is not None:
                    raise ConcurrencyConflict(
                        f'active run already exists: {active["run_id"]}'
                    )
            connection.execute(
                '''
                INSERT INTO runs (
                    run_id, state, version, payload, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ''',
                (
                    record.run_id,
                    record.state.value,
                    record.version,
                    _dump(record),
                    record.created_at.isoformat(),
                    record.updated_at.isoformat(),
                ),
            )
            self._append_event_conn(
                connection,
                run_id=record.run_id,
                source='orchestrator',
                event_type='run.created',
                payload={'objective': record.objective, 'state': record.state.value},
            )
        return record

    def create_terminal_retry(
        self, record: RunRecord, *, parent_run_id: str, retry_key: str,
        checkpoint_digest: str, one_active_run: bool,
    ) -> tuple[RunRecord, bool]:
        """Atomically point the parent's retry slot at its newest child."""
        terminal = tuple(state.value for state in TERMINAL_STATES)
        placeholders = ','.join('?' for _ in terminal)
        with self.transaction() as connection:
            existing = connection.execute(
                '''SELECT terminal_run_retries.child_run_id AS child_run_id,
                          terminal_run_retries.retry_key AS retry_key,
                          runs.state AS child_state
                     FROM terminal_run_retries
                     JOIN runs ON runs.run_id = terminal_run_retries.child_run_id
                    WHERE terminal_run_retries.parent_run_id = ?''',
                (parent_run_id,),
            ).fetchone()
            if existing is not None and (
                existing['child_state'] not in terminal
                or existing['retry_key'] == retry_key
            ):
                return self.get_run(str(existing['child_run_id'])), False
            parent = connection.execute(
                'SELECT state FROM runs WHERE run_id = ?', (parent_run_id,)
            ).fetchone()
            if parent is None:
                raise RecordNotFound(parent_run_id)
            if parent['state'] not in terminal:
                raise ConcurrencyConflict('terminal retry source is not terminal')
            if one_active_run:
                active = connection.execute(
                    f'SELECT run_id FROM runs WHERE state NOT IN ({placeholders}) LIMIT 1',
                    terminal,
                ).fetchone()
                if active is not None:
                    raise ConcurrencyConflict(f'active run already exists: {active["run_id"]}')
            connection.execute(
                'INSERT INTO runs (run_id, state, version, payload, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)',
                (record.run_id, record.state.value, record.version, _dump(record), record.created_at.isoformat(), record.updated_at.isoformat()),
            )
            if existing is None:
                connection.execute(
                    'INSERT INTO terminal_run_retries (parent_run_id, child_run_id, retry_key, checkpoint_digest, created_at) VALUES (?, ?, ?, ?, ?)',
                    (parent_run_id, record.run_id, retry_key, checkpoint_digest, record.created_at.isoformat()),
                )
            else:
                connection.execute(
                    '''UPDATE terminal_run_retries
                          SET child_run_id = ?, retry_key = ?, checkpoint_digest = ?, created_at = ?
                        WHERE parent_run_id = ?''',
                    (record.run_id, retry_key, checkpoint_digest, record.created_at.isoformat(), parent_run_id),
                )
                superseded_payload = {
                    'parent_run_id': parent_run_id,
                    'child_run_id': str(existing['child_run_id']),
                    'superseded_by': record.run_id,
                }
                self._append_event_conn(
                    connection,
                    run_id=str(existing['child_run_id']),
                    source='orchestrator',
                    event_type='run.retry_superseded',
                    payload=superseded_payload,
                )
            payload = {'parent_run_id': parent_run_id, 'child_run_id': record.run_id, 'checkpoint_digest': checkpoint_digest}
            self._append_event_conn(connection, run_id=parent_run_id, source='orchestrator', event_type='run.retry_created', payload=payload)
            self._append_event_conn(connection, run_id=record.run_id, source='orchestrator', event_type='run.created', payload={'objective': record.objective, 'state': record.state.value, 'parent_run_id': parent_run_id})
            self._append_event_conn(connection, run_id=record.run_id, source='orchestrator', event_type='run.retry_created', payload=payload)
        return record, True

    def get_terminal_retry_child(self, parent_run_id: str) -> RunRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                '''SELECT runs.payload FROM terminal_run_retries
                   JOIN runs ON runs.run_id = terminal_run_retries.child_run_id
                   WHERE parent_run_id = ?''',
                (parent_run_id,),
            ).fetchone()
        return RunRecord.model_validate_json(row['payload']) if row else None

    def get_run(self, run_id: str) -> RunRecord:
        with self._connect() as connection:
            row = connection.execute(
                'SELECT payload FROM runs WHERE run_id = ?',
                (run_id,),
            ).fetchone()
        if row is None:
            raise RecordNotFound(run_id)
        return RunRecord.model_validate_json(row['payload'])

    def list_runs(self) -> list[RunRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                'SELECT payload FROM runs ORDER BY created_at DESC'
            ).fetchall()
        return [RunRecord.model_validate_json(row['payload']) for row in rows]

    def list_active_runs(self) -> list[RunRecord]:
        terminal = tuple(state.value for state in TERMINAL_STATES)
        placeholders = ','.join('?' for _ in terminal)
        with self._connect() as connection:
            rows = connection.execute(
                f'''
                SELECT payload FROM runs
                WHERE state NOT IN ({placeholders})
                ORDER BY created_at
                ''',
                terminal,
            ).fetchall()
        return [RunRecord.model_validate_json(row['payload']) for row in rows]

    def replace_run(self, record: RunRecord, *, expected_version: int) -> RunRecord:
        # This is the genuine optimistic-concurrency path: the caller reads a
        # version outside the lock (e.g. get_run), works on the record, then
        # replays the write guarded by WHERE version = expected_version. A
        # concurrent writer between the read and the UPDATE makes rowcount 0,
        # which raises instead of silently losing the update.
        now = utc_now()
        updated = record.model_copy(
            update={'version': expected_version + 1, 'updated_at': now}
        )
        with self.transaction() as connection:
            cursor = connection.execute(
                '''
                UPDATE runs
                SET state = ?, version = ?, payload = ?, updated_at = ?
                WHERE run_id = ? AND version = ?
                ''',
                (
                    updated.state.value,
                    updated.version,
                    _dump(updated),
                    updated.updated_at.isoformat(),
                    updated.run_id,
                    expected_version,
                ),
            )
            if cursor.rowcount != 1:
                raise ConcurrencyConflict(
                    f'run was updated concurrently: {record.run_id}'
                )
        return updated

    def reset_methodology_revision_budget(
        self,
        run_id: str,
        *,
        reason: str,
    ) -> RunRecord:
        # Unlike replace_run, this re-reads the row inside the already-held
        # write transaction, so the WHERE version = row['version'] guard is
        # defensive only (no writer can interleave); the budget reset and its
        # event are committed atomically.
        with self.transaction() as connection:
            row = connection.execute(
                'SELECT payload, version FROM runs WHERE run_id = ?',
                (run_id,),
            ).fetchone()
            if row is None:
                raise RecordNotFound(run_id)
            current = RunRecord.model_validate_json(row['payload'])
            now = utc_now()
            updated = current.model_copy(
                update={
                    'methodology_revision_count': 0,
                    'version': int(row['version']) + 1,
                    'updated_at': now,
                }
            )
            cursor = connection.execute(
                '''
                UPDATE runs
                SET state = ?, version = ?, payload = ?, updated_at = ?
                WHERE run_id = ? AND version = ?
                ''',
                (
                    updated.state.value,
                    updated.version,
                    _dump(updated),
                    updated.updated_at.isoformat(),
                    run_id,
                    int(row['version']),
                ),
            )
            if cursor.rowcount != 1:
                raise ConcurrencyConflict(
                    f'run was updated concurrently: {run_id}'
                )
            self._append_event_conn(
                connection,
                run_id=run_id,
                source='orchestrator',
                event_type='methodology.revision_budget_reset',
                payload={
                    'previous_revision_count': (
                        current.methodology_revision_count
                    ),
                    'revision_count': 0,
                    'reason': reason,
                },
            )
        return updated

    def transition_run(
        self,
        run_id: str,
        target: RunState,
        *,
        source: str = 'orchestrator',
        payload: dict[str, Any] | None = None,
        updates: dict[str, Any] | None = None,
    ) -> RunRecord:
        with self.transaction() as connection:
            row = connection.execute(
                'SELECT payload, version FROM runs WHERE run_id = ?',
                (run_id,),
            ).fetchone()
            if row is None:
                raise RecordNotFound(run_id)
            current = RunRecord.model_validate_json(row['payload'])
            validate_transition(current.state, target)
            now = utc_now()
            runtime_updates: dict[str, Any] = {}
            # The run clock is stored as a base (active_runtime_seconds) plus
            # an open interval (active_since). Entering a human-wait state
            # closes the interval into the base; leaving a human-wait state
            # for work reopens it. PAUSED and terminal transitions are handled
            # by the engine (pause/resume/check_turn_budget) via `updates`, so
            # this method deliberately stays out of those paths.
            if (
                target in HUMAN_WAIT_STATES
                and current.state not in HUMAN_WAIT_STATES
            ):
                active_runtime = current.active_runtime_seconds
                if current.active_since is not None:
                    active_runtime += max(
                        0.0,
                        (now - current.active_since).total_seconds(),
                    )
                runtime_updates = {
                    'active_runtime_seconds': active_runtime,
                    'active_since': None,
                }
            elif (
                current.state in HUMAN_WAIT_STATES
                and target not in HUMAN_WAIT_STATES
                and target not in TERMINAL_STATES
                and target != RunState.PAUSED
            ):
                runtime_updates = {'active_since': now}
            changed = current.model_copy(
                update={
                    **(updates or {}),
                    **runtime_updates,
                    'state': target,
                    'version': int(row['version']) + 1,
                    'updated_at': now,
                }
            )
            cursor = connection.execute(
                '''
                UPDATE runs
                SET state = ?, version = ?, payload = ?, updated_at = ?
                WHERE run_id = ? AND version = ?
                ''',
                (
                    target.value,
                    changed.version,
                    _dump(changed),
                    now.isoformat(),
                    run_id,
                    row['version'],
                ),
            )
            if cursor.rowcount != 1:
                raise ConcurrencyConflict(f'run was updated concurrently: {run_id}')
            # The state change and its event commit together, so the event log
            # is the authoritative record of the transition.
            self._append_event_conn(
                connection,
                run_id=run_id,
                source=source,
                event_type='run.state_changed',
                payload={
                    'from': current.state.value,
                    'to': target.value,
                    **(payload or {}),
                },
            )
        return changed

    def save_turn(self, record: TurnRecord) -> TurnRecord:
        # UPSERT makes turn writes idempotent: recovery and retries may
        # re-deliver a turn record and must land on the same row.
        with self.transaction() as connection:
            connection.execute(
                '''
                INSERT INTO turns (
                    turn_id, run_id, status, payload, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(turn_id) DO UPDATE SET
                    status = excluded.status,
                    payload = excluded.payload,
                    updated_at = excluded.updated_at
                ''',
                (
                    record.turn_id,
                    record.run_id,
                    record.status,
                    _dump(record),
                    record.created_at.isoformat(),
                    record.updated_at.isoformat(),
                ),
            )
        return record

    def list_turns(self, run_id: str) -> list[TurnRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                'SELECT payload FROM turns WHERE run_id = ? ORDER BY created_at',
                (run_id,),
            ).fetchall()
        return [TurnRecord.model_validate_json(row['payload']) for row in rows]

    def mark_running_turns_interrupted(self, run_id: str) -> int:
        # Recovery sweep: turns left 'running' by a crashed process are marked
        # failed so they are never resumed as if they completed. Each turn is
        # rewritten in its own transaction; a crash mid-sweep just re-runs the
        # sweep, which is safe because finished turns are skipped.
        changed = 0
        for turn in self.list_turns(run_id):
            if turn.status != 'running':
                continue
            updated = turn.model_copy(
                update={
                    'status': 'failed',
                    'error': 'orchestrator restarted during active agent turn',
                    'updated_at': utc_now(),
                }
            )
            self.save_turn(updated)
            changed += 1
        return changed

    def save_action(self, record: ActionRecord) -> ActionRecord:
        # The idempotency key is the retry guard: a re-submitted action
        # resolves to the originally stored record (the winner) and any
        # differences in the new copy are ignored.
        with self.transaction() as connection:
            existing = connection.execute(
                'SELECT payload FROM actions WHERE idempotency_key = ?',
                (record.idempotency_key,),
            ).fetchone()
            if existing is not None:
                return ActionRecord.model_validate_json(existing['payload'])
            connection.execute(
                '''
                INSERT INTO actions (
                    action_id, run_id, approval_status, idempotency_key,
                    payload, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ''',
                (
                    record.action_id,
                    record.run_id,
                    record.approval_status.value,
                    record.idempotency_key,
                    _dump(record),
                    record.created_at.isoformat(),
                    record.updated_at.isoformat(),
                ),
            )
        return record

    def save_action_with_event(
        self, record: ActionRecord, *, source: str, payload: dict[str, Any],
    ) -> tuple[ActionRecord, bool]:
        """Persist an action and its proposal event in one transaction."""
        with self.transaction() as connection:
            existing = connection.execute(
                'SELECT payload FROM actions WHERE idempotency_key = ?',
                (record.idempotency_key,),
            ).fetchone()
            if existing is not None:
                return ActionRecord.model_validate_json(existing['payload']), False
            connection.execute(
                '''INSERT INTO actions (action_id, run_id, approval_status,
                   idempotency_key, payload, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)''',
                (record.action_id, record.run_id, record.approval_status.value,
                 record.idempotency_key, _dump(record),
                 record.created_at.isoformat(), record.updated_at.isoformat()),
            )
            self._append_event_conn(
                connection, run_id=record.run_id, source=source,
                event_type='action.proposed', payload=payload,
            )
        return record, True

    def update_action(
        self,
        action_id: str,
        *,
        approval_status: ApprovalStatus,
        reviewer: str,
        reason: str,
    ) -> ActionRecord:
        with self.transaction() as connection:
            row = connection.execute(
                'SELECT payload FROM actions WHERE action_id = ?',
                (action_id,),
            ).fetchone()
            if row is None:
                raise RecordNotFound(action_id)
            action = ActionRecord.model_validate_json(row['payload'])
            # Approval idempotency: only PENDING and AUTOMATICALLY_APPROVED
            # actions may be finalized by a human, and the status check runs
            # inside the write transaction, so a second approval of the same
            # action always raises instead of applying twice.
            if action.approval_status not in {
                ApprovalStatus.PENDING,
                ApprovalStatus.AUTOMATICALLY_APPROVED,
            }:
                raise ConcurrencyConflict(
                    f'action is already terminal: {action.approval_status}'
                )
            updated = action.model_copy(
                update={
                    'approval_status': approval_status,
                    'reviewer': reviewer,
                    'reason': reason,
                    'updated_at': utc_now(),
                }
            )
            connection.execute(
                '''
                UPDATE actions
                SET approval_status = ?, payload = ?, updated_at = ?
                WHERE action_id = ?
                ''',
                (
                    updated.approval_status.value,
                    _dump(updated),
                    updated.updated_at.isoformat(),
                    action_id,
                ),
            )
        return updated

    def mark_action_honeydew_approved(
        self,
        action_id: str,
        *,
        review_turn_id: str,
    ) -> ActionRecord:
        with self.transaction() as connection:
            row = connection.execute(
                'SELECT payload FROM actions WHERE action_id = ?',
                (action_id,),
            ).fetchone()
            if row is None:
                raise RecordNotFound(action_id)
            action = ActionRecord.model_validate_json(row['payload'])
            # Two-stage approval: Honeydew's sign-off is recorded as a flag
            # while the action deliberately stays PENDING, waiting for the
            # human half of the gate.
            if action.approval_status != ApprovalStatus.PENDING:
                raise ConcurrencyConflict(
                    f'action is not pending: {action.approval_status}'
                )
            updated = action.model_copy(
                update={
                    'honeydew_approved': True,
                    'honeydew_review_turn_id': review_turn_id,
                    'updated_at': utc_now(),
                }
            )
            connection.execute(
                '''
                UPDATE actions
                SET payload = ?, updated_at = ?
                WHERE action_id = ?
                ''',
                (
                    _dump(updated),
                    updated.updated_at.isoformat(),
                    action_id,
                ),
            )
        return updated

    def mark_action_execution_failed(
        self,
        action_id: str,
        *,
        reason: str,
    ) -> ActionRecord:
        with self.transaction() as connection:
            row = connection.execute(
                'SELECT payload FROM actions WHERE action_id = ?',
                (action_id,),
            ).fetchone()
            if row is None:
                raise RecordNotFound(action_id)
            action = ActionRecord.model_validate_json(row['payload'])
            # Only an APPROVED action may be marked execution-failed; the
            # terminal status prevents the action from ever being executed.
            if action.approval_status != ApprovalStatus.APPROVED:
                raise ConcurrencyConflict(
                    f'action is not approved: {action.approval_status}'
                )
            updated = action.model_copy(
                update={
                    'approval_status': ApprovalStatus.EXECUTION_FAILED,
                    'reason': reason,
                    'updated_at': utc_now(),
                }
            )
            connection.execute(
                '''
                UPDATE actions
                SET approval_status = ?, payload = ?, updated_at = ?
                WHERE action_id = ?
                ''',
                (
                    updated.approval_status.value,
                    _dump(updated),
                    updated.updated_at.isoformat(),
                    action_id,
                ),
            )
        return updated

    def get_action(self, action_id: str) -> ActionRecord:
        with self._connect() as connection:
            row = connection.execute(
                'SELECT payload FROM actions WHERE action_id = ?',
                (action_id,),
            ).fetchone()
        if row is None:
            raise RecordNotFound(action_id)
        return ActionRecord.model_validate_json(row['payload'])

    def list_actions(self, run_id: str) -> list[ActionRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                'SELECT payload FROM actions WHERE run_id = ? ORDER BY created_at',
                (run_id,),
            ).fetchall()
        return [ActionRecord.model_validate_json(row['payload']) for row in rows]

    def create_job_if_absent(self, record: JobRecord) -> tuple[JobRecord, bool]:
        # Returns (stored, False) when the idempotency key already exists so a
        # retry after a crash never submits a duplicate cluster job.
        with self.transaction() as connection:
            existing = connection.execute(
                'SELECT payload FROM jobs WHERE idempotency_key = ?',
                (record.idempotency_key,),
            ).fetchone()
            if existing is not None:
                return JobRecord.model_validate_json(existing['payload']), False
            connection.execute(
                '''
                INSERT INTO jobs (
                    job_id, run_id, action_id, status, idempotency_key,
                    payload, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ''',
                (
                    record.job_id,
                    record.run_id,
                    record.action_id,
                    record.status.value,
                    record.idempotency_key,
                    _dump(record),
                    record.created_at.isoformat(),
                    record.updated_at.isoformat(),
                ),
            )
        return record, True

    def update_job(self, record: JobRecord) -> JobRecord:
        # Blind write keyed on job_id only (no version guard): the job watcher
        # is the sole writer and re-reads before every update, so the rowcount
        # check is purely a not-found signal.
        updated = record.model_copy(update={'updated_at': utc_now()})
        with self.transaction() as connection:
            cursor = connection.execute(
                '''
                UPDATE jobs
                SET status = ?, payload = ?, updated_at = ?
                WHERE job_id = ?
                ''',
                (
                    updated.status.value,
                    _dump(updated),
                    updated.updated_at.isoformat(),
                    updated.job_id,
                ),
            )
            if cursor.rowcount != 1:
                raise RecordNotFound(updated.job_id)
        return updated

    def get_job(self, job_id: str) -> JobRecord:
        with self._connect() as connection:
            row = connection.execute(
                'SELECT payload FROM jobs WHERE job_id = ?',
                (job_id,),
            ).fetchone()
        if row is None:
            raise RecordNotFound(job_id)
        return JobRecord.model_validate_json(row['payload'])

    def list_jobs(
        self,
        run_id: str,
        *,
        statuses: Iterable[JobStatus] | None = None,
    ) -> list[JobRecord]:
        parameters: list[Any] = [run_id]
        query = 'SELECT payload FROM jobs WHERE run_id = ?'
        if statuses:
            values = [status.value for status in statuses]
            query += ' AND status IN (' + ','.join('?' for _ in values) + ')'
            parameters.extend(values)
        query += ' ORDER BY created_at, job_id'
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [JobRecord.model_validate_json(row['payload']) for row in rows]

    def save_artifact(self, record: ArtifactRecord) -> ArtifactRecord:
        # INSERT OR IGNORE: artifacts are append-only and immutable, so a
        # re-delivered artifact id keeps the first write.
        with self.transaction() as connection:
            connection.execute(
                '''
                INSERT OR IGNORE INTO artifacts (
                    artifact_id, run_id, job_id, payload, created_at
                ) VALUES (?, ?, ?, ?, ?)
                ''',
                (
                    record.artifact_id,
                    record.run_id,
                    record.job_id,
                    _dump(record),
                    record.created_at.isoformat(),
                ),
            )
        return record

    def list_artifacts(self, run_id: str) -> list[ArtifactRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                '''
                SELECT payload FROM artifacts
                WHERE run_id = ? ORDER BY created_at, artifact_id
                ''',
                (run_id,),
            ).fetchall()
        return [ArtifactRecord.model_validate_json(row['payload']) for row in rows]

    def save_dataset(
        self,
        record: IngestedDatasetRecord,
    ) -> IngestedDatasetRecord:
        with self.transaction() as connection:
            connection.execute(
                '''
                INSERT OR IGNORE INTO datasets (
                    dataset_id, payload, created_at
                ) VALUES (?, ?, ?)
                ''',
                (
                    record.dataset_id,
                    _dump(record),
                    record.created_at.isoformat(),
                ),
            )
        # Re-read the stored row so a duplicate ingest returns the canonical
        # record (first write wins) rather than the caller's copy.
        return self.get_dataset(record.dataset_id)

    def get_dataset(self, dataset_id: str) -> IngestedDatasetRecord:
        with self._connect() as connection:
            row = connection.execute(
                'SELECT payload FROM datasets WHERE dataset_id = ?',
                (dataset_id,),
            ).fetchone()
        if row is None:
            raise RecordNotFound(dataset_id)
        return IngestedDatasetRecord.model_validate_json(row['payload'])

    def list_datasets(self) -> list[IngestedDatasetRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                'SELECT payload FROM datasets ORDER BY created_at, dataset_id'
            ).fetchall()
        return [
            IngestedDatasetRecord.model_validate_json(row['payload'])
            for row in rows
        ]

    def list_events(
        self,
        run_id: str,
        *,
        after_sequence: int = 0,
    ) -> list[EventRecord]:
        # Cursor-style read for incremental consumers (SSE). Ordering by
        # sequence_number equals insertion order because appends are
        # serialized by the write transaction.
        with self._connect() as connection:
            rows = connection.execute(
                '''
                SELECT payload FROM events
                WHERE run_id = ? AND sequence_number > ?
                ORDER BY sequence_number
                ''',
                (run_id, after_sequence),
            ).fetchall()
        return [EventRecord.model_validate_json(row['payload']) for row in rows]

    def save_conversation_binding(
        self,
        record: ConversationSourceBinding,
    ) -> ConversationSourceBinding:
        with self.transaction() as connection:
            connection.execute(
                '''
                INSERT INTO conversation_bindings (conversation_id, payload)
                VALUES (?, ?)
                ON CONFLICT(conversation_id) DO UPDATE SET
                    payload = excluded.payload
                ''',
                (record.conversation_id, json.dumps(record.model_dump(mode='json'))),
            )
        return record

    def get_conversation_binding(
        self,
        conversation_id: str,
    ) -> ConversationSourceBinding | None:
        with self._connect() as connection:
            row = connection.execute(
                'SELECT payload FROM conversation_bindings'
                ' WHERE conversation_id = ?',
                (conversation_id,),
            ).fetchone()
        if row is None:
            return None
        return ConversationSourceBinding.model_validate(json.loads(row['payload']))

    def bind_conversation_sources(
        self,
        conversation_id: str,
        source_ids: list[str],
        *,
        created_by: str = 'operator',
    ) -> ConversationSourceBinding:
        existing = self.get_conversation_binding(conversation_id)
        merged = sorted(set((existing.source_ids if existing else []) + source_ids))
        binding = ConversationSourceBinding(
            conversation_id=conversation_id,
            source_ids=merged,
            created_by=existing.created_by if existing else created_by,
            created_at=existing.created_at if existing else utc_now(),
            updated_at=utc_now(),
        )
        return self.save_conversation_binding(binding)

    def unbind_conversation_source(
        self,
        conversation_id: str,
        source_id: str,
    ) -> ConversationSourceBinding | None:
        existing = self.get_conversation_binding(conversation_id)
        if existing is None:
            return None
        remaining = [sid for sid in existing.source_ids if sid != source_id]
        if remaining == existing.source_ids:
            return existing
        binding = existing.model_copy(
            update={'source_ids': remaining, 'updated_at': utc_now()}
        )
        return self.save_conversation_binding(binding)

    def save_knowledge_source(self, record: KnowledgeSource) -> KnowledgeSource:
        with self.transaction() as connection:
            connection.execute(
                '''
                INSERT INTO knowledge_sources (
                    source_id, source_type, canonical_uri, run_scope,
                    access_policy, source_version, digest, ingested_at,
                    index_version, title, metadata, parent_source_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_id) DO UPDATE SET
                    source_type = excluded.source_type,
                    canonical_uri = excluded.canonical_uri,
                    run_scope = excluded.run_scope,
                    access_policy = excluded.access_policy,
                    source_version = excluded.source_version,
                    digest = excluded.digest,
                    ingested_at = excluded.ingested_at,
                    index_version = excluded.index_version,
                    title = excluded.title,
                    metadata = excluded.metadata,
                    parent_source_id = excluded.parent_source_id
                ''',
                (
                    record.source_id,
                    record.source_type.value,
                    record.canonical_uri,
                    record.run_scope,
                    record.access_policy,
                    record.source_version,
                    record.digest,
                    record.ingested_at.isoformat(),
                    record.index_version,
                    record.title,
                    json.dumps(record.metadata, sort_keys=True),
                    record.parent_source_id,
                ),
            )
        return record

    def get_knowledge_source(self, source_id: str) -> KnowledgeSource:
        with self._connect() as connection:
            row = connection.execute(
                'SELECT * FROM knowledge_sources WHERE source_id = ?',
                (source_id,),
            ).fetchone()
        if row is None:
            raise RecordNotFound(source_id)
        return self._knowledge_source_from_row(row)

    def find_knowledge_source(
        self,
        *,
        digest: str,
        canonical_uri: str,
    ) -> KnowledgeSource | None:
        """Resolve an existing source by content digest and canonical URI.

        Used by ingestion to deduplicate re-ingested approved sources: identical
        content from the same URI returns the original row instead of creating a
        second source with a new evidence URI.
        """
        with self._connect() as connection:
            row = connection.execute(
                'SELECT * FROM knowledge_sources '
                'WHERE digest = ? AND canonical_uri = ?',
                (digest, canonical_uri),
            ).fetchone()
        if row is None:
            return None
        return self._knowledge_source_from_row(row)

    def list_knowledge_sources(
        self,
        *,
        source_types: Iterable[SourceType] | None = None,
        run_scope: str | None = None,
    ) -> list[KnowledgeSource]:
        parameters: list[Any] = []
        query = 'SELECT * FROM knowledge_sources'
        clauses: list[str] = []
        if run_scope is not None:
            clauses.append('(run_scope = ? OR run_scope IS NULL)')
            parameters.append(run_scope)
        if source_types:
            values = [item.value for item in source_types]
            clauses.append(
                'source_type IN (' + ','.join('?' for _ in values) + ')'
            )
            parameters.extend(values)
        if clauses:
            query += ' WHERE ' + ' AND '.join(clauses)
        query += ' ORDER BY ingested_at, source_id'
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [self._knowledge_source_from_row(row) for row in rows]

    @staticmethod
    def _knowledge_source_from_row(row: sqlite3.Row) -> KnowledgeSource:
        return KnowledgeSource(
            source_id=row['source_id'],
            source_type=SourceType(row['source_type']),
            canonical_uri=row['canonical_uri'],
            run_scope=row['run_scope'],
            access_policy=row['access_policy'],
            source_version=row['source_version'],
            digest=row['digest'],
            ingested_at=datetime.fromisoformat(row['ingested_at']),
            index_version=row['index_version'],
            title=row['title'],
            metadata=json.loads(row['metadata']),
            parent_source_id=row['parent_source_id'],
        )

    def delete_knowledge_source(self, source_id: str) -> bool:
        with self.transaction() as connection:
            connection.execute(
                'DELETE FROM knowledge_chunks_fts '
                'WHERE chunk_id IN ('
                '  SELECT chunk_id FROM knowledge_chunks WHERE source_id = ?'
                ')',
                (source_id,),
            )
            cursor = connection.execute(
                'DELETE FROM knowledge_sources WHERE source_id = ?',
                (source_id,),
            )
        return cursor.rowcount == 1

    def delete_knowledge_sources_by_digest(self, digest: str) -> int:
        with self.transaction() as connection:
            connection.execute(
                'DELETE FROM knowledge_chunks_fts '
                'WHERE chunk_id IN ('
                '  SELECT chunk_id FROM knowledge_chunks WHERE source_id IN ('
                '    SELECT source_id FROM knowledge_sources WHERE digest = ?'
                '  )'
                ')',
                (digest,),
            )
            cursor = connection.execute(
                'DELETE FROM knowledge_sources WHERE digest = ?',
                (digest,),
            )
        return cursor.rowcount

    def replace_knowledge_chunks(
        self,
        source_id: str,
        chunks: list[KnowledgeChunk],
    ) -> list[KnowledgeChunk]:
        with self.transaction() as connection:
            connection.execute(
                'DELETE FROM knowledge_chunks_fts '
                'WHERE chunk_id IN ('
                '  SELECT chunk_id FROM knowledge_chunks WHERE source_id = ?'
                ')',
                (source_id,),
            )
            connection.execute(
                'DELETE FROM knowledge_chunks WHERE source_id = ?',
                (source_id,),
            )
            for chunk in chunks:
                connection.execute(
                    '''
                    INSERT INTO knowledge_chunks (
                        chunk_id, source_id, chunk_index, text, digest,
                        token_count, index_version
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ''',
                    (
                        chunk.chunk_id,
                        chunk.source_id,
                        chunk.chunk_index,
                        chunk.text,
                        chunk.digest,
                        chunk.token_count,
                        chunk.index_version,
                    ),
                )
                connection.execute(
                    '''
                    INSERT INTO knowledge_chunks_fts (chunk_id, text)
                    VALUES (?, ?)
                    ''',
                    (chunk.chunk_id, chunk.text),
                )
        return chunks

    def list_knowledge_chunks(
        self,
        source_id: str,
    ) -> list[KnowledgeChunk]:
        with self._connect() as connection:
            rows = connection.execute(
                '''
                SELECT * FROM knowledge_chunks
                WHERE source_id = ? ORDER BY chunk_index
                ''',
                (source_id,),
            ).fetchall()
        return [
            KnowledgeChunk(
                chunk_id=row['chunk_id'],
                source_id=row['source_id'],
                chunk_index=row['chunk_index'],
                text=row['text'],
                digest=row['digest'],
                token_count=row['token_count'],
                index_version=row['index_version'],
            )
            for row in rows
        ]

    def search_knowledge_chunks(
        self,
        query: str,
        *,
        source_ids: list[str] | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        # Agent-context queries concatenate turn kind, objective, and prompt
        # prefixes into long strings; matching only a fixed prefix of terms
        # missed the distinctive words entirely, so every term competes.
        # Stopwords and prompt-boilerplate tokens are filtered first so
        # common words do not dominate the bm25 ranking.
        terms = [term for term in or_query(query, max_terms=24).split()]
        fts_query = ' OR '.join(f'"{term}"' for term in terms) or None
        with self._connect() as connection:
            if fts_query and source_ids:
                placeholders = ', '.join('?' * len(source_ids))
                rows = connection.execute(
                    f'''
                    SELECT c.chunk_id, c.source_id, c.chunk_index, c.text,
                           c.digest, c.token_count, c.index_version,
                           bm25(knowledge_chunks_fts) AS rank
                    FROM knowledge_chunks c
                    JOIN knowledge_chunks_fts
                      ON knowledge_chunks_fts.chunk_id = c.chunk_id
                    WHERE knowledge_chunks_fts MATCH ?
                      AND c.source_id IN ({placeholders})
                    ORDER BY rank
                    LIMIT ?
                    ''',
                    (fts_query, *source_ids, limit * 3),
                ).fetchall()
            elif fts_query:
                rows = connection.execute(
                    '''
                    SELECT c.chunk_id, c.source_id, c.chunk_index, c.text,
                           c.digest, c.token_count, c.index_version,
                           bm25(knowledge_chunks_fts) AS rank
                    FROM knowledge_chunks c
                    JOIN knowledge_chunks_fts
                      ON knowledge_chunks_fts.chunk_id = c.chunk_id
                    WHERE knowledge_chunks_fts MATCH ?
                    ORDER BY rank
                    LIMIT ?
                    ''',
                    (fts_query, limit * 3),
                ).fetchall()
            else:
                rows = []
        return [
            {
                'chunk_id': row['chunk_id'],
                'source_id': row['source_id'],
                'chunk_index': row['chunk_index'],
                'text': row['text'],
                'digest': row['digest'],
                'token_count': row['token_count'],
                'index_version': row['index_version'],
                'rank': row['rank'],
            }
            for row in rows[:limit]
        ]

    def create_corpus(self, record: CorpusRecord) -> CorpusRecord:
        with self.transaction() as connection:
            connection.execute(
                '''
                INSERT INTO rag_corpora (corpus_id, slug, title, created_at, metadata)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(corpus_id) DO UPDATE SET
                    slug = excluded.slug,
                    title = excluded.title,
                    metadata = excluded.metadata
                ''',
                (
                    record.corpus_id,
                    record.slug,
                    record.title,
                    record.created_at.isoformat(),
                    json.dumps(record.metadata, sort_keys=True),
                ),
            )
        return record

    def get_corpus(self, slug: str) -> CorpusRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                'SELECT * FROM rag_corpora WHERE slug = ?',
                (slug,),
            ).fetchone()
        if row is None:
            return None
        return self._corpus_from_row(row)

    def list_corpora(self) -> list[CorpusRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                'SELECT * FROM rag_corpora ORDER BY created_at, corpus_id'
            ).fetchall()
        return [self._corpus_from_row(row) for row in rows]

    @staticmethod
    def _corpus_from_row(row: sqlite3.Row) -> CorpusRecord:
        return CorpusRecord(
            corpus_id=row['corpus_id'],
            slug=row['slug'],
            title=row['title'],
            created_at=datetime.fromisoformat(row['created_at']),
            metadata=json.loads(row['metadata']),
        )

    def add_corpus_source(self, corpus_id: str, source_id: str) -> bool:
        with self.transaction() as connection:
            cursor = connection.execute(
                '''
                INSERT INTO rag_corpus_sources (corpus_id, source_id, added_at)
                VALUES (?, ?, ?)
                ON CONFLICT(corpus_id, source_id) DO NOTHING
                ''',
                (corpus_id, source_id, utc_now().isoformat()),
            )
        return cursor.rowcount == 1

    def list_corpus_sources(self, corpus_id: str) -> list[str]:
        with self._connect() as connection:
            rows = connection.execute(
                '''
                SELECT source_id FROM rag_corpus_sources
                WHERE corpus_id = ? ORDER BY added_at, source_id
                ''',
                (corpus_id,),
            ).fetchall()
        return [row['source_id'] for row in rows]

    def upsert_rag_document(self, record: RagDocumentRecord) -> RagDocumentRecord:
        with self.transaction() as connection:
            connection.execute(
                '''
                INSERT INTO rag_documents (doc_id, source_id, payload)
                VALUES (?, ?, ?)
                ON CONFLICT(doc_id) DO UPDATE SET
                    source_id = excluded.source_id,
                    payload = excluded.payload
                ''',
                (record.doc_id, record.source_id, _dump(record)),
            )
        return record

    def replace_rag_sections(
        self,
        doc_id: str,
        sections: list[RagSectionRecord],
    ) -> int:
        with self.transaction() as connection:
            connection.execute('DELETE FROM rag_sections WHERE doc_id = ?', (doc_id,))
            for section in sections:
                connection.execute(
                    '''
                    INSERT INTO rag_sections (section_id, doc_id, payload)
                    VALUES (?, ?, ?)
                    ''',
                    (section.section_id, section.doc_id, _dump(section)),
                )
        return len(sections)

    def replace_rag_chunks(
        self,
        source_id: str,
        chunks: list[RagChunkRecord],
    ) -> int:
        # Same transactional shape as replace_knowledge_chunks: stale FTS
        # rows are removed and fresh ones inserted inside one transaction so
        # search never observes a half-replaced index.
        with self.transaction() as connection:
            connection.execute(
                'DELETE FROM rag_chunks_fts '
                'WHERE chunk_id IN ('
                '  SELECT chunk_id FROM rag_chunks WHERE source_id = ?'
                ')',
                (source_id,),
            )
            connection.execute(
                'DELETE FROM rag_chunks WHERE source_id = ?',
                (source_id,),
            )
            for chunk in chunks:
                connection.execute(
                    '''
                    INSERT INTO rag_chunks (
                        chunk_id, source_id, kind, chunk_index, text, payload
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ''',
                    (
                        chunk.chunk_id,
                        chunk.source_id,
                        chunk.kind,
                        chunk.chunk_index,
                        chunk.text,
                        _dump(chunk),
                    ),
                )
                connection.execute(
                    '''
                    INSERT INTO rag_chunks_fts (chunk_id, text)
                    VALUES (?, ?)
                    ''',
                    (chunk.chunk_id, chunk.text),
                )
        return len(chunks)

    def search_rag_chunks_fts(
        self,
        query: str,
        *,
        source_ids: list[str] | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        # Identical term handling to search_knowledge_chunks: every
        # whitespace-separated term longer than one character competes via
        # OR, capped at 24 terms.
        terms = [term for term in query.split() if len(term) > 1][:24]
        fts_query = ' OR '.join(f'"{term}"' for term in terms) or None
        with self._connect() as connection:
            if fts_query and source_ids:
                placeholders = ', '.join('?' * len(source_ids))
                rows = connection.execute(
                    f'''
                    SELECT c.payload, bm25(rag_chunks_fts) AS rank
                    FROM rag_chunks c
                    JOIN rag_chunks_fts
                      ON rag_chunks_fts.chunk_id = c.chunk_id
                    WHERE rag_chunks_fts MATCH ?
                      AND c.source_id IN ({placeholders})
                    ORDER BY rank
                    LIMIT ?
                    ''',
                    (fts_query, *source_ids, limit * 3),
                ).fetchall()
            elif fts_query:
                rows = connection.execute(
                    '''
                    SELECT c.payload, bm25(rag_chunks_fts) AS rank
                    FROM rag_chunks c
                    JOIN rag_chunks_fts
                      ON rag_chunks_fts.chunk_id = c.chunk_id
                    WHERE rag_chunks_fts MATCH ?
                    ORDER BY rank
                    LIMIT ?
                    ''',
                    (fts_query, limit * 3),
                ).fetchall()
            else:
                rows = []
        hits: list[dict[str, Any]] = []
        for row in rows[:limit]:
            hit = json.loads(row['payload'])
            hit['rank'] = row['rank']
            hits.append(hit)
        return hits

    def list_rag_chunks(
        self,
        *,
        source_ids: list[str] | None = None,
        kinds: list[str] | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        query = 'SELECT payload FROM rag_chunks'
        clauses: list[str] = []
        parameters: list[Any] = []
        if source_ids:
            clauses.append(
                'source_id IN (' + ','.join('?' for _ in source_ids) + ')'
            )
            parameters.extend(source_ids)
        if kinds:
            clauses.append('kind IN (' + ','.join('?' for _ in kinds) + ')')
            parameters.extend(kinds)
        if clauses:
            query += ' WHERE ' + ' AND '.join(clauses)
        query += ' ORDER BY source_id, chunk_index'
        if limit is not None:
            query += ' LIMIT ?'
            parameters.append(limit)
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [json.loads(row['payload']) for row in rows]

    def upsert_rag_chunk_vectors(self, meta: ChunkVectorMeta, vec_bytes: bytes) -> None:
        with self.transaction() as connection:
            connection.execute(
                '''
                INSERT INTO rag_chunk_vectors (
                    chunk_id, vec, model_id, revision, dims, index_version
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(chunk_id) DO UPDATE SET
                    vec = excluded.vec,
                    model_id = excluded.model_id,
                    revision = excluded.revision,
                    dims = excluded.dims,
                    index_version = excluded.index_version
                ''',
                (
                    meta.chunk_id,
                    sqlite3.Binary(vec_bytes),
                    meta.model_id,
                    meta.revision,
                    meta.dims,
                    meta.index_version,
                ),
            )

    def list_rag_chunk_vectors(
        self,
        model_id: str | None = None,
    ) -> list[tuple[ChunkVectorMeta, bytes]]:
        query = (
            'SELECT chunk_id, vec, model_id, revision, dims, index_version '
            'FROM rag_chunk_vectors'
        )
        parameters: list[Any] = []
        if model_id is not None:
            query += ' WHERE model_id = ?'
            parameters.append(model_id)
        query += ' ORDER BY chunk_id'
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [
            (
                ChunkVectorMeta(
                    chunk_id=row['chunk_id'],
                    model_id=row['model_id'],
                    revision=row['revision'],
                    dims=row['dims'],
                    index_version=row['index_version'],
                ),
                bytes(row['vec']),
            )
            for row in rows
        ]

    def upsert_knowledge_chunk_vectors(
        self, meta: ChunkVectorMeta, vec_bytes: bytes
    ) -> None:
        with self.transaction() as connection:
            connection.execute(
                '''
                INSERT INTO knowledge_chunk_vectors (
                    chunk_id, vec, model_id, revision, dims, index_version
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(chunk_id) DO UPDATE SET
                    vec = excluded.vec,
                    model_id = excluded.model_id,
                    revision = excluded.revision,
                    dims = excluded.dims,
                    index_version = excluded.index_version
                ''',
                (
                    meta.chunk_id,
                    sqlite3.Binary(vec_bytes),
                    meta.model_id,
                    meta.revision,
                    meta.dims,
                    meta.index_version,
                ),
            )

    def list_knowledge_chunk_vectors(
        self, model_id: str | None = None
    ) -> list[tuple[ChunkVectorMeta, bytes]]:
        query = (
            'SELECT chunk_id, vec, model_id, revision, dims, index_version'
            ' FROM knowledge_chunk_vectors'
        )
        parameters: list[Any] = []
        if model_id is not None:
            query += ' WHERE model_id = ?'
            parameters.append(model_id)
        query += ' ORDER BY chunk_id'
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [
            (
                ChunkVectorMeta(
                    chunk_id=row['chunk_id'],
                    model_id=row['model_id'],
                    revision=row['revision'],
                    dims=row['dims'],
                    index_version=row['index_version'],
                ),
                bytes(row['vec']),
            )
            for row in rows
        ]

    def list_all_knowledge_chunks(
        self, *, limit: int | None = None
    ) -> list[dict[str, Any]]:
        query = (
            'SELECT chunk_id, source_id, chunk_index, text, digest,'
            ' token_count, index_version FROM knowledge_chunks'
            ' ORDER BY source_id, chunk_index'
        )
        parameters: list[Any] = []
        if limit is not None:
            query += ' LIMIT ?'
            parameters.append(limit)
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [
            {
                'chunk_id': row['chunk_id'],
                'source_id': row['source_id'],
                'chunk_index': row['chunk_index'],
                'text': row['text'],
                'digest': row['digest'],
                'token_count': row['token_count'],
                'index_version': row['index_version'],
            }
            for row in rows
        ]

    def get_knowledge_chunks(self, chunk_ids: Sequence[str]) -> list[dict[str, Any]]:
        if not chunk_ids:
            return []
        placeholders = ', '.join('?' for _ in chunk_ids)
        query = (
            'SELECT chunk_id, source_id, chunk_index, text, digest,'
            ' token_count, index_version FROM knowledge_chunks'
            f' WHERE chunk_id IN ({placeholders})'
        )
        with self._connect() as connection:
            rows = connection.execute(query, list(chunk_ids)).fetchall()
        by_id = {
            row['chunk_id']: {
                'chunk_id': row['chunk_id'],
                'source_id': row['source_id'],
                'chunk_index': row['chunk_index'],
                'text': row['text'],
                'digest': row['digest'],
                'token_count': row['token_count'],
                'index_version': row['index_version'],
            }
            for row in rows
        }
        return [by_id[cid] for cid in chunk_ids if cid in by_id]

    def save_context_packet(self, packet: ContextPacket) -> ContextPacket:
        with self.transaction() as connection:
            connection.execute(
                '''
                INSERT INTO context_packets (
                    packet_id, run_id, agent, turn_number, turn_kind, query,
                    index_version, ranked_sources, exact_text_supplied,
                    token_budget, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''',
                (
                    packet.packet_id,
                    packet.run_id,
                    packet.agent.value,
                    packet.turn_number,
                    packet.turn_kind.value,
                    packet.query,
                    packet.index_version,
                    json.dumps(packet.ranked_sources),
                    packet.exact_text_supplied,
                    packet.token_budget,
                    packet.created_at.isoformat(),
                ),
            )
        return packet

    def get_context_packet(self, packet_id: str) -> ContextPacket:
        with self._connect() as connection:
            row = connection.execute(
                'SELECT * FROM context_packets WHERE packet_id = ?',
                (packet_id,),
            ).fetchone()
        if row is None:
            raise RecordNotFound(packet_id)
        return ContextPacket(
            packet_id=row['packet_id'],
            run_id=row['run_id'],
            agent=AgentName(row['agent']),
            turn_number=row['turn_number'],
            turn_kind=TurnKind(row['turn_kind']),
            query=row['query'],
            index_version=row['index_version'],
            ranked_sources=json.loads(row['ranked_sources']),
            exact_text_supplied=row['exact_text_supplied'],
            token_budget=row['token_budget'],
            created_at=datetime.fromisoformat(row['created_at']),
        )

    def list_context_packets(self, run_id: str) -> list[ContextPacket]:
        with self._connect() as connection:
            rows = connection.execute(
                '''
                SELECT * FROM context_packets
                WHERE run_id = ? ORDER BY created_at, packet_id
                ''',
                (run_id,),
            ).fetchall()
        return [self._context_packet_from_row(row) for row in rows]

    @staticmethod
    def _context_packet_from_row(row: sqlite3.Row) -> ContextPacket:
        return ContextPacket(
            packet_id=row['packet_id'],
            run_id=row['run_id'],
            agent=AgentName(row['agent']),
            turn_number=row['turn_number'],
            turn_kind=TurnKind(row['turn_kind']),
            query=row['query'],
            index_version=row['index_version'],
            ranked_sources=json.loads(row['ranked_sources']),
            exact_text_supplied=row['exact_text_supplied'],
            token_budget=row['token_budget'],
            created_at=datetime.fromisoformat(row['created_at']),
        )
