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
from typing import TYPE_CHECKING
from uuid import uuid4

logger = logging.getLogger(__name__)

import discord
from discord import app_commands

from .artifact_delivery import ArtifactBundle, build_run_artifact_bundle
from .schemas import (
    ApprovalStatus,
    IngestedDatasetRecord,
    ResearchAnswer,
    RunCreateRequest,
    RunRecord,
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


MAX_DISCORD_RESEARCH_ANSWER_CHARS = 2000


def format_research_answer(answer: ResearchAnswer) -> str:
    """Render a research answer within Discord's message length bound.

    The answer text is capped first, then citation lines (source + verbatim
    excerpt) fill the remaining budget so the trust loop — checking a claim
    against its source — survives even a long answer.
    """
    if answer.unanswerable:
        return (
            'The knowledge corpus does not contain material to answer this '
            'question. Rephrase it against the corpus domain (ML methods, '
            'uncertainty quantification, metric learning, and similar).'
        )
    lines: list[str] = []
    answer_text = ' '.join(answer.answer.strip().split())
    if len(answer_text) > 1100:
        answer_text = answer_text[:1097].rstrip() + '...'
    lines.append(answer_text)
    budget = MAX_DISCORD_RESEARCH_ANSWER_CHARS - len(answer_text) - 2
    citations = answer.citations
    if citations:
        header = f'**Sources ({len(citations)})**'
        budget -= len(header) + 2
        lines.extend(['', header])
        for index, citation in enumerate(citations, start=1):
            excerpt = ' '.join(citation.excerpt.split())
            if len(excerpt) > 100:
                excerpt = excerpt[:97].rstrip() + '...'
            line = f'[{index}] {citation.source} — "{excerpt}"'
            if len(line) > budget:
                break
            lines.append(line)
            budget -= len(line) + 1
    return '\n'.join(lines)


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
            name='research-start',
            description='Start a Glasslab research run from an objective.',
            guild=self.guild,
        )
        @app_commands.describe(
            objective=(
                'Research objective for Honeydew to turn into a protocol '
                'and evaluation contract proposal.'
            )
        )
        async def research_start(
            interaction: discord.Interaction,
            objective: app_commands.Range[str, 10, 2000],
        ) -> None:
            await self._on_research_start(
                interaction,
                objective=str(objective),
            )

        @self.tree.command(
            name='benchmark-start',
            description='Import and start a supported Glasslab ML benchmark.',
            guild=self.guild,
        )
        @app_commands.describe(
            archive='One supported ML_Benchmark_*.zip task bundle.',
            objective='Optional narrower objective for this benchmark run.',
        )
        async def benchmark_start(
            interaction: discord.Interaction,
            archive: discord.Attachment,
            objective: app_commands.Range[str, 10, 1000] | None = None,
        ) -> None:
            await self._on_benchmark_start(
                interaction,
                archive=archive,
                objective=str(objective) if objective else None,
            )

        @self.tree.command(
            name='task-start',
            description='Compile, preflight, and start a Glasslab research task.',
            guild=self.guild,
        )
        @app_commands.describe(
            archive='ZIP containing one problem.md and an optional evaluator rubric.',
            objective='Optional narrower objective for this research run.',
        )
        async def task_start(
            interaction: discord.Interaction,
            archive: discord.Attachment,
            objective: app_commands.Range[str, 10, 1000] | None = None,
        ) -> None:
            await self._on_benchmark_start(
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
            name='research-question',
            description=(
                'Ask a research question; Honeydew answers from the '
                'knowledge corpus with citations.'
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

    async def _on_research_start(
        self,
        interaction: discord.Interaction,
        *,
        objective: str,
    ) -> None:
        actor = self._actor(interaction)
        if not self.policy.is_authorized(actor):
            await self._respond(
                interaction,
                'You are not authorized to start Glasslab research runs.',
            )
            return
        if str(interaction.channel_id) != self.channel_id:
            await self._respond(
                interaction,
                'Start research runs from the configured Glasslab channel.',
            )
            return
        await self._respond(
            interaction,
            (
                'Research request accepted. Honeydew is drafting the protocol '
                'and evaluation contract proposal; a run thread will appear '
                'in this channel.'
            ),
        )
        task = asyncio.create_task(
            self._create_run(
                interaction=interaction,
                objective=objective,
            )
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _create_run(
        self,
        *,
        interaction: discord.Interaction,
        objective: str,
    ) -> None:
        try:
            # Engine work is synchronous and disk/DB bound; run it in a worker
            # thread so the gateway event loop stays responsive.
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

    async def _on_benchmark_start(
        self,
        interaction: discord.Interaction,
        *,
        archive: discord.Attachment,
        objective: str | None,
    ) -> None:
        actor = self._actor(interaction)
        if not self.policy.is_authorized(actor):
            await self._respond(
                interaction,
                'You are not authorized to start Glasslab benchmark runs.',
            )
            return
        if str(interaction.channel_id) != self.channel_id:
            await self._respond(
                interaction,
                'Start benchmark runs from the configured Glasslab channel.',
            )
            return
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
                f'Benchmark import or run creation failed: {exc}',
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )

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
        if str(interaction.channel_id) != self.channel_id:
            await self._respond(
                interaction,
                'Ask research questions from the configured Glasslab channel.',
            )
            return
        # A research_answer turn is a bounded agent turn (about a minute);
        # the followup window is generous enough to hold it.
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
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
        try:
            answer = await asyncio.to_thread(
                execute_discord_research_question,
                self.engine,
                question=question,
                conversation_id=f'discord-{uuid4().hex[:16]}',
            )
            await interaction.followup.send(
                format_research_answer(answer),
                ephemeral=True,
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
