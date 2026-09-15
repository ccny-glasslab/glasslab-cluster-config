"""Read-only, agent-directed knowledge retrieval tool for Honeydew.

``retrieve_evidence`` lets a Honeydew turn iterate retrieve -> reason ->
retrieve instead of relying only on the one-shot per-turn retrieval. It adds
a *surface*, not a second retrieval implementation: every call is a projection
over :meth:`KnowledgeManager.retrieve_evidence`, which delegates to the same
scoped ranking/diversify/token-budget pipeline.

The tool is exposed to Honeydew only (never Beaker), it is read-only by
construction (no writes, no filesystem, no shell), its output passes the same
secret scan as ingested content, and each call is recorded as a durable
``agent.tool_call`` / ``agent.tool_result`` event.
"""

from __future__ import annotations

from dataclasses import dataclass
from threading import Lock
from uuid import uuid4

from .config import Settings
from .knowledge_manager import KnowledgeManager, RetrievedEvidence
from .research_store import ResearchStore
from .schemas import AgentName, TurnKind

TOOL_NAME = 'retrieve_evidence'
DEFAULT_K = 5
MIN_K = 1
MAX_K = 20

__all__ = [
    'DEFAULT_K',
    'MAX_K',
    'MIN_K',
    'TOOL_NAME',
    'BoundRetrieveEvidenceTool',
    'KnowledgeToolDenied',
    'KnowledgeToolError',
    'KnowledgeToolRegistry',
    'KnowledgeToolResult',
]


class KnowledgeToolError(RuntimeError):
    pass


class KnowledgeToolDenied(KnowledgeToolError):
    """A caller tried to use the tool where its agent surface does not allow it."""


@dataclass(frozen=True)
class KnowledgeToolResult:
    query: str
    k: int
    packet_id: str
    context: str | None
    uris: tuple[str, ...]
    verified: tuple[bool, ...]
    chunk_count: int
    bytes_returned: int
    truncated: bool


class BoundRetrieveEvidenceTool:
    """One run/agent retrieval surface, executable by the orchestrator.

    The engine creates one binding per (run, Honeydew) and reuses it across
    turns, refreshing ``turn_number``/``turn_kind`` before each turn. The
    capability ``token`` is what the OpenCode tool file sends back to the
    orchestrator callback endpoint; it is scoped to this run and agent and
    grants nothing but read-only retrieval.
    """

    def __init__(
        self,
        *,
        knowledge: KnowledgeManager,
        store: ResearchStore,
        settings: Settings,
        run_id: str,
        agent: AgentName,
        token: str,
        turn_number: int,
        turn_kind: TurnKind,
    ) -> None:
        self.knowledge = knowledge
        self.store = store
        self.settings = settings
        self.run_id = run_id
        self.agent = agent
        self.token = token
        self.turn_number = turn_number
        self.turn_kind = turn_kind

    def refresh_turn(self, *, turn_number: int, turn_kind: TurnKind) -> None:
        self.turn_number = turn_number
        self.turn_kind = turn_kind

    def __call__(self, query: str, k: int = DEFAULT_K) -> KnowledgeToolResult:
        if self.agent is not AgentName.HONEYDEW:
            raise KnowledgeToolDenied(
                f'{TOOL_NAME} is not available to {self.agent.value}'
            )
        normalized_query = ' '.join(str(query).split())
        if not normalized_query:
            raise KnowledgeToolError('query must not be empty')
        max_chars = self.settings.knowledge_tool_max_query_chars
        if len(normalized_query) > max_chars:
            normalized_query = normalized_query[:max_chars]
        if isinstance(k, bool) or not isinstance(k, (int, float)):
            raise KnowledgeToolError('k must be an integer')
        resolved_k = max(MIN_K, min(int(k), self.settings.knowledge_tool_max_k))

        self.store.append_event(
            run_id=self.run_id,
            source=self.agent.value,
            event_type='agent.tool_call',
            payload={
                'tool': TOOL_NAME,
                'agent': self.agent.value,
                'turn_number': self.turn_number,
                'turn_kind': self.turn_kind.value,
                'query': normalized_query,
                'k': resolved_k,
            },
        )
        evidence = self.knowledge.retrieve_evidence(
            run_id=self.run_id,
            agent=self.agent.value,
            turn_number=self.turn_number,
            turn_kind=self.turn_kind.value,
            query=normalized_query,
            max_results=resolved_k,
            token_budget=self.settings.knowledge_token_budget,
            run_scope=self.run_id,
            pinned_source_ids=None,
        )
        result = self._apply_budget(evidence, normalized_query, resolved_k)
        self.store.append_event(
            run_id=self.run_id,
            source=self.agent.value,
            event_type='agent.tool_result',
            payload={
                'tool': TOOL_NAME,
                'agent': self.agent.value,
                'turn_number': self.turn_number,
                'turn_kind': self.turn_kind.value,
                'query': normalized_query,
                'k': resolved_k,
                'packet_id': result.packet_id,
                'returned_uris': list(result.uris),
                'verified': list(result.verified),
                'chunk_count': result.chunk_count,
                'bytes_returned': result.bytes_returned,
                'truncated': result.truncated,
            },
        )
        return result

    def _used_bytes(self) -> int:
        total = 0
        for event in self.store.list_events(self.run_id):
            if event.event_type != 'agent.tool_result':
                continue
            value = event.payload.get('bytes_returned')
            if isinstance(value, int) and not isinstance(value, bool):
                total += value
        return total

    def _apply_budget(
        self,
        evidence: RetrievedEvidence,
        query: str,
        k: int,
    ) -> KnowledgeToolResult:
        # Iterative retrieval must not escape the per-run evidence snapshot
        # budget: cumulative rendered tool output may not exceed
        # evidence_snapshot_max_bytes. Entries are admitted whole, lowest rank
        # first to drop, exactly like the per-turn token budget.
        remaining = max(
            0,
            self.settings.evidence_snapshot_max_bytes - self._used_bytes(),
        )
        kept = list(evidence.entries)
        context: str | None = None
        truncated = False
        if kept:
            while kept:
                candidate = self.knowledge.render_entries(kept)
                if (
                    candidate is not None
                    and len(candidate.encode('utf-8')) <= remaining
                ):
                    context = candidate
                    break
                kept = kept[:-1]
            if context is None:
                truncated = True
        chunks = list(evidence.chunks)[: len(kept)]
        bytes_returned = len(context.encode('utf-8')) if context else 0
        return KnowledgeToolResult(
            query=query,
            k=k,
            packet_id=evidence.packet.packet_id,
            context=context,
            uris=tuple(str(chunk['uri']) for chunk in chunks),
            verified=tuple(bool(chunk['verified']) for chunk in chunks),
            chunk_count=len(chunks),
            bytes_returned=bytes_returned,
            truncated=truncated,
        )


