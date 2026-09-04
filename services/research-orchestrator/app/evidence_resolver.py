"""Evidence URI resolver for research orchestrator.

This module provides deterministic resolution of claim evidence URIs against
the authoritative store tables. Every URI must resolve to an actual record
before acceptance - fabricated or non-existent URIs fail validation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from .research_store import ResearchStore
from .schemas import ArtifactRecord, EventRecord, JobRecord, KnowledgeChunk, KnowledgeSource


@dataclass
class ResolvedEvidence:
    """Result of URI resolution against store tables."""

    uri: str
    resolved: bool
    resolved_to: Literal[
        'artifact', 'job', 'event', 'knowledge_source', 'knowledge_chunk'
    ] | None = None
    record_id: str | None = None
    record: ArtifactRecord | JobRecord | EventRecord | KnowledgeSource | KnowledgeChunk | None = None
    error: str | None = None


class EvidenceURIResolver:
    """Resolve claim evidence URIs against authoritative store tables.

    The resolver validates that every evidence URI points to an actual
    record in the database. Unknown schemes or missing records result in
    unresolved status.

    Supported URI formats:
    - artifact://<run_id>/artifacts/<path>  -> artifact record
    - job://<job_id>                         -> job record
    - event://<event_id>                     -> event record
    - knowledge://<source_id>                -> knowledge source record
    - knowledge://context:<packet_id>        -> context packet record
    - git://<path>                           -> not supported (placeholder)
    - contract://<path>                      -> not supported (placeholder)
    """

    # Pattern to extract run_id and path from artifact:// URIs
    # Format: artifact://<run_id>/artifacts/<path>
    _ARTIFACT_PATTERN = re.compile(r'^artifact://([^/]+)/artifacts/(.+)$')

    # Pattern for knowledge:// URIs
    # Format: knowledge://<source_id> or knowledge://context:<packet_id>
    _KNOWLEDGE_PATTERN = re.compile(r'^knowledge://([^:]+)(?::([^:]+))?$')

    def __init__(self, store: ResearchStore) -> None:
        self.store = store

    def resolve(self, uri: str) -> ResolvedEvidence:
        """Resolve a single evidence URI against store tables.

        Returns ResolvedEvidence with resolved=True if the URI points to
        an existing record, or resolved=False with an error message.
        """
        if not uri.startswith(
            ('artifact://', 'job://', 'event://', 'knowledge://')
        ):
            return ResolvedEvidence(
                uri=uri,
                resolved=False,
                error=f'unsupported evidence URI scheme',
            )

        if uri.startswith('artifact://'):
            return self._resolve_artifact(uri)

        if uri.startswith('job://'):
            return self._resolve_job(uri)

        if uri.startswith('event://'):
            return self._resolve_event(uri)

        if uri.startswith('knowledge://'):
            return self._resolve_knowledge(uri)

        return ResolvedEvidence(uri=uri, resolved=False, error='unknown scheme')

    def _resolve_artifact(self, uri: str) -> ResolvedEvidence:
        """Resolve artifact://<run_id>/artifacts/<path> to artifact record."""
        match = self._ARTIFACT_PATTERN.match(uri)
        if not match:
            return ResolvedEvidence(
                uri=uri,
                resolved=False,
                error='artifact:// URI must follow format: artifact://<run_id>/artifacts/<path>',
            )

        run_id, path = match.groups()
        # Find the artifact record by run_id and uri pattern
        artifacts = self.store.list_artifacts(run_id)
        for artifact in artifacts:
            if f'artifacts/{artifact.uri}' == path or artifact.uri == path:
                return ResolvedEvidence(
                    uri=uri,
                    resolved=True,
                    resolved_to='artifact',
                    record_id=artifact.artifact_id,
                    record=artifact,
                )

        return ResolvedEvidence(
            uri=uri,
            resolved=False,
            error=f'artifact not found for path: {path}',
        )

    def _resolve_job(self, uri: str) -> ResolvedEvidence:
        """Resolve job://<job_id> to job record."""
        if not uri.startswith('job://'):
            return ResolvedEvidence(
                uri=uri, resolved=False, error='not a job:// URI'
            )

        job_id = uri[6:]  # Remove 'job://' prefix
        try:
            job = self.store.get_job(job_id)
            return ResolvedEvidence(
                uri=uri,
                resolved=True,
                resolved_to='job',
                record_id=job.job_id,
                record=job,
            )
        except Exception:
            return ResolvedEvidence(
                uri=uri,
                resolved=False,
                error=f'job not found: {job_id}',
            )

    def _resolve_event(self, uri: str) -> ResolvedEvidence:
        """Resolve event://<event_id> to event record."""
        if not uri.startswith('event://'):
            return ResolvedEvidence(
                uri=uri, resolved=False, error='not an event:// URI'
            )

        event_id = uri[8:]  # Remove 'event://' prefix
        try:
            # Event records don't have a get_event method, so search by id
            # We need to use list_events and filter
            # But we don't know the run_id - this is a limitation
            # For now, mark as unresolved since we can't look it up directly
            return ResolvedEvidence(
                uri=uri,
                resolved=False,
                error='event:// URI resolution requires run_id (not yet supported)',
            )
        except Exception as e:
            return ResolvedEvidence(
                uri=uri,
                resolved=False,
                error=f'event not found: {event_id}, error: {e}',
            )

    def _resolve_knowledge(self, uri: str) -> ResolvedEvidence:
        """Resolve knowledge://<source_id> or knowledge://context:<packet_id>.

        - knowledge://<source_id> -> KnowledgeSource record
        - knowledge://context:<packet_id> -> ContextPacket record
        """
        match = self._KNOWLEDGE_PATTERN.match(uri)
        if not match:
            return ResolvedEvidence(
                uri=uri,
                resolved=False,
                error='knowledge:// URI must follow format: knowledge://<source_id> or knowledge://context:<packet_id>',
            )

        first_part = match.group(1)
        second_part = match.group(2)

        if second_part is None:
            # knowledge://<source_id>
            source_id = first_part
            try:
                source = self.store.get_knowledge_source(source_id)
                return ResolvedEvidence(
                    uri=uri,
                    resolved=True,
                    resolved_to='knowledge_source',
                    record_id=source.source_id,
                    record=source,
                )
            except Exception:
                return ResolvedEvidence(
                    uri=uri,
                    resolved=False,
                    error=f'knowledge source not found: {source_id}',
                )
        else:
            # knowledge://context:<packet_id>
            if first_part != 'context':
                return ResolvedEvidence(
                    uri=uri,
                    resolved=False,
                    error=f'knowledge:// context: prefix required, got: {first_part}',
                )
            packet_id = second_part
            try:
                packet = self.store.get_context_packet(packet_id)
                return ResolvedEvidence(
                    uri=uri,
                    resolved=True,
                    resolved_to='knowledge_chunk',
                    record_id=packet.packet_id,
                    record=packet,
                )
            except Exception:
                return ResolvedEvidence(
                    uri=uri,
                    resolved=False,
                    error=f'context packet not found: {packet_id}',
                )

    def resolve_all(self, uris: list[str]) -> list[ResolvedEvidence]:
        """Resolve multiple URIs, returning list of ResolvedEvidence."""
        return [self.resolve(uri) for uri in uris]
