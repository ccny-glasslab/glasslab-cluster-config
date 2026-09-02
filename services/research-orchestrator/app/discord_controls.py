"""Outbound Discord command and button control surface.

Slash commands and component buttons are only ever a UI; every handler funnels
into engine calls that persist to the authoritative store, and authorization is
re-checked server-side at the point of execution (never trusted from the
rendered button). Heavy engine work is pushed to threads so the discord.py
event loop is never blocked.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import io
import logging
import re
from typing import TYPE_CHECKING
from uuid import uuid4

logger = logging.getLogger(__name__)

import discord
from discord import app_commands

from .artifact_delivery import ArtifactBundle, build_run_artifact_bundle
from .schemas import (
    ActionRecord,
    ApprovalStatus,
    IngestedDatasetRecord,
    JobRecord,
    JobStatus,
    PolicyClassification,
    ResearchAnswer,
    RunCreateRequest,
    RunRecord,
    RunState,
    TERMINAL_STATES,
)
from .turn_inspection import (
    DEFAULT_DISCORD_TURN_LIMIT,
    MAXIMUM_DISCORD_TURN_LIMIT,
    format_turn_history,
    summarize_turns,
)

if TYPE_CHECKING:
    from .engine import ResearchOrchestrator


CONTROL_PREFIX = 'glasslab'
PACKET_PREFIX = 'packet'

RUN_LIST_LIMIT = 10
DISCORD_MESSAGE_LIMIT = 2000

# Action types that represent a pending human approval in the workflow. The
# mapped value is the instruction shown to the operator as the next action.
_PENDING_APPROVAL_ACTIONS = {
    'approve_protocol': 'approve the proposed protocol',
    'propose_evaluation_contract': 'approve evaluation-contract promotion',
    'submit_experiment_matrix': 'approve cluster execution',
    'accept_final_report': 'accept the final report',
}

_PHASE_LABELS = {
    RunState.CREATED: 'created',
    RunState.PREPARING: 'preparing',
    RunState.HONEYDEW_DRAFTING_PROTOCOL: 'drafting protocol',
    RunState.AWAITING_PROTOCOL_APPROVAL: 'awaiting protocol approval',
    RunState.BEAKER_DRAFTING_CONTRACT: 'drafting evaluation contract',
    RunState.HONEYDEW_REVIEWING_CONTRACT: 'reviewing contract candidate',
    RunState.AWAITING_CONTRACT_PROMOTION: 'awaiting contract promotion',
    RunState.BEAKER_PLANNING: 'planning implementation',
    RunState.BEAKER_IMPLEMENTING: 'implementing workload',
    RunState.BEAKER_FINALIZING: 'finalizing workload',
    RunState.HONEYDEW_REVIEWING: 'honeydew reviewing implementation',
    RunState.BEAKER_REVISING: 'revising implementation',
    RunState.AWAITING_EXECUTION_APPROVAL: 'awaiting execution approval',
    RunState.JOB_QUEUED: 'jobs queued',
    RunState.JOB_RUNNING: 'jobs running',
    RunState.BEAKER_ANALYZING: 'analyzing results',
    RunState.HONEYDEW_VERIFYING: 'verifying results',
    RunState.HONEYDEW_WRITING_REPORT: 'writing report',
    RunState.AWAITING_FINAL_ACCEPTANCE: 'awaiting final acceptance',
    RunState.COMPLETE: 'complete',
    RunState.PAUSED: 'paused',
    RunState.FAILED: 'failed',
    RunState.CANCELLED: 'cancelled',
    RunState.TIMED_OUT: 'timed out',
}

_NEXT_ACTION_BY_STATE = {
    RunState.CREATED: 'orchestrator is preparing the run',
    RunState.PREPARING: 'orchestrator is preparing workspaces',
    RunState.HONEYDEW_DRAFTING_PROTOCOL: 'Honeydew is drafting the protocol',
    RunState.BEAKER_DRAFTING_CONTRACT: 'Beaker is drafting the contract candidate',
    RunState.HONEYDEW_REVIEWING_CONTRACT: 'Honeydew is reviewing the contract candidate',
    RunState.BEAKER_PLANNING: 'Beaker is planning the implementation',
    RunState.BEAKER_IMPLEMENTING: 'Beaker is implementing the workload',
    RunState.BEAKER_FINALIZING: 'Beaker is finalizing the workload',
    RunState.HONEYDEW_REVIEWING: 'Honeydew is reviewing the implementation',
    RunState.BEAKER_REVISING: 'Beaker is revising the implementation',
    RunState.JOB_QUEUED: 'waiting for cluster jobs to run',
    RunState.JOB_RUNNING: 'cluster jobs are running',
    RunState.BEAKER_ANALYZING: 'Beaker is analyzing results',
    RunState.HONEYDEW_VERIFYING: 'Honeydew is verifying results',
    RunState.HONEYDEW_WRITING_REPORT: 'Honeydew is writing the report',
}

# Statuses whose counts are always rendered in a fixed order. Every JobStatus
# is included so queued/running/succeeded/failed/cancelled/submitting/unknown
# jobs are never silently omitted from the projection.
_RENDERED_JOB_STATUSES = tuple(JobStatus)


@dataclass(frozen=True)
class RunStatusView:
    """Durable-derived status snapshot for a single run."""

    run_id: str
    state: str
    phase: str
    pending_approval: str | None
    job_counts: dict[str, int]
    next_action: str


def pending_human_approval(actions: list[ActionRecord]) -> ActionRecord | None:
    # Only actions that still require a human decision are "next action" worthy.
    # A combined HONEYDEW_AND_HUMAN_APPROVAL gate is not human-ready until
    # Honeydew has signed off (honeydew_approved); before that, the next action
    # is derived from the current Honeydew-review state instead.
    for action in actions:
        if action.approval_status != ApprovalStatus.PENDING:
            continue
        if action.policy_classification == PolicyClassification.HUMAN_APPROVAL:
            return action
        if (
            action.policy_classification
            == PolicyClassification.HONEYDEW_AND_HUMAN_APPROVAL
            and action.honeydew_approved
        ):
            return action
    return None


def job_status_counts(jobs: list[JobRecord]) -> dict[str, int]:
    counts = {status.value: 0 for status in _RENDERED_JOB_STATUSES}
    for job in jobs:
        if job.status.value in counts:
            counts[job.status.value] += 1
    return counts


def next_action_for(run: RunRecord, actions: list[ActionRecord]) -> str:
    pending = pending_human_approval(actions)
    if pending is not None:
        return _PENDING_APPROVAL_ACTIONS.get(
            pending.type,
            f'resolve pending {pending.type}',
        )
    if run.state in TERMINAL_STATES:
        return f'no action; run is {run.state.value.lower()}'
    if run.state == RunState.PAUSED:
        return 'resume the paused run'
    return _NEXT_ACTION_BY_STATE.get(run.state, f'progress through {run.state.value}')


def build_run_status_view(
    run: RunRecord,
    actions: list[ActionRecord],
    jobs: list[JobRecord],
) -> RunStatusView:
    pending = pending_human_approval(actions)
    return RunStatusView(
        run_id=run.run_id,
        state=run.state.value,
        phase=_PHASE_LABELS.get(run.state, run.state.value),
        pending_approval=pending.type if pending is not None else None,
        job_counts=job_status_counts(jobs),
        next_action=next_action_for(run, actions),
    )


def render_run_status(view: RunStatusView) -> str:
    jobs = ', '.join(
        f'{status} {view.job_counts[status]}'
        for status in (s.value for s in _RENDERED_JOB_STATUSES)
    )
    lines = [
        f'Research run `{view.run_id}`',
        f'State: {view.state} ({view.phase})',
        f'Pending approval: {view.pending_approval or "none"}',
        f'Jobs: {jobs}',
        f'Next action: {view.next_action}',
    ]
    return '\n'.join(lines)


def select_runs_for_list(
    runs: list[RunRecord],
    *,
    limit: int = RUN_LIST_LIMIT,
) -> list[RunRecord]:
    # Active runs first, then the most recently updated terminal runs, capped at
    # `limit`. Both halves are ordered by updated_at (newest first); run_id is a
    # stable tie-breaker so equal timestamps still produce a deterministic order
    # regardless of the store's creation order.
    active = [run for run in runs if run.state not in TERMINAL_STATES]
    terminal = [run for run in runs if run.state in TERMINAL_STATES]
    active.sort(key=lambda run: (run.updated_at, run.run_id), reverse=True)
    terminal.sort(key=lambda run: (run.updated_at, run.run_id), reverse=True)
    return (active + terminal)[:limit]


def render_run_list(runs: list[RunRecord]) -> str:
    if not runs:
        return 'No Glasslab research runs.'
    lines = ['Glasslab research runs:']
    for run in runs:
        state = run.state.value
        if run.state in TERMINAL_STATES:
            state = f'{state} (terminal)'
        lines.append(f'- `{run.run_id}` — {state}')
    return '\n'.join(lines)


def bound_discord_message(text: str, limit: int = DISCORD_MESSAGE_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3] + '...'


@dataclass(frozen=True)
class DiscordControlActor:
    user_id: str
    display_name: str
    guild_id: str | None
    role_ids: frozenset[str]

    @property
    def reviewer(self) -> str:
        # Human actor identity written into the authoritative event log; the
        # user id plus display name makes approvals attributable after the fact.
        return f'discord:{self.user_id}:{self.display_name}'


class DiscordControlPolicy:
    def __init__(
        self,
        *,
        guild_id: str,
        admin_role_id: str | None,
        admin_user_ids: list[str],
    ) -> None:
        self.guild_id = guild_id
        self.admin_role_id = admin_role_id
        self.admin_user_ids = frozenset(admin_user_ids)

    def is_authorized(self, actor: DiscordControlActor) -> bool:
        # Controls are guild-bound: an interaction from any other guild (or
        # with a spoofed role set from DMs) is rejected. Explicit user ids
        # override the role check so operator accounts survive role churn.
        if actor.guild_id != self.guild_id:
            return False
        if actor.user_id in self.admin_user_ids:
            return True
        return bool(
            self.admin_role_id
            and self.admin_role_id in actor.role_ids
        )


def execute_discord_action(
    engine: ResearchOrchestrator,
    *,
    operation: str,
    action_id: str,
    actor: DiscordControlActor,
    reason: str | None = None,
) -> None:
    # Whitelist the operation so a malformed custom_id can never reach the
    # engine as anything other than approve or reject.
    if operation == 'approve':
        engine.approve_action(
            action_id,
            reviewer=actor.reviewer,
            reason=reason or 'Approved through Discord controls.',
        )
    elif operation == 'reject':
        engine.reject_action(
            action_id,
            reviewer=actor.reviewer,
            reason=reason or 'Rejected through Discord controls.',
        )
    else:
        raise ValueError(f'unsupported Discord operation: {operation}')


def execute_discord_run_creation(
    engine: ResearchOrchestrator,
    *,
    objective: str,
) -> RunRecord:
    return engine.create_run(RunCreateRequest(objective=objective))


def execute_discord_run_cancellation(
    engine: ResearchOrchestrator,
    *,
    run_id: str,
    actor: DiscordControlActor,
    reason: str | None = None,
) -> RunRecord:
    return engine.cancel_run(
        run_id,
        requested_by=actor.reviewer,
        reason=reason or 'Cancelled through Discord controls.',
    )


def execute_discord_run_control(
    engine: ResearchOrchestrator,
    *,
    operation: str,
    run_id: str,
    actor: DiscordControlActor,
    reason: str | None = None,
) -> RunRecord:
    if operation == 'pause':
        return engine.pause_run(
            run_id,
            requested_by=actor.reviewer,
            reason=reason or 'Paused through Discord controls.',
        )
    if operation == 'resume':
        return engine.resume_run(
            run_id,
            requested_by=actor.reviewer,
            reason=reason or 'Resumed through Discord controls.',
        )
    raise ValueError(f'unsupported Discord run operation: {operation}')


def execute_discord_dataset_ingestion(
    engine: ResearchOrchestrator,
    *,
    filename: str,
    content: bytes,
    name: str,
    role: str,
    contains_labels: bool,
    actor: DiscordControlActor,
    media_type: str | None = None,
) -> IngestedDatasetRecord:
    return engine.datasets.ingest_bytes(
        content,
        filename=filename,
        name=name,
        role=role,
        contains_labels=contains_labels,
        media_type=media_type,
        uploaded_by=actor.reviewer,
    )


def execute_discord_task_creation(
    engine: ResearchOrchestrator,
    *,
    filename: str,
    content: bytes,
    objective: str | None,
) -> RunRecord:
    task = engine.import_task_bundle(
        filename=filename,
        content=content,
    )
    preflight = engine.task_preflight(task)
    if not preflight.ready:
        # Fail closed: a task that does not compile, have its assets, or pass
        # checksum verification must never be turned into a run. The raised
        # message carries the actionable feedback (what the spec is missing)
        # so the operator can fix and resubmit instead of hitting a dead end.
        raise ValueError(
            preflight.feedback
            or 'task compiled but is not ready: '
            + '; '.join(preflight.blocking_issues)
        )
    return engine.create_run(
        RunCreateRequest(
            objective=objective
            or f'Complete and evaluate the imported {task.display_name} benchmark.',
            task_id=task.task_id,
            task_bundle_digest=task.digest,
        )
    )


def execute_discord_artifact_export(
    engine: ResearchOrchestrator,
    *,
    run_id: str,
    maximum_bytes: int,
    include_source: bool,
) -> ArtifactBundle:
    engine.store.get_run(run_id)
    return build_run_artifact_bundle(
        run_id=run_id,
        artifacts=engine.store.list_artifacts(run_id),
        jobs=engine.store.list_jobs(run_id),
        shared_mount_root=engine.settings.shared_mount_root,
        maximum_bytes=maximum_bytes,
        include_source=include_source,
    )


def execute_discord_turn_history(
    engine: ResearchOrchestrator,
    *,
    run_id: str,
    limit: int,
) -> str:
    # Same redaction and bounding as GET /runs/{run_id}/turns (see
    # turn_inspection.py) so the Discord view can never show more, or less
    # safely, than the HTTP API does.
    run = engine.store.get_run(run_id)
    turns = engine.store.list_turns(run_id)
    summaries = summarize_turns(turns, limit=limit)
    return format_turn_history(run, summaries, total_turns=len(turns))


def execute_discord_research_question(
    engine: ResearchOrchestrator,
    *,
    question: str,
    conversation_id: str,
) -> ResearchAnswer:
    return engine.answer_research_question(
        question=question,
        conversation_id=conversation_id,
    )


MAX_DISCORD_PACKET_CHUNK_CHARS = 1800


def build_packet_button_view(answer: ResearchAnswer) -> discord.ui.View:
    """One button per unique packet: click -> that citation's source text.

    The custom id carries the citation index + a normalized excerpt prefix so
    the renderer can pick the exact ranked-source block the citation quoted
    (excerpt match first, then the citation's rank as fallback). Discord
    rejects duplicated custom ids, so buttons are deduplicated by packet id.
    """
    view = discord.ui.View(timeout=None)
    seen: set[str] = set()
    for index, citation in enumerate(answer.citations, start=1):
        packet_id = citation.knowledge_uri.rsplit('/', 1)[-1]
        if packet_id in seen or index > 5:
            continue
        seen.add(packet_id)
        excerpt_prefix = re.sub(
            r'[^A-Za-z0-9]', '', ' '.join(citation.excerpt.split())
        )[:36]
        button = discord.ui.Button(
            label=f'Source [{index}]',
            style=discord.ButtonStyle.secondary,
            custom_id=(
                f'{PACKET_PREFIX}:{packet_id}:{index}:{excerpt_prefix}'
            ),
        )
        view.add_item(button)
    return view


def format_packet_for_discord(
    engine: ResearchOrchestrator,
    packet_id: str,
    excerpt_prefix: str = '',
    source_index: int | None = None,
) -> list[str]:
    """Render a knowledge packet's source text as bounded Discord messages.

    With an excerpt prefix (from a citation button), only the ranked-source
    block whose text contains the normalized excerpt is shown — the exact
    chunk the citation quoted. If the excerpt does not match, the citation's
    rank in the packet is used; otherwise the whole packet is shown.
    """
    packet = engine.knowledge.get_context_packet(packet_id)
    exact = ' '.join((packet.exact_text_supplied or '').split())
    chunks: list[str] = []
    if not exact:
        chunks.append(
            f'Packet `{packet_id}` has no attached text (0 ranked sources).'
        )
        return chunks
    header = f'**Packet `{packet_id}`**'
    blocks = re.findall(
        r'<knowledge-context[^>]*>(.*?)</knowledge-context>',
        packet.exact_text_supplied or '',
        flags=re.S,
    )
    if excerpt_prefix:
        excerpt_prefix = re.sub(r'[^A-Za-z0-9]', '', excerpt_prefix)
        match = next(
            (
                index for index, block in enumerate(blocks)
                if excerpt_prefix
                in re.sub(r'[^A-Za-z0-9]', '', ' '.join(block.split()))
            ),
            None,
        )
    else:
        match = None
    if match is None and source_index is not None and blocks:
        if 1 <= source_index <= len(blocks):
            match = source_index - 1
    if match is not None:
        header += f' — cited source {match + 1}'
        exact = ' '.join(blocks[match].split())
    else:
        header += ' — full packet (excerpt not matched)'
    sources = ' · '.join(
        str(s.get('source_id', '?'))[:40] for s in packet.ranked_sources
    ) or 'no ranked sources'
    header += f' — sources: {sources}'
    chunks.append(header)
    body = exact
    while body:
        chunks.append(body[:MAX_DISCORD_PACKET_CHUNK_CHARS])
        body = body[MAX_DISCORD_PACKET_CHUNK_CHARS:]
    return chunks


MAX_DISCORD_RESEARCH_ANSWER_CHARS = 2000


def strip_knowledge_wrappers(text: str) -> str:
    """Remove <knowledge-context ...> wrapper tags the model echoes back.

    The retrieved material is wrapped in <knowledge-context> blocks before
    injection; the model sometimes quotes the wrapper markup verbatim. The
    inner source text is kept, only the tags (plain and HTML-escaped) are
    dropped.
    """
    cleaned = re.sub(r'<\s*/?\s*knowledge-context[^>]*>', '', text)
    cleaned = re.sub(r'&lt;\s*/?\s*knowledge-context[^>]*&gt;', '', cleaned)
    return cleaned


def format_research_answer(answer: ResearchAnswer) -> list[str]:
    """Render a research answer as bounded Discord messages (no truncation).

    The answer text is chunked to Discord's message limit (knowledge-context
    wrappers stripped), then a citations block lists each source with its
    packet id.
    """
    if answer.unanswerable:
        return [
            'The knowledge corpus does not contain material to answer this '
            'question. Rephrase it against the corpus domain (ML methods, '
            'uncertainty quantification, metric learning, and similar).'
        ]
    messages: list[str] = []
    answer_text = strip_knowledge_wrappers(
        ' '.join(answer.answer.strip().split())
    )
    if not answer_text:
        messages.append(
            'The answer contained only formatting markup and no text.'
        )
    else:
        body = answer_text
        while body:
            messages.append(body[:1800])
            body = body[1800:]
    if answer.citations:
        messages.append(
            '**Sources (' + str(len(answer.citations)) + ')** — '
            'click a Source button below to see its text'
        )
        for index, citation in enumerate(answer.citations, start=1):
            excerpt = ' '.join(citation.excerpt.split())
            if len(excerpt) > 100:
                excerpt = excerpt[:97].rstrip() + '...'
            packet_id = citation.knowledge_uri.rsplit('/', 1)[-1]
            messages.append(
                f'[{index}] {citation.source} — "{excerpt}" — '
                f'`{packet_id}`'
            )
    return messages


# Compatibility name for callers that predate generic task compilation.
execute_discord_benchmark_creation = execute_discord_task_creation


class DiscordControlGateway:
    """Outbound Gateway listener for bounded approval-button interactions."""

    def __init__(
        self,
        *,
        engine: ResearchOrchestrator,
        bot_token: str,
        guild_id: str,
        channel_id: str,
        admin_role_id: str | None,
        admin_user_ids: list[str],
        maximum_dataset_upload_bytes: int,
        maximum_artifact_bundle_bytes: int = 24 * 1024 * 1024,
    ) -> None:
        self.engine = engine
        self.bot_token = bot_token
        self.channel_id = channel_id
        self.maximum_dataset_upload_bytes = maximum_dataset_upload_bytes
        self.maximum_artifact_bundle_bytes = maximum_artifact_bundle_bytes
        self.policy = DiscordControlPolicy(
            guild_id=guild_id,
            admin_role_id=admin_role_id,
            admin_user_ids=admin_user_ids,
        )
        intents = discord.Intents.none()
        intents.guilds = True
        self.client = discord.Client(intents=intents)
        self.guild = discord.Object(id=int(guild_id))
        self.tree = app_commands.CommandTree(self.client)
        # Slash commands are guild-scoped: syncing to the explicit guild object
        # avoids global sync rate limits and rollout delays.
        self._commands_synced = False
        self._register_commands()
        self.client.on_ready = self._on_ready
        self.client.on_interaction = self._on_interaction
        self._tasks: set[asyncio.Task[None]] = set()

    def _register_commands(self) -> None:
        @self.tree.command(
            name='task-start',
            description=(
                'Start an investigation: attach a task ZIP (problem.md + '
                'rubric) or give an objective.'
            ),
            guild=self.guild,
        )
        @app_commands.describe(
            archive='Optional ZIP containing problem.md and an evaluator rubric.',
            objective='Research objective (required when no archive is attached).',
        )
        async def task_start(
            interaction: discord.Interaction,
            archive: discord.Attachment | None = None,
            objective: app_commands.Range[str, 10, 2000] | None = None,
        ) -> None:
            await self._on_task_start(
                interaction,
                archive=archive,
                objective=str(objective) if objective else None,
            )

        @self.tree.command(
            name='research-cancel',
            description='Cancel a Glasslab research run and its active jobs.',
            guild=self.guild,
        )
        @app_commands.describe(
            run_id='Optional in a run thread; required from the main channel.',
            reason='Optional reason recorded in the authoritative event log.',
        )
        async def research_cancel(
            interaction: discord.Interaction,
            run_id: str | None = None,
            reason: app_commands.Range[str, 3, 500] | None = None,
        ) -> None:
            await self._on_research_cancel(
                interaction,
                run_id=run_id,
                reason=str(reason) if reason else None,
            )

        for operation in ('pause', 'resume'):
            self._register_run_control_command(operation)

        @self.tree.command(
            name='dataset-upload',
            description='Register an immutable dataset for Glasslab research tasks.',
            guild=self.guild,
        )
        @app_commands.describe(
            dataset='Dataset file to store in the shared artifact registry.',
            name='Stable lowercase name used by experiment code.',
            role='Purpose such as train, test, labels, or input.',
            contains_labels='Whether the uploaded file contains target labels.',
        )
        async def dataset_upload(
            interaction: discord.Interaction,
            dataset: discord.Attachment,
            name: app_commands.Range[str, 1, 63],
            role: app_commands.Range[str, 1, 120] = 'input',
            contains_labels: bool = False,
        ) -> None:
            await self._on_dataset_upload(
                interaction,
                dataset=dataset,
                name=str(name),
                role=str(role),
                contains_labels=contains_labels,
            )

        @self.tree.command(
            name='research-artifacts',
            description='Download verified artifacts for a Glasslab research run.',
            guild=self.guild,
        )
        @app_commands.describe(
            run_id='Optional in a run thread; required from the main channel.',
            include_source='Include frozen source and task ZIP files.',
        )
        async def research_artifacts(
            interaction: discord.Interaction,
            run_id: str | None = None,
            include_source: bool = False,
        ) -> None:
            await self._on_research_artifacts(
                interaction,
                run_id=run_id,
                include_source=include_source,
            )

        @self.tree.command(
            name='research-turns',
            description=(
                'Show a Glasslab research run\'s redacted agent turn history.'
            ),
            guild=self.guild,
        )
        @app_commands.describe(
            run_id='Optional in a run thread; required from the main channel.',
            limit=(
                f'Most recent turns to show (default '
                f'{DEFAULT_DISCORD_TURN_LIMIT}, max '
                f'{MAXIMUM_DISCORD_TURN_LIMIT}).'
            ),
        )
        async def research_turns(
            interaction: discord.Interaction,
            run_id: str | None = None,
            limit: app_commands.Range[
                int, 1, MAXIMUM_DISCORD_TURN_LIMIT
            ] = DEFAULT_DISCORD_TURN_LIMIT,
        ) -> None:
            await self._on_research_turns(
                interaction,
                run_id=run_id,
                limit=int(limit),
            )

        @self.tree.command(
            name='research-status',
            description='Show the durable status of a Glasslab research run.',
            guild=self.guild,
        )
        @app_commands.describe(
            run_id='Optional in a run thread; required from the main channel.',
        )
        async def research_status(
            interaction: discord.Interaction,
            run_id: str | None = None,
        ) -> None:
            await self._on_research_status(interaction, run_id=run_id)

        @self.tree.command(
            name='research-list',
            description='List active and recent Glasslab research runs.',
            guild=self.guild,
        )
        async def research_list(interaction: discord.Interaction) -> None:
            await self._on_research_list(interaction)

        @self.tree.command(
            name='research-question',
            description=(
                'Ask the knowledge corpus — a ~1-minute cited answer, no run.'
            ),
            guild=self.guild,
        )
        @app_commands.describe(
            question='Research question for the knowledge corpus.',
        )
        async def research_question(
            interaction: discord.Interaction,
            question: app_commands.Range[str, 5, 2000],
        ) -> None:
            await self._on_research_question(
                interaction,
                question=str(question),
            )

        @self.tree.command(
            name='research-promote',
            description=(
                'Turn this research thread into a run (protocol draft starts).'
            ),
            guild=self.guild,
        )
        @app_commands.describe(
            objective='Optional run objective (default: from the thread).',
        )
        async def research_promote(
            interaction: discord.Interaction,
            objective: app_commands.Range[str, 10, 1000] | None = None,
        ) -> None:
            await self._on_research_promote(
                interaction,
                objective=objective,
            )

        @self.tree.command(
            name='packet',
            description=(
                'Show the full source text behind a citation packet id.'
            ),
            guild=self.guild,
        )
        @app_commands.describe(
            packet_id='The citation packet id (see [n] citations on answers).',
        )
        async def packet(
            interaction: discord.Interaction,
            packet_id: app_commands.Range[str, 1, 64],
        ) -> None:
            await self._on_packet(
                interaction,
                packet_id=str(packet_id),
            )
    def _register_run_control_command(self, operation: str) -> None:
        async def callback(
            interaction: discord.Interaction,
            run_id: str | None = None,
            reason: app_commands.Range[str, 3, 500] | None = None,
        ) -> None:
            await self._on_research_control(
                interaction,
                operation=operation,
                run_id=run_id,
                reason=str(reason) if reason else None,
            )

        callback.__name__ = f'research_{operation}'
        # discord.py derives the command signature from the callback's name and
        # type annotations, so the dynamically generated pause/resume callbacks
        # must carry real ones even though they are built at runtime.
        callback.__annotations__['interaction'] = discord.Interaction
        self.tree.command(
            name=f'research-{operation}',
            description=f'{operation.capitalize()} a Glasslab research run.',
            guild=self.guild,
        )(
            app_commands.describe(
                run_id='Optional in a run thread; required from the main channel.',
                reason='Optional reason recorded in the authoritative event log.',
            )(callback)
        )

    async def _on_ready(self) -> None:
        if self._commands_synced:
            return
        # Sync exactly once per process lifetime to keep command registrations
        # stable across reconnects and avoid hammering the sync endpoint.
        await self.tree.sync(guild=self.guild)
        self._commands_synced = True

    async def run(self) -> None:
        await self.client.start(self.bot_token, reconnect=True)

    async def close(self) -> None:
        await self.client.close()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    @staticmethod
    def _is_thread(interaction: discord.Interaction) -> bool:
        return isinstance(getattr(interaction, 'channel', None), discord.Thread)

    @staticmethod
    def _actor(interaction: discord.Interaction) -> DiscordControlActor:
        role_ids: set[str] = set()
        if isinstance(interaction.user, discord.Member):
            role_ids = {str(role.id) for role in interaction.user.roles}
        return DiscordControlActor(
            user_id=str(interaction.user.id),
            display_name=interaction.user.display_name,
            guild_id=(
                str(interaction.guild_id)
                if interaction.guild_id is not None
                else None
            ),
            role_ids=frozenset(role_ids),
        )

    async def _respond(
        self,
        interaction: discord.Interaction,
        content: str,
    ) -> None:
        await interaction.response.send_message(
            content,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _on_task_start(
        self,
        interaction: discord.Interaction,
        *,
        archive: discord.Attachment | None,
        objective: str | None,
    ) -> None:
        actor = self._actor(interaction)
        if not self.policy.is_authorized(actor):
            await self._respond(
                interaction,
                'You are not authorized to start Glasslab investigations.',
            )
            return
        if str(interaction.channel_id) != self.channel_id:
            await self._respond(
                interaction,
                'Start investigations from the configured Glasslab channel.',
            )
            return
        if archive is None and not objective:
            await self._respond(
                interaction,
                (
                    'Attach a task ZIP (problem.md + rubric) or provide an '
                    'objective to start an investigation.'
                ),
            )
            return
        if archive is None:
            # Objective-only path: accept immediately and create the run in
            # the background; the run thread appears in the channel shortly.
            await self._respond(
                interaction,
                (
                    'Research request accepted. Honeydew is drafting the '
                    'protocol and evaluation contract proposal; a run thread '
                    'will appear in this channel.'
                ),
            )
            task = asyncio.create_task(
                self._create_objective_run(
                    interaction=interaction,
                    objective=objective or '',
                )
            )
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            return
        # Archived path: compile + preflight + start (a 40-90s import), so
        # defer first and reply once the run is created.
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            if archive.size > self.engine.task_bundles.MAX_ARCHIVE_BYTES:
                raise ValueError(
                    'task archive exceeds the configured import size limit'
                )
            content = await archive.read()
            run = await asyncio.to_thread(
                execute_discord_task_creation,
                self.engine,
                filename=archive.filename,
                content=content,
                objective=objective,
            )
            destination = (
                f'<#{run.discord_thread_id}>'
                if run.discord_thread_id
                else f'run `{run.run_id}`'
            )
            await interaction.followup.send(
                (
                    f'Task compiled, preflighted, and started in {destination}. '
                    'Honeydew is drafting the protocol from the task and rubric.'
                ),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception as exc:
            await interaction.followup.send(
                f'Task import or run creation failed: {exc}',
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )

    async def _create_objective_run(
        self,
        *,
        interaction: discord.Interaction,
        objective: str,
    ) -> None:
        try:
            run = await asyncio.to_thread(
                execute_discord_run_creation,
                self.engine,
                objective=objective,
            )
            destination = (
                f'<#{run.discord_thread_id}>'
                if run.discord_thread_id
                else f'run `{run.run_id}`'
            )
            await interaction.followup.send(
                (
                    f'Research run created in {destination}. '
                    'Review the proposed protocol and evaluation contract there.'
                ),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception as exc:
            try:
                await interaction.followup.send(
                    f'Research run creation failed: {exc}',
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException:
                return

    def _resolve_controlled_run(
        self,
        *,
        channel_id: str,
        run_id: str | None,
    ) -> RunRecord:
        if run_id:
            run = self.engine.store.get_run(run_id)
        else:
            # Outside a run thread a run id is required; inside the thread the
            # run is resolved from the thread's own id, so commands issued in
            # the thread never need a run id.
            run = next(
                (
                    candidate
                    for candidate in self.engine.store.list_runs()
                    if candidate.discord_thread_id == channel_id
                ),
                None,
            )
            if run is None:
                raise ValueError(
                    'run_id is required outside a Glasslab research thread'
                )
        # Control is scoped to the run's own thread or the configured channel;
        # a run cannot be paused/cancelled from an unrelated channel.
        if channel_id not in {self.channel_id, run.discord_thread_id}:
            raise ValueError(
                'control the run from its research thread or the configured '
                'Glasslab channel'
            )
        return run

    async def _on_research_control(
        self,
        interaction: discord.Interaction,
        *,
        operation: str,
        run_id: str | None,
        reason: str | None,
    ) -> None:
        actor = self._actor(interaction)
        if not self.policy.is_authorized(actor):
            await self._respond(
                interaction,
                f'You are not authorized to {operation} Glasslab research runs.',
            )
            return
        try:
            run = self._resolve_controlled_run(
                channel_id=str(interaction.channel_id),
                run_id=run_id,
            )
            await self._respond(
                interaction,
                (
                    f'{operation.capitalize()} request accepted for '
                    f'`{run.run_id}`. The authoritative result will follow.'
                ),
            )
            task = asyncio.create_task(
                self._execute_run_control(
                    interaction=interaction,
                    operation=operation,
                    run_id=run.run_id,
                    actor=actor,
                    reason=reason,
                )
            )
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        except Exception as exc:
            await self._respond(
                interaction,
                f'Run {operation} failed: {exc}',
            )

    async def _execute_run_control(
        self,
        *,
        interaction: discord.Interaction,
        operation: str,
        run_id: str,
        actor: DiscordControlActor,
        reason: str | None,
    ) -> None:
        try:
            updated = await asyncio.to_thread(
                execute_discord_run_control,
                self.engine,
                operation=operation,
                run_id=run_id,
                actor=actor,
                reason=reason,
            )
            await interaction.followup.send(
                f'Run `{updated.run_id}` is now {updated.state.value}.',
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception as exc:
            try:
                await interaction.followup.send(
                    f'Run {operation} failed: {exc}',
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException:
                return

    async def _on_dataset_upload(
        self,
        interaction: discord.Interaction,
        *,
        dataset: discord.Attachment,
        name: str,
        role: str,
        contains_labels: bool,
    ) -> None:
        actor = self._actor(interaction)
        if not self.policy.is_authorized(actor):
            await self._respond(
                interaction,
                'You are not authorized to ingest Glasslab datasets.',
            )
            return
        if str(interaction.channel_id) != self.channel_id:
            await self._respond(
                interaction,
                'Upload datasets from the configured Glasslab channel.',
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            if dataset.size > self.maximum_dataset_upload_bytes:
                raise ValueError(
                    'dataset exceeds the Discord upload size limit'
                )
            content = await dataset.read()
            record = await asyncio.to_thread(
                execute_discord_dataset_ingestion,
                self.engine,
                filename=dataset.filename,
                content=content,
                name=name,
                role=role,
                contains_labels=contains_labels,
                actor=actor,
                media_type=dataset.content_type,
            )
            await interaction.followup.send(
                (
                    f'Dataset `{record.name}` registered as '
                    f'`{record.reference_uri}`.\n'
                    f'SHA-256: `{record.sha256}`; size: '
                    f'{record.size_bytes} bytes.\n'
                    'Use that reference in the task problem statement or '
                    'TaskSpec asset list.'
                ),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception as exc:
            await interaction.followup.send(
                f'Dataset ingestion failed: {exc}',
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )

    async def _on_research_artifacts(
        self,
        interaction: discord.Interaction,
        *,
        run_id: str | None,
        include_source: bool,
    ) -> None:
        actor = self._actor(interaction)
        if not self.policy.is_authorized(actor):
            await self._respond(
                interaction,
                'You are not authorized to download Glasslab artifacts.',
            )
            return
        try:
            run = self._resolve_controlled_run(
                channel_id=str(interaction.channel_id),
                run_id=run_id,
            )
        except Exception as exc:
            await self._respond(interaction, f'Artifact export failed: {exc}')
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            bundle = await asyncio.to_thread(
                execute_discord_artifact_export,
                self.engine,
                run_id=run.run_id,
                maximum_bytes=self.maximum_artifact_bundle_bytes,
                include_source=include_source,
            )
            await interaction.followup.send(
                (
                    f'Digest-verified artifact bundle for `{run.run_id}` '
                    f'({bundle.artifact_count} files).'
                ),
                file=discord.File(
                    io.BytesIO(bundle.content),
                    filename=bundle.filename,
                ),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception as exc:
            await interaction.followup.send(
                f'Artifact export failed: {exc}',
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )

    async def _on_research_turns(
        self,
        interaction: discord.Interaction,
        *,
        run_id: str | None,
        limit: int,
    ) -> None:
        actor = self._actor(interaction)
        if not self.policy.is_authorized(actor):
            await self._respond(
                interaction,
                'You are not authorized to inspect Glasslab research turns.',
            )
            return
        try:
            run = self._resolve_controlled_run(
                channel_id=str(interaction.channel_id),
                run_id=run_id,
            )
        except Exception as exc:
            await self._respond(interaction, f'Turn inspection failed: {exc}')
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            message = await asyncio.to_thread(
                execute_discord_turn_history,
                self.engine,
                run_id=run.run_id,
                limit=limit,
            )
            await interaction.followup.send(
                message,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception as exc:
            await interaction.followup.send(
                f'Turn inspection failed: {exc}',
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )

    async def _on_research_question(
        self,
        interaction: discord.Interaction,
        *,
        question: str,
    ) -> None:
        actor = self._actor(interaction)
        if not self.policy.is_authorized(actor):
            await self._respond(
                interaction,
                'You are not authorized to ask Glasslab research questions.',
            )
            return
        # Allow the configured main channel (one-shot) or a run/research
        # thread (conversational). In a thread, the channel id becomes the
        # stable conversation id so follow-up questions chain.
        if (
            str(interaction.channel_id) != self.channel_id
            and not self._is_thread(interaction)
        ):
            await self._respond(
                interaction,
                'Ask research questions from the configured Glasslab channel '
                'or a research thread.',
            )
            return
        # A research_answer turn is a bounded agent turn (about a minute);
        # the followup window is generous enough to hold it. The answer lands
        # visibly: in an existing thread via the followup, otherwise in a new
        # public thread named from the question.
        try:
            await interaction.response.defer(thinking=True)
        except Exception as exc:  # noqa: BLE001 - ack can race an earlier
            # interaction (duplicate invocation) or expire; nothing can be
            # delivered then, but it must be logged, not silent.
            logger.error(
                'research_question: could not acknowledge interaction: '
                '%s: %s',
                type(exc).__name__,
                exc,
            )
            return
        logger.info('research_question starting: %.120s', question)
        in_thread = self._is_thread(interaction)
        conversation_id = (
            f'discord-thread-{interaction.channel_id}'
            if in_thread
            else f'discord-{uuid4().hex[:16]}'
        )
        try:
            # Thread first: the conversation is visible immediately (follow-ups
            # can be typed), then the placeholder edits in the answer when the
            # turn completes — a poor-man's stream over a ~1-minute turn.
            if in_thread:
                placeholder = await interaction.followup.send(
                    'Working on it… (retrieving sources + drafting the answer)',
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            else:
                thread = await interaction.channel.create_thread(
                    name=f'research: {question[:80]}',
                    type=discord.ChannelType.public_thread,
                    auto_archive_duration=1440,
                )
                placeholder = await thread.send(
                    'Working on it… (retrieving sources + drafting the answer)',
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            answer = await asyncio.to_thread(
                execute_discord_research_question,
                self.engine,
                question=question,
                conversation_id=conversation_id,
            )
            rendered = [
                message for message in format_research_answer(answer)
                if message
            ]
            view = build_packet_button_view(answer)
            if not view.children:
                view = None
            sources_message_index = next(
                (i for i, m in enumerate(rendered) if m.startswith('**Sources')),
                len(rendered) - 1,
            )
            await placeholder.edit(
                content=rendered[0],
                view=view if sources_message_index == 0 else None,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            for index, extra in enumerate(rendered[1:], start=1):
                await placeholder.channel.send(
                    extra,
                    view=view if index == sources_message_index else None,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
        except Exception as exc:  # noqa: BLE001 - report, never crash the gateway
            logger.error(
                'research_question failed: %s: %s',
                type(exc).__name__,
                exc,
            )
            try:
                await interaction.followup.send(
                    f'Research question failed: {type(exc).__name__}: {exc}',
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except Exception as followup_exc:  # noqa: BLE001 - best effort
                logger.error(
                    'research_question: could not deliver failure notice: '
                    '%s: %s',
                    type(followup_exc).__name__,
                    followup_exc,
                )

    async def _on_research_promote(
        self,
        interaction: discord.Interaction,
        *,
        objective: str | None,
    ) -> None:
        actor = self._actor(interaction)
        if not self.policy.is_authorized(actor):
            await self._respond(
                interaction,
                'You are not authorized to promote Glasslab research threads.',
            )
            return
        if not self._is_thread(interaction):
            await self._respond(
                interaction,
                'Promote a research thread from inside the thread.',
            )
            return
        try:
            await interaction.response.defer(thinking=True)
        except Exception as exc:  # noqa: BLE001 - ack can race or expire
            logger.error(
                'research_promote: could not acknowledge interaction: '
                '%s: %s',
                type(exc).__name__,
                exc,
            )
            return
        conversation_id = f'discord-thread-{interaction.channel_id}'
        logger.info('research_promote starting for %s', conversation_id)
        try:
            placeholder = await interaction.followup.send(
                'Promoting this conversation into a run… (drafting the '
                'protocol)',
                allowed_mentions=discord.AllowedMentions.none(),
            )
            run = await asyncio.to_thread(
                self.engine.promote_conversation,
                conversation_id,
                objective=objective,
            )
            await placeholder.edit(
                content=(
                    f'Promoted to run **{run.run_id}** — protocol drafted, '
                    f'state `{run.state.value}`. Use `/research-status` in '
                    'this thread to follow it.'
                ),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception as exc:  # noqa: BLE001 - report, never crash the gateway
            logger.error(
                'research_promote failed: %s: %s',
                type(exc).__name__,
                exc,
            )
            try:
                await interaction.followup.send(
                    f'Promotion failed: {type(exc).__name__}: {exc}',
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except Exception as followup_exc:  # noqa: BLE001 - best effort
                logger.error(
                    'research_promote: could not deliver failure notice: '
                    '%s: %s',
                    type(followup_exc).__name__,
                    followup_exc,
                )

    async def _on_packet_button(
        self,
        interaction: discord.Interaction,
        *,
        packet_id: str,
        excerpt_prefix: str = '',
        source_index: int | None = None,
    ) -> None:
        actor = self._actor(interaction)
        if not self.policy.is_authorized(actor):
            await self._respond(
                interaction,
                'You are not authorized to inspect knowledge packets.',
            )
            return
        await interaction.response.defer(thinking=True)
        try:
            chunks = await asyncio.to_thread(
                format_packet_for_discord,
                self.engine,
                packet_id,
                excerpt_prefix,
                source_index,
            )
            await interaction.followup.send(
                chunks[0],
                allowed_mentions=discord.AllowedMentions.none(),
            )
            for chunk in chunks[1:]:
                await interaction.followup.send(
                    chunk,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
        except Exception as exc:  # noqa: BLE001 - report, never crash the gateway
            logger.error(
                'packet button failed: %s: %s',
                type(exc).__name__,
                exc,
            )
            try:
                await interaction.followup.send(
                    f'Packet lookup failed: {type(exc).__name__}: {exc}',
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except Exception as followup_exc:  # noqa: BLE001 - best effort
                logger.error(
                    'packet button: could not deliver failure notice: %s: %s',
                    type(followup_exc).__name__,
                    followup_exc,
                )

    async def _on_packet(
        self,
        interaction: discord.Interaction,
        *,
        packet_id: str,
    ) -> None:
        actor = self._actor(interaction)
        if not self.policy.is_authorized(actor):
            await self._respond(
                interaction,
                'You are not authorized to inspect knowledge packets.',
            )
            return
        if (
            str(interaction.channel_id) != self.channel_id
            and not self._is_thread(interaction)
        ):
            await self._respond(
                interaction,
                'Run /packet from the configured Glasslab channel or a '
                'research thread.',
            )
            return
        await interaction.response.defer(thinking=True)
        try:
            chunks = await asyncio.to_thread(
                format_packet_for_discord,
                self.engine,
                packet_id=packet_id,
            )
            if self._is_thread(interaction):
                first, *rest = chunks
                await interaction.followup.send(
                    first,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                for chunk in rest:
                    await interaction.followup.send(
                        chunk,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
            else:
                await interaction.channel.send(
                    chunks[0],
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                for chunk in chunks[1:]:
                    await interaction.channel.send(
                        chunk,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
        except Exception as exc:
            logger.error('packet lookup failed: %s: %s', type(exc).__name__, exc)
            try:
                await interaction.followup.send(
                    f'Packet lookup failed: {type(exc).__name__}: {exc}',
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except Exception as followup_exc:  # noqa: BLE001 - best effort
                logger.error(
                    'packet: could not deliver failure notice: %s: %s',
                    type(followup_exc).__name__,
                    followup_exc,
                )

    async def _on_research_cancel(
        self,
        interaction: discord.Interaction,
        *,
        run_id: str | None,
        reason: str | None,
    ) -> None:
        actor = self._actor(interaction)
        if not self.policy.is_authorized(actor):
            await self._respond(
                interaction,
                'You are not authorized to cancel Glasslab research runs.',
            )
            return
        try:
            run = self._resolve_controlled_run(
                channel_id=str(interaction.channel_id),
                run_id=run_id,
            )
            cancelled = await asyncio.to_thread(
                execute_discord_run_cancellation,
                self.engine,
                run_id=run.run_id,
                actor=actor,
                reason=reason,
            )
            await self._respond(
                interaction,
                f'Run `{cancelled.run_id}` is now {cancelled.state.value}.',
            )
        except Exception as exc:
            await self._respond(
                interaction,
                f'Run cancellation failed: {exc}',
            )

    async def _on_research_status(
        self,
        interaction: discord.Interaction,
        *,
        run_id: str | None,
    ) -> None:
        actor = self._actor(interaction)
        if not self.policy.is_authorized(actor):
            await self._respond(
                interaction,
                'You are not authorized to inspect Glasslab research runs.',
            )
            return
        try:
            run, actions, jobs = await asyncio.to_thread(
                self._load_run_status,
                channel_id=str(interaction.channel_id),
                run_id=run_id,
            )
        except Exception as exc:
            await self._respond(interaction, f'Status lookup failed: {exc}')
            return
        view = build_run_status_view(run, actions, jobs)
        await self._respond(
            interaction,
            bound_discord_message(render_run_status(view)),
        )

    def _load_run_status(
        self,
        *,
        channel_id: str,
        run_id: str | None,
    ) -> tuple[RunRecord, list[ActionRecord], list[JobRecord]]:
        # Synchronous, disk/DB-bound reads run in a worker thread so the
        # gateway event loop stays responsive; resolution is re-checked against
        # the durable store at this point, never trusted from Discord.
        run = self._resolve_controlled_run(
            channel_id=channel_id,
            run_id=run_id,
        )
        actions = self.engine.store.list_actions(run.run_id)
        jobs = self.engine.store.list_jobs(run.run_id)
        return run, actions, jobs

    async def _on_research_list(self, interaction: discord.Interaction) -> None:
        actor = self._actor(interaction)
        if not self.policy.is_authorized(actor):
            await self._respond(
                interaction,
                'You are not authorized to list Glasslab research runs.',
            )
            return
        if str(interaction.channel_id) != self.channel_id:
            await self._respond(
                interaction,
                'List research runs from the configured Glasslab channel.',
            )
            return
        try:
            runs = await asyncio.to_thread(self.engine.store.list_runs)
        except Exception as exc:
            await self._respond(interaction, f'Run list failed: {exc}')
            return
        await self._respond(
            interaction,
            bound_discord_message(render_run_list(select_runs_for_list(runs))),
        )

    async def _on_interaction(
        self,
        interaction: discord.Interaction,
    ) -> None:
        if interaction.type != discord.InteractionType.component:
            return
        data = interaction.data
        if not isinstance(data, dict):
            return
        custom_id = str(data.get('custom_id', ''))
        if custom_id.startswith(f'{PACKET_PREFIX}:'):
            parts = custom_id[len(PACKET_PREFIX) + 1:].split(':', 2)
            packet_id = parts[0]
            source_index = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
            excerpt_prefix = parts[2] if len(parts) > 2 else ''
            await self._on_packet_button(
                interaction,
                packet_id=packet_id,
                excerpt_prefix=excerpt_prefix,
                source_index=source_index,
            )
            return
        parts = custom_id.split(':', 2)
        if len(parts) != 3 or parts[0] != CONTROL_PREFIX:
            return
        operation, action_id = parts[1:]
        if operation not in {'approve', 'reject'}:
            return

        actor = self._actor(interaction)
        if not self.policy.is_authorized(actor):
            await self._respond(
                interaction,
                'You are not authorized to control Glasslab research runs.',
            )
            return
        # The button row is only a hint: authorization, thread attachment, and
        # the stored approval status are re-checked here because the engine
        # never trusts Discord state.
        try:
            action = self.engine.store.get_action(action_id)
            run = self.engine.store.get_run(action.run_id)
        except Exception:
            await self._respond(interaction, 'This action no longer exists.')
            return
        if (
            run.discord_thread_id is None
            or str(interaction.channel_id) != run.discord_thread_id
        ):
            await self._respond(
                interaction,
                'This control is not attached to this research thread.',
            )
            return
        if action.approval_status != ApprovalStatus.PENDING:
            await self._respond(
                interaction,
                f'This action is already {action.approval_status.value}.',
            )
            return

        if operation == 'reject':
            # Rejection requires revision feedback, so collect it through a
            # modal instead of acting on the click alone.
            await interaction.response.send_modal(
                RejectActionModal(
                    gateway=self,
                    action_id=action_id,
                    actor=actor,
                )
            )
            return

        await self._respond(
            interaction,
            (
                f'{operation.capitalize()} request received. '
                'The authoritative result will be posted in this thread.'
            ),
        )
        task = asyncio.create_task(
            self._execute(
                interaction=interaction,
                operation=operation,
                action_id=action_id,
                actor=actor,
                reason=None,
            )
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _execute(
        self,
        *,
        interaction: discord.Interaction,
        operation: str,
        action_id: str,
        actor: DiscordControlActor,
        reason: str | None,
    ) -> None:
        try:
            await asyncio.to_thread(
                execute_discord_action,
                self.engine,
                operation=operation,
                action_id=action_id,
                actor=actor,
                reason=reason,
            )
        except Exception as exc:
            try:
                await interaction.followup.send(
                    f'{operation.capitalize()} failed: {exc}',
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException:
                return

    async def submit_rejection(
        self,
        *,
        interaction: discord.Interaction,
        action_id: str,
        actor: DiscordControlActor,
        reason: str,
    ) -> None:
        await self._respond(
            interaction,
            (
                'Reject request received with revision feedback. '
                'The authoritative result will be posted in this thread.'
            ),
        )
        task = asyncio.create_task(
            self._execute(
                interaction=interaction,
                operation='reject',
                action_id=action_id,
                actor=actor,
                reason=reason,
            )
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)


class RejectActionModal(discord.ui.Modal):
    def __init__(
        self,
        *,
        gateway: DiscordControlGateway,
        action_id: str,
        actor: DiscordControlActor,
    ) -> None:
        super().__init__(title='Reject research action')
        self.gateway = gateway
        self.action_id = action_id
        self.actor = actor
        self.feedback = discord.ui.TextInput(
            label='Required revision',
            style=discord.TextStyle.paragraph,
            placeholder=(
                'Describe what Honeydew or Beaker must correct before '
                'requesting approval again.'
            ),
            min_length=5,
            max_length=1000,
            required=True,
        )
        self.add_item(self.feedback)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.gateway.submit_rejection(
            interaction=interaction,
            action_id=self.action_id,
            actor=self.actor,
            reason=str(self.feedback.value).strip(),
        )