class KnowledgeToolRegistry:
    """Maps per-run capability tokens to their bound retrieval tool."""

    def __init__(
        self,
        *,
        knowledge: KnowledgeManager,
        store: ResearchStore,
        settings: Settings,
    ) -> None:
        self.knowledge = knowledge
        self.store = store
        self.settings = settings
        self._by_token: dict[str, BoundRetrieveEvidenceTool] = {}
        self._by_key: dict[tuple[str, AgentName], BoundRetrieveEvidenceTool] = {}
        self._lock = Lock()

    def bind(
        self,
        *,
        run_id: str,
        agent: AgentName,
        turn_number: int,
        turn_kind: TurnKind,
    ) -> BoundRetrieveEvidenceTool:
        with self._lock:
            key = (run_id, agent)
            bound = self._by_key.get(key)
            if bound is None:
                bound = BoundRetrieveEvidenceTool(
                    knowledge=self.knowledge,
                    store=self.store,
                    settings=self.settings,
                    run_id=run_id,
                    agent=agent,
                    token=uuid4().hex,
                    turn_number=turn_number,
                    turn_kind=turn_kind,
                )
                self._by_key[key] = bound
                self._by_token[bound.token] = bound
            else:
                bound.refresh_turn(
                    turn_number=turn_number, turn_kind=turn_kind
                )
            return bound

    def resolve(self, token: str) -> BoundRetrieveEvidenceTool:
        with self._lock:
            bound = self._by_token.get(token)
        if bound is None:
            raise KnowledgeToolDenied('unknown or expired tool capability token')
        return bound

    def execute(self, *, token: str, query: str, k: int) -> KnowledgeToolResult:
        return self.resolve(token)(query, k)

    def revoke_run(self, run_id: str) -> None:
        with self._lock:
            keys = [key for key in self._by_key if key[0] == run_id]
            for key in keys:
                bound = self._by_key.pop(key)
                self._by_token.pop(bound.token, None)

    def revoke_agent(self, run_id: str, agent: AgentName) -> None:
        with self._lock:
            bound = self._by_key.pop((run_id, agent), None)
            if bound is not None:
                self._by_token.pop(bound.token, None)
