from __future__ import annotations

from hashlib import sha256
import json
import os
from pathlib import Path
import re
import subprocess
import zipfile
from threading import RLock
from typing import Any
from uuid import uuid4, uuid5, NAMESPACE_URL

from .analysis_notebook import write_analysis_notebook
from .artifact_delivery import ArtifactDeliveryError
from .cluster import ClusterExecutor
from .config import Settings
from .contract_candidates import ContractCandidateManager
from .contracts import EvaluationContractResolver
from .discord_adapter import DiscordAdapter
from .datasets import DatasetIngestionManager
from .evidence import (
    EvidencePhase,
    build_evidence_snapshot,
    serialize_evidence,
)
from .matrix import expand_experiment_matrix
import httpx

from .opencode_runtime import AgentRuntime, OpenCodeRuntimeError
from .policy import ActionPolicy
from .preflight import MatrixPreflightReport, preflight_matrix
from .research_store import ResearchStore
from .schemas import (
    ActionRecord,
    AgentName,
    AgentTurnResult,
    ApprovalStatus,
    ArtifactRecord,
    ContractCandidateRequest,
    ContextPacket,
    EvaluationContractDescriptor,
    EvaluationContractProposal,
    ExperimentMatrix,
    JOB_TERMINAL_STATUSES,
    JobRecord,
    JobStatus,
    PolicyClassification,
    RequestedAction,
    RunCreateRequest,
    RunRecord,
    RunState,
    TerminalRetryRequest,
    TERMINAL_STATES,
    TurnKind,
    TurnRecord,
    utc_now,
)
from .storage import ConcurrencyConflict
from .task_bundles import (
    TaskBundleManager,
    TaskBundleRecord,
    TaskPreflight,
)
from .workspaces import WorkspaceManager
from .knowledge_manager import KnowledgeManager


class WorkflowError(RuntimeError):
    pass


NON_RETRYABLE_TURN_FAILURE_CLASSES = frozenset(
    {'validation', 'kind_mismatch', 'workflow'}
)
_RETRYABLE_TURN_FAILURE_CLASSES = frozenset(
    {'startup', 'turn_timeout', 'repeated_tool_loop', 'provider', 'network'}
)


def _is_retryable_turn_failure(exc: Exception) -> bool:
    """Transient runtime failures are retryable; deterministic ones are not."""
    if isinstance(exc, (httpx.HTTPError, TimeoutError)):
        return True
    if isinstance(exc, OpenCodeRuntimeError):
        return (
            exc.failure_class in _RETRYABLE_TURN_FAILURE_CLASSES
            or exc.failure_class is None
        )
    return False


class ResearchOrchestrator:
    def __init__(
        self,
        *,
        settings: Settings,
        store: ResearchStore,
        runtime: AgentRuntime,
        workspaces: WorkspaceManager,
        contracts: EvaluationContractResolver,
        contract_candidates: ContractCandidateManager,
        policy: ActionPolicy,
        cluster: ClusterExecutor,
        discord: DiscordAdapter,
        task_bundles: TaskBundleManager | None = None,
        datasets: DatasetIngestionManager | None = None,
        knowledge: KnowledgeManager | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.runtime = runtime
        self.workspaces = workspaces
        self.contracts = contracts
        self.contract_candidates = contract_candidates
        self.datasets = datasets or DatasetIngestionManager(
            store=store,
            root=settings.dataset_upload_root,
            shared_mount_root=settings.shared_mount_root,
            maximum_bytes=settings.maximum_dataset_upload_bytes,
        )
        self.task_bundles = task_bundles or TaskBundleManager(
            root=settings.task_bundle_root,
            shared_mount_root=settings.shared_mount_root,
            dataset_catalog_path=settings.benchmark_dataset_catalog_path,
            task_asset_root=settings.task_asset_root,
            maximum_asset_bytes=settings.maximum_task_asset_bytes,
            ingested_datasets=self.datasets,
        )
        # Knowledge context is additive and failure-tolerant: the manager is
        # constructed from settings and wired into every turn (see
        # _get_agent_context). None of it is allowed to block a run.
        self.knowledge = knowledge or KnowledgeManager(
            store=store,
            root=Path(settings.knowledge_root),
            allowlist_roots=[Path(p) for p in settings.knowledge_allowlist_roots],
            chunk_size=settings.knowledge_chunk_size,
            chunk_overlap=settings.knowledge_chunk_overlap,
            max_source_bytes=settings.knowledge_max_source_bytes,
            max_results=settings.knowledge_max_results,
            token_budget=settings.knowledge_token_budget,
        )
        self.policy = policy
        self.cluster = cluster
        self.discord = discord
        # Serializes all run-advancing mutations in this process. Pause and
        # cancel deliberately do NOT take this lock so they can abort a model
        # turn that currently holds it (see pause_run).
        self._advance_lock = RLock()

    def _publish_latest(self, run_id: str) -> None:
        events = self.store.list_events(run_id)
        if not events:
            return
        self._publish_event(run_id, events[-1])

    def _publish_event(self, run_id: str, event) -> None:
        run = self.store.get_run(run_id)
        try:
            status_message_id = self.discord.publish(
                thread_id=run.discord_thread_id,
                status_message_id=run.discord_status_message_id,
                event=event,
            )
            if (
                status_message_id
                and status_message_id != run.discord_status_message_id
            ):
                current = self.store.get_run(run_id)
                self.store.replace_run(
                    current.model_copy(
                        update={
                            'discord_status_message_id': status_message_id,
                        }
                    ),
                    expected_version=current.version,
                )
        except Exception:
            # Discord is a replaceable projection and cannot fail the workflow.
            return

    def _republish_pending_approval(self, run_id: str) -> bool:
        pending_ids = {
            action.action_id
            for action in self.store.list_actions(run_id)
            if action.approval_status == ApprovalStatus.PENDING
        }
        for event in reversed(self.store.list_events(run_id)):
            if (
                event.event_type == 'action.proposed'
                and event.payload.get('action_id') in pending_ids
            ):
                self._publish_event(run_id, event)
                return True
        return False

    def _attach_discord_thread(self, run_id: str) -> RunRecord:
        """Attach one independent, best-effort transcript thread to a run."""
        run = self.store.get_run(run_id)
        if run.discord_thread_id:
            return run
        try:
            thread_id = self.discord.create_thread(
                run_id=run_id, objective=run.objective,
            )
        except Exception:
            return run
        if not thread_id:
            return run
        return self.store.replace_run(
            run.model_copy(update={'discord_thread_id': thread_id}),
            expected_version=run.version,
        )

    def _event(
        self,
        run_id: str,
        *,
        source: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        self.store.append_event(
            run_id=run_id,
            source=source,
            event_type=event_type,
            payload=payload or {},
        )
        self._publish_latest(run_id)

    def _transition(
        self,
        run_id: str,
        target: RunState,
        *,
        payload: dict[str, Any] | None = None,
        updates: dict[str, Any] | None = None,
    ) -> RunRecord:
        run = self.store.transition_run(
            run_id,
            target,
            payload=payload,
            updates=updates,
        )
        self._publish_latest(run_id)
        return run

    def create_run(self, request: RunCreateRequest) -> RunRecord:
        with self._advance_lock:
            task = (
                self.task_bundles.get(
                    request.task_id,
                    request.task_bundle_digest,
                )
                if request.task_id
                else None
            )
            contract_id = (
                request.evaluation_contract_id
                or (task.default_contract_id if task else None)
                or self.settings.default_evaluation_contract_id
            )
            contract_version = (
                request.evaluation_contract_version
                or (task.default_contract_version if task else None)
                or self.settings.default_evaluation_contract_version
            )
            contract = self.contracts.resolve(contract_id, contract_version)
            if task:
                preflight = self.task_preflight(task)
                if not preflight.ready:
                    raise WorkflowError(
                        'task preflight failed: '
                        + '; '.join(preflight.blocking_issues)
                    )
            run_id = uuid4().hex
            paths = self.workspaces.prepare(run_id)
            base_commit = self.workspaces.worktree_base_commit(run_id)
            if task:
                self.workspaces.install_task_bundle(
                    run_id=run_id,
                    problem_path=task.problem_path,
                    evaluator_prompt_path=task.evaluator_prompt_path,
                )
            now = utc_now()
            record = RunRecord(
                run_id=run_id,
                objective=request.objective,
                state=RunState.CREATED,
                evaluation_contract_id=contract_id,
                evaluation_contract_version=contract_version,
                evaluation_contract_digest=contract.digest,
                task_id=task.task_id if task else None,
                task_bundle_digest=task.digest if task else None,
                task_bundle_path=task.archive_path if task else None,
                task_definition=(
                    task.model_dump(mode='json') if task else None
                ),
                workspace_base_commit=base_commit,
                beaker_workspace=str(paths.beaker),
                honeydew_workspace=str(paths.honeydew),
                shared_artifacts_path=str(paths.shared_artifacts),
                reports_path=str(paths.reports),
                maximum_turns=request.maximum_turns or self.settings.maximum_turns,
                maximum_runtime_seconds=(
                    request.maximum_runtime_seconds
                    or self.settings.maximum_runtime_seconds
                ),
                # Cap the per-run concurrency at the service ceiling even when
                # the request explicitly asks for more; the cap is global policy.
                maximum_parallel_jobs=min(
                    request.maximum_parallel_jobs
                    or self.settings.maximum_parallel_jobs,
                    self.settings.maximum_parallel_jobs,
                ),
                active_since=now,
                created_at=now,
                updated_at=now,
            )
            self.store.create_run(
                record,
                one_active_run=self.settings.one_active_run,
            )
            try:
                thread_id = self.discord.create_thread(
                    run_id=run_id,
                    objective=request.objective,
                )
            except Exception:
                # Discord is a replaceable projection; a thread failure must not
                # fail run creation (the run itself is already committed above).
                thread_id = None
            if thread_id:
                current = self.store.get_run(run_id)
                record = self.store.replace_run(
                    current.model_copy(
                        update={'discord_thread_id': thread_id}
                    ),
                    expected_version=current.version,
                )
            self._publish_latest(run_id)
            # Walk through PREPARING explicitly even though the next line moves
            # straight on: if the process dies between these transitions,
            # recover() sees a run in PREPARING and resumes drafting, which is
            # the only signal that workspace setup finished.
            self._transition(run_id, RunState.PREPARING)
            self._materialize_objective_datasets(run_id)
            self._transition(run_id, RunState.HONEYDEW_DRAFTING_PROTOCOL)
            try:
                self._draft_protocol(run_id)
            except Exception as exc:
                self._fail_run(run_id, exc)
                raise
            return self.store.get_run(run_id)

    def _materialize_objective_datasets(self, run_id: str) -> None:
        # Copy every ingested dataset cited by the objective into the agent
        # workspaces. Agent runtimes have no network egress, so without the
        # local copy the implementer can only fail (issue #98).
        run = self.store.get_run(run_id)
        dataset_ids = sorted(
            set(
                re.findall(
                    r'glasslab-dataset://([0-9a-f]{64})',
                    run.objective,
                )
            )
        )
        if not dataset_ids:
            return
        datasets: list[tuple[str, str]] = []
        for dataset_id in dataset_ids:
            record = self.store.get_dataset(dataset_id)
            source = Path(record.path)
            digest = sha256()
            with source.open('rb') as handle:
                for chunk in iter(
                    lambda: handle.read(1024 * 1024), b''
                ):
                    digest.update(chunk)
            if digest.hexdigest() != record.sha256:
                raise WorkflowError(
                    'objective dataset failed checksum verification: '
                    f'{record.name}'
                )
            datasets.append((record.path, record.filename))
        installed = self.workspaces.install_run_datasets(
            run_id=run_id,
            datasets=datasets,
        )
        self._event(
            run_id,
            source='orchestrator',
            event_type='run.datasets_materialized',
            payload={'files': sorted(set(installed))},
        )

    def import_task_bundle(
        self,
        *,
        filename: str,
        content: bytes,
    ) -> TaskBundleRecord:
        with self._advance_lock:
            return self._compile_task_bundle(
                filename=filename,
                content=content,
            )

    def _compile_task_bundle(
        self,
        *,
        filename: str,
        content: bytes,
    ) -> TaskBundleRecord:
        digest = sha256(content).hexdigest()
        existing = self.task_bundles.find_by_digest(digest)
        if existing is not None:
            # Re-import of identical bytes is a no-op: the compiled bundle is
            # keyed by content digest, so the archive is only ever compiled once.
            return existing
        staged = self.task_bundles.stage_archive(
            filename=filename,
            content=content,
        )
        # The session is keyed by a digest-derived id rather than a real run id,
        # so a recompiled archive never leaks into a research run's workspace.
        compiler_id = f'task-compiler-{staged.digest[:16]}'
        workspace = staged.root / 'normalized'
        try:
            session = self.runtime.ensure_session(
                run_id=compiler_id,
                agent=AgentName.HONEYDEW,
                workspace=workspace,
                existing_session_id=None,
            )
            result, _ = self.runtime.run_turn(
                run_id=compiler_id,
                agent=AgentName.HONEYDEW,
                workspace=workspace,
                session_id=session.session_id,
                prompt=(
                    'Compile this task archive into a Glasslab TaskSpec. Read '
                    '`problem.md` and `eval_agent_prompt.md`. Select only one '
                    'approved runtime profile: cpu-ml-standard-v1 for tabular, '
                    'classical ML, and lightweight CPU work; or '
                    'gpu-ml-standard-v1 when the task materially requires '
                    'PyTorch/CUDA training. Identify every external dataset or '
                    'model asset as an asset proposal. Preserve any exact '
                    '`glasslab-dataset://<sha256>` reference from the problem '
                    'as approved_uri. Otherwise use a canonical public HTTPS '
                    'URL only when one is explicit or confidently known. Do '
                    'not invent credentials, private URLs, commands, images, '
                    'Kubernetes fields, or evaluator code. List exact metric '
                    'keys and evidence artifacts required by the rubric. Put '
                    'unresolved requirements in missing_inputs. Return kind '
                    '"task_spec", task_spec_proposal, no requested actions, '
                    'and done=true.'
                ),
            )
            if (
                result.kind != TurnKind.TASK_SPEC
                or result.task_spec_proposal is None
                or result.requested_actions
            ):
                raise WorkflowError(
                    'Honeydew task compiler did not return a valid TaskSpec'
                )
            return self.task_bundles.compile(
                staged,
                result.task_spec_proposal,
            )
        except Exception:
            self.task_bundles.discard_staged(staged)
            raise
        finally:
            self.runtime.release(
                run_id=compiler_id,
                agent=AgentName.HONEYDEW,
            )

    def task_preflight(self, task: TaskBundleRecord) -> TaskPreflight:
        try:
            self.contracts.resolve(
                task.default_contract_id,
                task.default_contract_version,
            )
            evaluator_ready = True
        except Exception:
            evaluator_ready = False
        return self.task_bundles.preflight(
            task,
            permitted_images=self.policy.permitted_images,
            evaluator_ready=evaluator_ready,
        )

    def _check_turn_budget(self, run: RunRecord) -> None:
        # Both limits are soft caps checked only at turn boundaries: they stop a
        # turn from STARTING once exhausted, but never interrupt an in-flight
        # model turn. turn_number is incremented after this check, so `>=`
        # admits exactly maximum_turns completed turns.
        if run.turn_number >= run.maximum_turns:
            self._transition(
                run.run_id,
                RunState.TIMED_OUT,
                payload={'reason': 'maximum turn count reached'},
            )
            raise WorkflowError('maximum turn count reached')
        active_runtime = run.active_runtime_seconds
        if run.active_since is not None:
            # active_runtime_seconds is the accumulated base stored by
            # storage.transition_run; add the time elapsed since the clock last
            # started (set on creation and on every resume/approval).
            active_runtime += max(
                0.0,
                (utc_now() - run.active_since).total_seconds(),
            )
        if active_runtime > run.maximum_runtime_seconds:
            self._transition(
                run.run_id,
                RunState.TIMED_OUT,
                payload={
                    'reason': 'maximum active runtime reached',
                    'active_runtime_seconds': active_runtime,
                },
            )
            raise WorkflowError('maximum active runtime reached')

    def _write_recovery_checkpoint(
        self,
        *,
        run_id: str,
        agent: AgentName,
        expected_kind: TurnKind,
        error: str,
    ) -> tuple[Path, dict[str, Any]]:
        run = self.store.get_run(run_id)
        workspace = Path(
            run.honeydew_workspace
            if agent == AgentName.HONEYDEW
            else run.beaker_workspace
        )
        # Only the last few structured outputs travel into the recovery prompt;
        # the full history lives in the event log, and unbounded transcripts
        # would blow the replacement session's context window.
        completed = [
            turn
            for turn in self.store.list_turns(run_id)
            if turn.status == 'completed' and turn.structured_output is not None
        ][-4:]
        try:
            # git status is best-effort context for the fresh session; the
            # worktree itself is the authoritative state.
            status = subprocess.run(
                ['git', 'status', '--short'],
                cwd=workspace,
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
            )
            workspace_status = status.stdout.splitlines()[:80]
        except (OSError, subprocess.SubprocessError) as exc:
            workspace_status = [f'git status unavailable: {exc}']
        checkpoint = {
            'schema_version': 'glasslab-recovery-checkpoint-v1',
            'run_id': run_id,
            'agent': agent.value,
            'state': run.state.value,
            'expected_turn_kind': expected_kind.value,
            'objective': run.objective[:1000],
            'evaluation_contract': {
                'contract_id': run.evaluation_contract_id,
                'version': run.evaluation_contract_version,
                'digest': run.evaluation_contract_digest,
            },
            'protocol_path': run.protocol_path,
            'task_id': run.task_id,
            'failed_turn_error': error[:1000],
            'workspace_status': workspace_status,
            'recent_completed_turns': [
                {
                    'agent': turn.agent.value,
                    'kind': turn.structured_output.kind.value,
                    'summary': turn.structured_output.summary[:500],
                }
                for turn in completed
            ],
            'instruction': (
                'Continue the current bounded phase from the persisted worktree. '
                'Inspect existing files before editing and do not repeat completed '
                'work solely because the OpenCode session was rotated.'
            ),
        }
        path = self.workspaces.write_recovery_checkpoint(
            run_id=run_id,
            agent=agent,
            payload=checkpoint,
        )
        return path, checkpoint

    def _rotate_agent_session(
        self,
        *,
        run_id: str,
        agent: AgentName,
        expected_kind: TurnKind,
        error: str,
    ) -> None:
        release_error: str | None = None
        try:
            self.runtime.release(run_id=run_id, agent=agent)
        except Exception as exc:
            release_error = str(exc)
        current = self.store.get_run(run_id)
        # Clear the session ids from the authoritative record BEFORE writing the
        # checkpoint: any later recovery then sees a fresh-session run and must
        # rebuild from the checkpoint instead of reusing a poisoned session.
        updates: dict[str, Any] = {'current_agent': None}
        if agent == AgentName.HONEYDEW:
            updates.update(
                {
                    'honeydew_runtime_id': None,
                    'honeydew_session_id': None,
                }
            )
        else:
            updates.update(
                {
                    'beaker_runtime_id': None,
                    'beaker_session_id': None,
                }
            )
        self.store.replace_run(
            current.model_copy(update=updates),
            expected_version=current.version,
        )
        path, _ = self._write_recovery_checkpoint(
            run_id=run_id,
            agent=agent,
            expected_kind=expected_kind,
            error=error,
        )
        self._event(
            run_id,
            source='orchestrator',
            event_type='agent.session_rotated',
            payload={
                'agent': agent.value,
                'reason': error[:1000],
                'checkpoint_path': str(path),
                'next_session': 'fresh',
                'runtime_release_error': release_error,
            },
        )

    def _recovery_context(
        self,
        *,
        run_id: str,
        agent: AgentName,
    ) -> str:
        checkpoint_path = self.workspaces.paths(run_id).events / (
            f'{agent.value}-recovery-checkpoint.json'
        )
        if not checkpoint_path.is_file():
            return ''
        checkpoint = json.loads(checkpoint_path.read_text(encoding='utf-8'))
        return (
            'This is a fresh OpenCode session after an interrupted or failed '
            'turn. The worktree and authoritative workflow state were preserved. '
            'Use this compact checkpoint and inspect the existing worktree before '
            'continuing:\n'
            + json.dumps(checkpoint, indent=2, sort_keys=True)
            + '\n\nCurrent bounded task:\n'
        )

    def _get_agent_context(
        self,
        *,
        run_id: str,
        agent: AgentName,
        turn_number: int,
        turn_kind: TurnKind,
        query: str,
    ) -> ContextPacket | None:
        """Retrieve and persist scoped context for an agent turn.

        The fallback to None on any retrieval failure is deliberate: knowledge
        is additive context, never a hard dependency of a turn. The workflow
        must not stall an experiment because the index or embedding endpoint
        is briefly unavailable. Scope is decided inside KnowledgeManager.retrieve
        from the agent/turn_kind, so this caller cannot widen it.
        """
        try:
            return self.knowledge.retrieve(
                run_id=run_id,
                agent=agent.value,
                turn_number=turn_number,
                turn_kind=turn_kind.value,
                query=query,
                index_version='v1',
                max_results=self.settings.knowledge_max_results,
                token_budget=self.settings.knowledge_token_budget,
                run_scope=run_id,
                allowed_source_types=None,
            )
        except Exception:
            return None

    def _retrieval_query(
        self,
        *,
        run_id: str,
        objective: str,
        prompt: str,
        turn_kind: TurnKind,
    ) -> str:
        """Derive a compact, deterministic retrieval intent for a turn.

        Combining the turn kind, the run objective, and the first 300 chars of
        the prompt gives stable lexical signal without letting the agent steer
        the query toward arbitrary material.
        """
        objective = (objective or '').strip()
        prompt_head = ' '.join(prompt.split())[:300].strip()
        return f'{turn_kind.value} {objective} {prompt_head}'.strip()

    @staticmethod
    def _required_turn_kind_instruction(expected_kind: TurnKind) -> str:
        """Make the workflow-selected result variant unambiguous to the runtime.

        AgentTurnResult permits several valid variants. The state machine has
        already selected exactly one of them, so the model must not infer that
        choice from the broader task text or schema alone.
        """
        return (
            '\n\nAUTHORITATIVE STRUCTURED OUTPUT CONTRACT:\n'
            f'- Set the JSON `kind` field to exactly `{expected_kind.value}`.\n'
            '- No other `kind` is acceptable for this turn, even if another '
            'variant appears plausible.\n'
            '- Complete the requested phase, then return one complete '
            'AgentTurnResult object with that exact kind.\n'
        )

    def _should_retry_turn(
        self,
        exc: Exception,
        run_id: str,
        agent: AgentName,
    ) -> bool:
        if not _is_retryable_turn_failure(exc):
            return False
        failed_turns = sum(
            1
            for turn in self.store.list_turns(run_id)
            if turn.agent == agent and turn.status == 'failed'
        )
        return failed_turns <= self.settings.agent_turn_max_retries

    def _retry_agent_turn(
        self,
        *,
        run_id: str,
        agent: AgentName,
        prompt: str,
        expected_kind: TurnKind,
        input_event: dict[str, Any],
    ) -> tuple[TurnRecord, AgentTurnResult]:
        # The failed attempt already consumed a turn number; roll it back so
        # the bounded retry does not double-count against the turn budget.
        current = self.store.get_run(run_id)
        self.store.replace_run(
            current.model_copy(
                update={'turn_number': max(0, current.turn_number - 1)}
            ),
            expected_version=current.version,
        )
        return self._run_agent_turn(
            run_id=run_id,
            agent=agent,
            prompt=prompt,
            expected_kind=expected_kind,
            input_event=input_event,
        )

    def _run_agent_turn(
        self,
        *,
        run_id: str,
        agent: AgentName,
        prompt: str,
        expected_kind: TurnKind,
        input_event: dict[str, Any],
    ) -> tuple[TurnRecord, AgentTurnResult]:
        run = self.store.get_run(run_id)
        self._check_turn_budget(run)
        workspace = Path(
            run.honeydew_workspace
            if agent == AgentName.HONEYDEW
            else run.beaker_workspace
        )
        existing_session = (
            run.honeydew_session_id
            if agent == AgentName.HONEYDEW
            else run.beaker_session_id
        )
        recovery_context = ''
        if existing_session is None:
            # The recovery checkpoint is injected only into a brand-new session
            # (after a rotation or a restart). A continuing session already has
            # the previous turns in its own context.
            recovery_context = self._recovery_context(
                run_id=run_id,
                agent=agent,
            )
        session = self.runtime.ensure_session(
            run_id=run_id,
            agent=agent,
            workspace=workspace,
            existing_session_id=existing_session,
        )
        run = self.store.get_run(run_id)
        # Persist the live session and turn number before the model does any
        # work, so a crash or pause mid-turn can find and abort the right
        # session (see recover() and pause_run).
        session_updates = {
            'current_agent': agent,
            'turn_number': run.turn_number + 1,
        }
        if agent == AgentName.HONEYDEW:
            session_updates.update(
                {
                    'honeydew_runtime_id': session.runtime_id,
                    'honeydew_session_id': session.session_id,
                }
            )
        else:
            session_updates.update(
                {
                    'beaker_runtime_id': session.runtime_id,
                    'beaker_session_id': session.session_id,
                }
            )
        self.store.replace_run(
            run.model_copy(update=session_updates),
            expected_version=run.version,
        )
        turn = TurnRecord(
            run_id=run_id,
            agent=agent,
            opencode_session_id=session.session_id,
            input_event=input_event,
            status='running',
        )
        self.store.save_turn(turn)
        self._event(
            run_id,
            source=agent.value,
            event_type='agent.turn_started',
            payload={
                'turn_id': turn.turn_id,
                'agent': agent.value,
                'kind': expected_kind.value,
            },
        )
        try:
            # Per-turn knowledge retrieval (see KnowledgeManager). The query is
            # derived from the turn's prompt and objective; the result is scoped
            # by agent role + turn kind and persisted as a ContextPacket before
            # the agent sees any of it. Attachment is recorded as an event so
            # the packet is citable (knowledge://context:<id>) and auditable.
            context_packet = self._get_agent_context(
                run_id=run_id,
                agent=agent,
                turn_number=run.turn_number + 1,
                turn_kind=expected_kind,
                query=self._retrieval_query(
                    run_id=run_id,
                    objective=run.objective,
                    prompt=prompt,
                    turn_kind=expected_kind,
                ),
            )
            if context_packet and context_packet.exact_text_supplied:
                prompt = (
                    context_packet.exact_text_supplied + '\n\n' + prompt
                )
                self._event(
                    run_id,
                    source='orchestrator',
                    event_type='agent.context_attached',
                    payload={
                        'turn_id': turn.turn_id,
                        'packet_id': context_packet.packet_id,
                        'agent': agent.value,
                        'turn_kind': expected_kind.value,
                        'ranked_count': len(context_packet.ranked_sources),
                        'token_count': (
                            context_packet.exact_text_supplied
                            and len(
                                context_packet.exact_text_supplied.split()
                            )
                            or 0
                        ),
                    },
                )
            if recovery_context:
                prompt = recovery_context + prompt
            # Append the phase-specific discriminator after all retrieved and
            # recovery context. Runtime schemas allow many valid kinds, but the
            # state machine has already chosen the one valid for this turn.
            prompt += self._required_turn_kind_instruction(expected_kind)
            result, message_id = self.runtime.run_turn(
                run_id=run_id,
                agent=agent,
                workspace=workspace,
                session_id=session.session_id,
                prompt=prompt,
            )
            if result.kind != expected_kind:
                returned_kind = result.kind
                self._event(
                    run_id,
                    source='orchestrator',
                    event_type='agent.output_rejected',
                    payload={
                        'turn_id': turn.turn_id,
                        'returned_kind': returned_kind.value,
                        'expected_kind': expected_kind.value,
                        'repair_attempted': True,
                    },
                )
                result, message_id = self.runtime.run_turn(
                    run_id=run_id,
                    agent=agent,
                    workspace=workspace,
                    session_id=session.session_id,
                    prompt=(
                        prompt
                        + '\n\nStructured kind correction. Your previous '
                        f'response used kind `{returned_kind.value}`, but this '
                        f'turn requires kind `{expected_kind.value}` and no '
                        'other. Complete the original task and return a '
                        'complete AgentTurnResult with exactly that kind. Do '
                        'not repeat an earlier workflow phase and do not '
                        'request actions.'
                    ),
                )
                if result.kind != expected_kind:
                    raise WorkflowError(
                        f'{agent.value} returned {result.kind}; expected '
                        f'{expected_kind} after one focused repair'
                    )
            completed = turn.model_copy(
                update={
                    'opencode_message_id': message_id,
                    'structured_output': result,
                    'status': 'completed',
                    'updated_at': utc_now(),
                }
            )
            self.store.save_turn(completed)
            current = self.store.get_run(run_id)
            self.store.replace_run(
                current.model_copy(update={'current_agent': None}),
                expected_version=current.version,
            )
            self._event(
                run_id,
                source=agent.value,
                event_type='agent.turn_completed',
                payload={
                    'turn_id': completed.turn_id,
                    'kind': result.kind.value,
                    'summary': result.summary,
                    'recommended_next_state': (
                        result.recommended_next_state.value
                        if result.recommended_next_state
                        else None
                    ),
                    'message_to_other_agent': result.message_to_other_agent,
                },
            )
        except Exception as exc:
            failed = turn.model_copy(
                update={
                    'status': 'failed',
                    'error': str(exc),
                    'updated_at': utc_now(),
                }
            )
            self.store.save_turn(failed)
            failure_class = getattr(exc, 'failure_class', None)
            self._event(
                run_id,
                source=agent.value,
                event_type='agent.turn_completed',
                payload={
                    'turn_id': turn.turn_id,
                    'status': 'failed',
                    'error': str(exc),
                    'failure_class': failure_class,
                },
            )
            self._rotate_agent_session(
                run_id=run_id,
                agent=agent,
                expected_kind=expected_kind,
                error=str(exc),
            )
            if self._should_retry_turn(exc, run_id, agent):
                return self._retry_agent_turn(
                    run_id=run_id,
                    agent=agent,
                    prompt=prompt,
                    expected_kind=expected_kind,
                    input_event=input_event,
                )
            raise
        current = self.store.get_run(run_id)
        # Pause/cancel bypass the advancement lock (they must be able to abort a
        # running turn), so the run may have left this phase while the model was
        # working. Refuse to let the caller drive the next transition on a
        # stalled run; recovery resumes at the correct phase instead.
        if current.state == RunState.PAUSED or current.state in TERMINAL_STATES:
            raise WorkflowError(
                'workflow advancement stopped after agent turn because run is '
                f'{current.state.value}'
            )
        return completed, result

    def _record_requested_actions(
        self,
        *,
        run_id: str,
        agent: AgentName,
        result: AgentTurnResult,
        turn_number: int,
    ) -> list[ActionRecord]:
        records: list[ActionRecord] = []
        for index, requested in enumerate(result.requested_actions):
            record = self.policy.build_record(
                run_id=run_id,
                proposed_by=agent,
                action=requested,
                ordinal=turn_number * 100 + index,
            )
            payload = self._approval_event_payload(record)
            record, created = self.store.save_action_with_event(
                record, source=agent.value, payload=payload,
            )
            records.append(record)
            if created:
                self._publish_latest(run_id)
            else:
                self._repair_action_proposed_event(
                    record, source=agent.value,
                    payload=self._approval_event_payload(record),
                )
        return records

    def _approval_event_payload(
        self,
        action: ActionRecord,
        *,
        human_approval_ready: bool | None = None,
    ) -> dict[str, Any]:
        run = self.store.get_run(action.run_id)
        if human_approval_ready is None:
            human_approval_ready = (
                action.approval_status == ApprovalStatus.PENDING
                and (
                    action.policy_classification
                    != PolicyClassification.HONEYDEW_AND_HUMAN_APPROVAL
                    or action.honeydew_approved
                )
            )
        artifacts = self.store.list_artifacts(action.run_id)
        relevant_artifact = next(
            (
                artifact
                for artifact in reversed(artifacts)
                if (
                    action.type == 'approve_protocol'
                    and artifact.type == 'protocol'
                )
                or (
                    action.type == 'accept_final_report'
                    and artifact.type == 'report'
                )
            ),
            None,
        )
        contract_proposal_artifact = next(
            (
                artifact
                for artifact in reversed(artifacts)
                if artifact.type == 'evaluation_contract_proposal'
            ),
            None,
        )
        effects = {
            'approve_protocol': (
                'Authorize Beaker to implement against this frozen protocol. '
                'This does not authorize a cluster job.'
            ),
            'submit_experiment_matrix': (
                'Authorize the orchestrator to validate, expand, and submit '
                'this experiment matrix under the recorded resource limits.'
            ),
            'accept_final_report': (
                'Accept the report as the final output and mark this research '
                'run complete. This does not publish it externally.'
            ),
            'propose_evaluation_contract': (
                'Promote the sealed, Honeydew-reviewed evaluation contract '
                'into the trusted catalog and bind this run to its exact digest. '
                'This does not authorize a cluster job.'
            ),
        }
        payload: dict[str, Any] = {
            'action_id': action.action_id,
            'type': action.type,
            'policy_classification': action.policy_classification.value,
            'approval_status': action.approval_status.value,
            'human_approval_ready': human_approval_ready,
            'honeydew_approved': action.honeydew_approved,
            'objective': run.objective,
            'reason': action.reason,
            'effect': effects.get(
                action.type,
                'Authorize the stored action under its deterministic policy.',
            ),
            'arguments': action.arguments,
            'evaluation_contract': {
                'contract_id': run.evaluation_contract_id,
                'version': run.evaluation_contract_version,
                'digest': run.evaluation_contract_digest,
            },
        }
        if relevant_artifact is not None:
            payload['artifact'] = {
                'type': relevant_artifact.type,
                'uri': relevant_artifact.uri,
                'sha256': relevant_artifact.sha256,
            }
        if contract_proposal_artifact is not None:
            payload['contract_proposal'] = (
                contract_proposal_artifact.metadata.get('proposal')
            )
            payload['contract_binding'] = (
                contract_proposal_artifact.metadata.get('technical_binding')
            )
        if action.type == 'propose_evaluation_contract':
            candidate = next(
                (
                    artifact
                    for artifact in reversed(artifacts)
                    if artifact.type == 'evaluation_contract_candidate'
                    and artifact.metadata.get('action_id') == action.action_id
                ),
                None,
            )
            if candidate is not None:
                payload['artifact'] = {
                    'type': candidate.type,
                    'uri': candidate.uri,
                    'sha256': candidate.sha256,
                }
                payload['contract_candidate'] = candidate.metadata
        if action.type == 'submit_experiment_matrix':
            try:
                payload['preflight'] = self._matrix_preflight_report(
                    run_id=action.run_id,
                    action=action,
                ).model_dump(mode='json')
            except Exception as exc:
                payload['preflight'] = {
                    'passed': False,
                    'job_count': 0,
                    'checks': [],
                    'comparisons': {},
                    'decisions': {},
                    'errors': [str(exc)],
                }
        if action.type == 'approve_protocol':
            payload['protocol_version'] = run.protocol_version
        return payload

    def _create_human_action(
        self,
        *,
        run_id: str,
        action_type: str,
        reason: str,
    ) -> ActionRecord:
        run = self.store.get_run(run_id)
        requested = RequestedAction(
            type=action_type,
            arguments={},
            reason=reason,
        )
        candidate = self.policy.build_record(
            run_id=run_id,
            proposed_by=AgentName.ORCHESTRATOR,
            action=requested,
            ordinal=run.turn_number * 1000 + len(self.store.list_actions(run_id)),
        )
        payload = self._approval_event_payload(candidate)
        record, created = self.store.save_action_with_event(
            candidate, source='orchestrator', payload=payload,
        )
        if created:
            self._publish_latest(run_id)
        else:
            self._repair_action_proposed_event(
                record, source='orchestrator',
                payload=self._approval_event_payload(record),
            )
        return record

    def _repair_action_proposed_event(
        self, action: ActionRecord, *, source: str, payload: dict[str, Any],
    ) -> None:
        if any(
            event.event_type == 'action.proposed'
            and event.payload.get('action_id') == action.action_id
            for event in self.store.list_events(action.run_id)
        ):
            return
        self._event(
            action.run_id, source=source, event_type='action.proposed',
            payload=payload,
        )

    def retry_terminal_run(
        self, parent_run_id: str, request: TerminalRetryRequest,
    ) -> RunRecord:
        """Create one fresh child from a sealed terminal protocol checkpoint."""
        with self._advance_lock:
            parent = self.store.get_run(parent_run_id)
            if parent.state not in {RunState.FAILED, RunState.TIMED_OUT}:
                raise WorkflowError('only FAILED or TIMED_OUT runs can be retried')
            existing_child = self.store.get_terminal_retry_child(parent_run_id)
            if existing_child is not None and existing_child.state not in TERMINAL_STATES:
                return existing_child
            source_protocol = Path(parent.protocol_path or '')
            if not source_protocol.is_file() or source_protocol.is_symlink():
                raise WorkflowError('terminal retry protocol checkpoint is unavailable')
            source_digest = sha256(source_protocol.read_bytes()).hexdigest()
            protocol_artifacts = [
                artifact for artifact in self.store.list_artifacts(parent_run_id)
                if artifact.type == 'protocol'
                and artifact.sha256 == source_digest
                and artifact.metadata.get('path') == str(source_protocol)
            ]
            if len(protocol_artifacts) != 1:
                raise WorkflowError('terminal retry requires the current approved protocol artifact')
            approved_protocol_actions = {
                action.action_id
                for action in self.store.list_actions(parent_run_id)
                if action.type == 'approve_protocol'
                and action.approval_status == ApprovalStatus.APPROVED
            }
            if not any(
                event.event_type == 'action.proposed'
                and event.payload.get('action_id') in approved_protocol_actions
                and event.payload.get('protocol_version') == parent.protocol_version
                and isinstance(event.payload.get('artifact'), dict)
                and event.payload['artifact'].get('sha256') == source_digest
                for event in self.store.list_events(parent_run_id)
            ):
                raise WorkflowError('terminal retry requires a human-approved protocol checkpoint')
            protocol = protocol_artifacts[0]
            if source_digest != protocol.sha256:
                raise WorkflowError('terminal retry protocol checksum mismatch')
            # Resolve again; a record of a contract is not proof that the
            # immutable contract still exists or has the same content.
            contract = self.contracts.resolve(
                parent.evaluation_contract_id, parent.evaluation_contract_version,
            )
            if contract.digest != parent.evaluation_contract_digest:
                raise WorkflowError('terminal retry evaluation contract checksum mismatch')
            task = None
            task_binding = None
            if parent.task_id:
                task = self.task_bundles.get(parent.task_id, parent.task_bundle_digest)
                if (
                    task.digest != parent.task_bundle_digest
                    or task.task_id != parent.task_id
                ):
                    raise WorkflowError('terminal retry task binding checksum mismatch')
                preflight = self.task_preflight(task)
                if not preflight.ready:
                    raise WorkflowError(
                        'terminal retry task preflight failed: '
                        + '; '.join(preflight.blocking_issues)
                    )
                task_binding = task.model_dump(mode='json')
            if not parent.workspace_base_commit:
                raise WorkflowError(
                    'terminal retry requires a durably recorded workspace base commit'
                )
            parent_base_commit = parent.workspace_base_commit
            child_id = uuid4().hex
            paths = self.workspaces.prepare(child_id, repo_ref=parent_base_commit)
            if task:
                self.workspaces.install_task_bundle(
                    run_id=child_id, problem_path=task.problem_path,
                    evaluator_prompt_path=task.evaluator_prompt_path,
                )
            manifest_path, checkpoint_digest = self.workspaces.create_terminal_retry_checkpoint(
                parent_run_id=parent_run_id,
                child_run_id=child_id,
                protocol_digest=protocol.sha256,
                contract={'contract_id': parent.evaluation_contract_id, 'version': parent.evaluation_contract_version, 'digest': parent.evaluation_contract_digest},
                task_binding=task_binding,
                base_commit=parent_base_commit,
            )
            if request.idempotency_key:
                retry_key = request.idempotency_key
            else:
                attempt = f'{parent_run_id}:{checkpoint_digest}'
                if existing_child is not None:
                    attempt = f'{attempt}:{existing_child.run_id}'
                retry_key = sha256(f'terminal-retry-v1:{attempt}'.encode()).hexdigest()
            now = utc_now()
            child = RunRecord(
                run_id=child_id, parent_run_id=parent_run_id,
                retry_checkpoint_digest=checkpoint_digest, objective=parent.objective,
                workspace_base_commit=parent_base_commit,
                state=RunState.PREPARING, protocol_path=str(paths.protocol / 'program.md'),
                protocol_version=parent.protocol_version,
                evaluation_contract_id=parent.evaluation_contract_id,
                evaluation_contract_version=parent.evaluation_contract_version,
                evaluation_contract_digest=parent.evaluation_contract_digest,
                task_id=task.task_id if task else None,
                task_bundle_digest=task.digest if task else None,
                task_bundle_path=task.archive_path if task else None,
                task_definition=task_binding,
                beaker_workspace=str(paths.beaker), honeydew_workspace=str(paths.honeydew),
                shared_artifacts_path=str(paths.shared_artifacts), reports_path=str(paths.reports),
                maximum_turns=self.settings.maximum_turns,
                maximum_runtime_seconds=self.settings.maximum_runtime_seconds,
                maximum_parallel_jobs=self.settings.maximum_parallel_jobs,
                active_since=now, created_at=now, updated_at=now,
            )
            child, created = self.store.create_terminal_retry(
                child, parent_run_id=parent_run_id, retry_key=retry_key,
                checkpoint_digest=checkpoint_digest, one_active_run=self.settings.one_active_run,
            )
            if not created:
                return child
            self._attach_discord_thread(child.run_id)
            self._event(child.run_id, source='orchestrator', event_type='retry.checkpoint_recorded', payload={'path': str(manifest_path), 'sha256': checkpoint_digest, 'parent_run_id': parent_run_id})
            try:
                self._resume_terminal_retry(child.run_id)
            except Exception as exc:
                self._fail_run(child.run_id, exc)
                raise
            # Parent may have an existing thread; this event is durable first
            # and Discord remains a best-effort projection.
            self._publish_latest(parent_run_id)
            return self.store.get_run(child.run_id)

    def _resume_terminal_retry(self, run_id: str) -> None:
        run = self.store.get_run(run_id)
        if run.state != RunState.PREPARING or not run.parent_run_id or not run.retry_checkpoint_digest:
            return
        manifest = self.workspaces.verify_terminal_retry_checkpoint(run_id, run.retry_checkpoint_digest)
        contract = manifest.get('contract', {})
        if contract != {'contract_id': run.evaluation_contract_id, 'version': run.evaluation_contract_version, 'digest': run.evaluation_contract_digest}:
            raise WorkflowError('retry checkpoint contract binding is invalid')
        if manifest.get('base_commit') != run.workspace_base_commit:
            raise WorkflowError('retry checkpoint base commit is invalid')
        if run.task_id:
            task = self.task_bundles.get(run.task_id, run.task_bundle_digest)
            preflight = self.task_preflight(task)
            if not preflight.ready:
                raise WorkflowError(
                    'retry task preflight failed: '
                    + '; '.join(preflight.blocking_issues)
                )
            if (
                manifest.get('task_binding') != task.model_dump(mode='json')
                or run.task_definition != task.model_dump(mode='json')
                or run.task_bundle_path != task.archive_path
            ):
                raise WorkflowError('retry task binding no longer matches checkpoint')
        elif manifest.get('task_binding') is not None:
            raise WorkflowError('retry checkpoint unexpectedly contains a task binding')
        descriptor = self.contracts.resolve(run.evaluation_contract_id, run.evaluation_contract_version)
        if descriptor.digest != run.evaluation_contract_digest:
            raise WorkflowError('retry contract digest no longer matches')
        # The original contract-promotion decision is not inherited as an
        # approval.  Its already-promoted immutable contract is re-projected
        # deterministically so the child's fresh protocol approval can verify
        # the same binding without trusting an old mutable proposal artifact.
        proposal_payload = self._proposal_from_bound_contract(
            descriptor.descriptor
        ).model_dump(mode='json')
        proposal_path = (
            Path(run.shared_artifacts_path) / 'evaluation-contract-proposal.json'
        )
        proposal_path.write_bytes(
            (json.dumps(proposal_payload, indent=2, sort_keys=True) + '\n').encode()
        )
        proposal_digest = sha256(proposal_path.read_bytes()).hexdigest()
        self._save_local_artifact(
            run_id=run_id, artifact_type='evaluation_contract_proposal',
            uri=(f'artifact://{run_id}/shared-artifacts/'
                 'evaluation-contract-proposal.json'),
            digest=proposal_digest,
            metadata={
                'path': str(proposal_path), 'proposal': proposal_payload,
                'origin': 'terminal_retry_bound_contract',
                'technical_binding': {
                    'status': 'compatible',
                    'contract_id': run.evaluation_contract_id,
                    'version': run.evaluation_contract_version,
                    'digest': run.evaluation_contract_digest,
                },
            },
        )
        self._save_local_artifact(
            run_id=run_id, artifact_type='protocol',
            uri=f'artifact://{run_id}/protocol/program.md',
            digest=str(manifest['protocol']['sha256']),
            metadata={'path': str(self.workspaces.paths(run_id).protocol / 'program.md'), 'parent_run_id': run.parent_run_id, 'retry_checkpoint_digest': run.retry_checkpoint_digest},
        )
        existing_protocol_action = next(
            (
                action for action in self.store.list_actions(run_id)
                if action.type == 'approve_protocol'
            ),
            None,
        )
        if existing_protocol_action is None:
            self._create_human_action(
                run_id=run_id, action_type='approve_protocol',
                reason='A terminal retry reuses a verified protocol but requires fresh human approval.',
            )
        else:
            self._repair_action_proposed_event(
                existing_protocol_action,
                source='orchestrator',
                payload=self._approval_event_payload(existing_protocol_action),
            )
        self._transition(run_id, RunState.AWAITING_PROTOCOL_APPROVAL, payload={'retry_parent_run_id': run.parent_run_id})

    def _save_local_artifact(
        self,
        *,
        run_id: str,
        artifact_type: str,
        uri: str,
        digest: str,
        metadata: dict[str, Any],
    ) -> ArtifactRecord:
        # The artifact id is a deterministic function of (run, uri, digest), not
        # random: re-delivering the same content re-derives the same id, which
        # makes recovery backfill idempotent (storage uses INSERT OR IGNORE).
        artifact = ArtifactRecord(
            artifact_id=uuid5(
                NAMESPACE_URL,
                f'{run_id}:{uri}:{digest}',
            ).hex,
            run_id=run_id,
            type=artifact_type,
            uri=uri,
            sha256=digest,
            metadata=metadata,
        )
        return self.store.save_artifact(artifact)

    def _latest_contract_proposal(
        self,
        run_id: str,
    ) -> dict[str, Any] | None:
        matches = [
            artifact
            for artifact in self.store.list_artifacts(run_id)
            if artifact.type == 'evaluation_contract_proposal'
        ]
        if not matches:
            return None
        proposal = matches[-1].metadata.get('proposal')
        return proposal if isinstance(proposal, dict) else None

    def _contract_binding_compatible(self, run_id: str) -> bool:
        # Re-derives the Honeydew proposal from the stored artifact (not from
        # memory) and checks it against the currently installed contract. The
        # digest equality pins the check to the exact contract the run was bound
        # to, so this is also what forces a promoted candidate to match before
        # execution. Missing resource keys default to sentinel values that FAIL
        # the check (the proposal must be explicit about limits).
        run = self.store.get_run(run_id)
        proposal = self._latest_contract_proposal(run_id)
        if proposal is None:
            return False
        try:
            contract = self.contracts.resolve(
                run.evaluation_contract_id,
                run.evaluation_contract_version,
            )
        except ValueError:
            return False
        descriptor = contract.descriptor
        primary = proposal.get('primary_metric')
        resources = proposal.get('resource_constraints')
        required_artifacts = proposal.get('required_artifacts')
        if not isinstance(primary, dict) or not isinstance(resources, dict):
            return False
        if not isinstance(required_artifacts, list):
            return False
        ceiling = descriptor.resource_constraints
        allowed_artifacts = set(descriptor.required_artifacts)
        if (
            descriptor.contract_id == 'generic-task-integrity-v1'
            and run.task_definition
        ):
            allowed_artifacts.update(
                str(item)
                for item in run.task_definition.get('required_artifacts', [])
            )
        return bool(
            contract.digest == run.evaluation_contract_digest
            and proposal.get('evaluator_type') == descriptor.contract_id
            and primary.get('name') == descriptor.manifest.get('primary_metric')
            and primary.get('direction')
            == descriptor.manifest.get('primary_metric_direction')
            and set(str(item) for item in required_artifacts).issubset(
                allowed_artifacts
            )
            and float(resources.get('cpu', float('inf'))) <= ceiling.cpu
            and float(resources.get('memory_gib', float('inf')))
            <= ceiling.memory_gib
            and int(resources.get('gpus', self.policy.maximum_gpus + 1))
            <= ceiling.gpus
            and int(
                resources.get(
                    'wallclock_minutes',
                    ceiling.wallclock_minutes + 1,
                )
            )
            <= ceiling.wallclock_minutes
        )

    def _draft_protocol(self, run_id: str, feedback: str | None = None) -> None:
        run = self.store.get_run(run_id)
        task_context = ''
        if run.task_definition:
            bound = self.contracts.resolve(
                run.evaluation_contract_id,
                run.evaluation_contract_version,
            )
            generic_task = (
                run.task_definition.get('compilation_source')
                == 'honeydew-task-spec'
            )
            task_context = (
                '\n\nThis is an imported research task. Read the immutable '
                '`benchmark-task/problem.md` and '
                '`benchmark-task/eval_agent_prompt.md` files. Treat the task '
                'requirements and evaluator rubric as binding source material. '
                + (
                    'The generic integrity evaluator is a baseline. Use it '
                    'only if artifact and metric-key checks are scientifically '
                    'sufficient; otherwise propose the task-specific evaluator '
                    'that Beaker should implement and Honeydew should review. '
                    if generic_task
                    else
                    'Do not redesign the evaluator contract or request a new '
                    'harness; the repository-controlled contract is already '
                    'bound to this legacy task. Make '
                    'evaluation_contract_proposal exactly match this installed '
                    'descriptor. '
                )
                + '\nInstalled descriptor:\n'
                + json.dumps(
                    bound.descriptor.model_dump(mode='json'),
                    indent=2,
                    sort_keys=True,
                )
            )
        prompt = (
            'Draft a concrete program.md for this objective:\n\n'
            f'{run.objective}\n\n'
            f'Evaluation contract: contract://{run.evaluation_contract_id}/'
            f'{run.evaluation_contract_version}@{run.evaluation_contract_digest}\n'
            'Write program.md in your workspace. Include hypotheses, independent '
            'and dependent variables, controls, baselines, source references, '
            'required artifacts, evaluation criteria, budgets, stopping '
            'conditions, and explicit approval gates. Return it as a produced '
            'file with purpose "protocol". The produced_files metadata does not '
            'create the file: use OpenCode\'s workspace file tool to write '
            '`program.md`, then read it back before returning. Also populate '
            'evaluation_contract_proposal with the scientific evaluator type, '
            'primary metric and direction, minimum meaningful effect, '
            'guardrails, required artifacts, budget policy, resource ceilings, '
            'and rationale. Propose data and metrics only. Do not propose '
            'executable paths, container images, commands, or checksums; those '
            'remain controlled by the orchestrator. The experiment workload '
            'must emit metrics and evidence only. `evaluation.json`, '
            '`rubric_score`, and `integrity_pass` are evaluator-owned outputs: '
            'the protocol may require them as final artifacts, but must not '
            'instruct Beaker or workload code to create, format, read, or score '
            'them. When the installed contract declares methodology '
            'requirements, only entries with mode `comparison` are experiment '
            'axes. Entries with mode `decision` require one justified choice; '
            'do not invent an ablation or improvement hypothesis for them.'
            + task_context
        )
        if feedback:
            prompt += f'\n\nHuman rejection feedback:\n{feedback}'
        turn, result = self._run_agent_turn(
            run_id=run_id,
            agent=AgentName.HONEYDEW,
            prompt=prompt,
            expected_kind=TurnKind.PROTOCOL_DRAFT,
            input_event={'objective': run.objective, 'feedback': feedback},
        )
        protocol_files = [
            item for item in result.produced_files if item.purpose == 'protocol'
        ]
        if not protocol_files:
            # Honeydew returned a formally valid protocol_draft turn but declared
            # no protocol file at all. This is a contract violation distinct from
            # a declared-but-missing file (handled below): the workspace action
            # was never attempted, so the repair must name the exact file and
            # purpose and revalidate before the run may advance.
            self._event(
                run_id,
                source='orchestrator',
                event_type='agent.output_rejected',
                payload={
                    'turn_id': turn.turn_id,
                    'expected_field': 'produced_files',
                    'expected_purpose': 'protocol',
                    'repair_attempted': True,
                },
            )
            _, result = self._run_agent_turn(
                run_id=run_id,
                agent=AgentName.HONEYDEW,
                prompt=(
                    'Focused protocol-file repair. Your previous protocol_draft '
                    'turn did not declare any produced protocol file. Write '
                    '`program.md` in your workspace now, then read it back to '
                    'confirm it exists. Declare exactly that file in '
                    '`produced_files` with purpose `protocol` and request no '
                    'actions. Do not redesign the protocol. Return kind '
                    '`protocol_draft` only after the file is written and '
                    'verified.'
                ),
                expected_kind=TurnKind.PROTOCOL_DRAFT,
                input_event={
                    'repair': 'missing_protocol_declaration',
                    'required_path': 'program.md',
                    'required_purpose': 'protocol',
                },
            )
            protocol_files = [
                item
                for item in result.produced_files
                if item.purpose == 'protocol'
            ]
            if result.requested_actions:
                raise WorkflowError(
                    'Honeydew protocol-file repair must request no actions'
                )
        if len(protocol_files) != 1:
            # Exactly one declared protocol file keeps the artifact binding
            # unambiguous; a declared-but-missing file is repaired below rather
            # than silently accepted from the structured response.
            raise WorkflowError('Honeydew must produce exactly one protocol file')
        protocol_path = Path(run.honeydew_workspace) / protocol_files[0].path
        if not protocol_path.is_file():
            self._event(
                run_id,
                source='orchestrator',
                event_type='agent.file_repair_requested',
                payload={
                    'agent': AgentName.HONEYDEW.value,
                    'path': protocol_files[0].path,
                    'reason': (
                        'The structured response declared the protocol, but no '
                        'workspace file existed.'
                    ),
                },
            )
            _, repaired = self._run_agent_turn(
                run_id=run_id,
                agent=AgentName.HONEYDEW,
                prompt=(
                    'Focused workspace repair. Your previous structured response '
                    f'declared `{protocol_files[0].path}`, but the authoritative '
                    'workspace does not contain that file. Write it now using '
                    'OpenCode\'s workspace file tool, then read it back. Do not '
                    'redesign the protocol or request actions. Return kind '
                    '`protocol_draft`, declare exactly that file with purpose '
                    '`protocol`, and only claim completion after verifying the '
                    'file exists.'
                ),
                expected_kind=TurnKind.PROTOCOL_DRAFT,
                input_event={
                    'repair': 'missing_workspace_file',
                    'path': protocol_files[0].path,
                },
            )
            repaired_protocols = [
                item
                for item in repaired.produced_files
                if item.path == protocol_files[0].path
                and item.purpose == 'protocol'
            ]
            if len(repaired_protocols) != 1 or repaired.requested_actions:
                raise WorkflowError(
                    'Honeydew protocol repair must produce the declared file '
                    'and no actions'
                )
            if not protocol_path.is_file():
                raise WorkflowError(
                    'Honeydew did not create the protocol after one focused '
                    'repair turn'
                )
            self._event(
                run_id,
                source='honeydew',
                event_type='agent.file_repair_completed',
                payload={'path': protocol_files[0].path},
            )
        resolved_contract = self.contracts.resolve(
            run.evaluation_contract_id,
            run.evaluation_contract_version,
        )
        descriptor = resolved_contract.descriptor
        proposal = result.evaluation_contract_proposal
        proposal_turn_id = turn.turn_id
        proposal_origin = 'honeydew'
        legacy_bound_task = bool(
            run.task_definition
            and run.task_definition.get('compilation_source') == 'legacy'
        )
        if proposal is None and legacy_bound_task:
            proposal = self._proposal_from_bound_contract(descriptor)
            proposal_origin = 'installed_contract'
            self._event(
                run_id,
                source='orchestrator',
                event_type='contract.proposal_bound',
                payload={
                    'turn_id': turn.turn_id,
                    'contract_id': descriptor.contract_id,
                    'contract_version': descriptor.version,
                    'reason': (
                        'The imported legacy task already has an immutable '
                        'repository-controlled evaluation contract.'
                    ),
                },
            )
        elif proposal is None:
            self._event(
                run_id,
                source='orchestrator',
                event_type='agent.output_rejected',
                payload={
                    'turn_id': turn.turn_id,
                    'expected_field': 'evaluation_contract_proposal',
                    'repair_attempted': True,
                },
            )
            proposal_turn, repaired = self._run_agent_turn(
                run_id=run_id,
                agent=AgentName.HONEYDEW,
                prompt=(
                    'Focused structured-output repair. Your protocol was '
                    'written successfully, but the prior response omitted '
                    '`evaluation_contract_proposal`. Do not edit program.md '
                    'or request actions. Return kind `protocol_draft` and '
                    'populate evaluation_contract_proposal with the scientific '
                    'evaluator type, primary metric and direction, minimum '
                    'meaningful effect, guardrails, required artifacts, budget '
                    'policy, resource ceilings, and rationale. Do not include '
                    'executable paths, commands, images, or checksums.'
                ),
                expected_kind=TurnKind.PROTOCOL_DRAFT,
                input_event={
                    'repair': 'missing_evaluation_contract_proposal',
                    'protocol_turn_id': turn.turn_id,
                },
            )
            proposal = repaired.evaluation_contract_proposal
            if proposal is None or repaired.requested_actions:
                raise WorkflowError(
                    'Honeydew contract-proposal repair did not return the '
                    'required bounded proposal'
                )
            proposal_turn_id = proposal_turn.turn_id
            self._event(
                run_id,
                source='honeydew',
                event_type='agent.output_repaired',
                payload={
                    'turn_id': proposal_turn.turn_id,
                    'field': 'evaluation_contract_proposal',
                },
            )
        proposal_resources = proposal.resource_constraints
        if (
            proposal_resources.cpu > self.policy.maximum_cpu
            or proposal_resources.memory_gib > self.policy.maximum_memory_gib
            or proposal_resources.gpus > self.policy.maximum_gpus
        ):
            raise WorkflowError(
                'Honeydew contract proposal exceeds orchestrator resource ceilings'
            )
        destination, digest = self.workspaces.copy_agent_output(
            run_id=run_id,
            agent=AgentName.HONEYDEW,
            relative_path=protocol_files[0].path,
            destination_kind='protocol',
        )
        current = self.store.get_run(run_id)
        self.store.replace_run(
            current.model_copy(
                update={
                    'protocol_path': str(destination),
                    'protocol_version': current.protocol_version + 1,
                }
            ),
            expected_version=current.version,
        )
        uri = f'artifact://{run_id}/protocol/program.md'
        self._save_local_artifact(
            run_id=run_id,
            artifact_type='protocol',
            uri=uri,
            digest=digest,
            metadata={
                'path': str(destination),
                'protocol_version': current.protocol_version + 1,
                'turn_id': turn.turn_id,
            },
        )
        self._event(
            run_id,
            source='honeydew',
            event_type='artifact.recorded',
            payload={
                'type': 'protocol',
                'uri': uri,
                'path': str(destination),
                'sha256': digest,
                'turn_id': turn.turn_id,
                'protocol_version': current.protocol_version + 1,
            },
        )
        technical_primary_metric = str(
            descriptor.manifest.get('primary_metric', '')
        )
        ceiling = descriptor.resource_constraints
        allowed_artifacts = set(descriptor.required_artifacts)
        if (
            descriptor.contract_id == 'generic-task-integrity-v1'
            and run.task_definition
        ):
            allowed_artifacts.update(
                str(item)
                for item in run.task_definition.get('required_artifacts', [])
            )
        binding_compatible = (
            proposal.evaluator_type == descriptor.contract_id
            and proposal.primary_metric.name == technical_primary_metric
            and proposal.primary_metric.direction
            == descriptor.manifest.get('primary_metric_direction')
            and set(proposal.required_artifacts).issubset(
                allowed_artifacts
            )
            and proposal_resources.cpu <= ceiling.cpu
            and proposal_resources.memory_gib <= ceiling.memory_gib
            and proposal_resources.gpus <= ceiling.gpus
            and proposal_resources.wallclock_minutes
            <= ceiling.wallclock_minutes
        )
        proposal_payload = proposal.model_dump(mode='json')
        proposal_path = (
            Path(run.shared_artifacts_path)
            / 'evaluation-contract-proposal.json'
        )
        proposal_path.parent.mkdir(parents=True, exist_ok=True)
        # Persist the proposal next to the worktrees so the approval UI and
        # later routing (see _contract_binding_compatible) read the same bytes
        # that were recorded as an artifact, instead of trusting agent memory.
        proposal_bytes = (
            json.dumps(proposal_payload, indent=2, sort_keys=True) + '\n'
        ).encode()
        proposal_path.write_bytes(proposal_bytes)
        proposal_digest = sha256(proposal_bytes).hexdigest()
        proposal_uri = (
            f'artifact://{run_id}/shared-artifacts/'
            'evaluation-contract-proposal.json'
        )
        technical_binding = {
            'status': (
                'compatible'
                if binding_compatible
                else 'requires_new_harness'
            ),
            'contract_id': run.evaluation_contract_id,
            'version': run.evaluation_contract_version,
            'digest': run.evaluation_contract_digest,
        }
        self._save_local_artifact(
            run_id=run_id,
            artifact_type='evaluation_contract_proposal',
            uri=proposal_uri,
            digest=proposal_digest,
            metadata={
                'path': str(proposal_path),
                'turn_id': proposal_turn_id,
                'origin': proposal_origin,
                'proposal': proposal_payload,
                'technical_binding': technical_binding,
            },
        )
        self._event(
            run_id,
            source='honeydew',
            event_type='artifact.recorded',
            payload={
                'type': 'evaluation_contract_proposal',
                'uri': proposal_uri,
                'path': str(proposal_path),
                'sha256': proposal_digest,
                'turn_id': proposal_turn_id,
                'origin': proposal_origin,
                'proposal': proposal_payload,
                'technical_binding': technical_binding,
            },
        )
        self._create_human_action(
            run_id=run_id,
            action_type='approve_protocol',
            reason='Human approval is required before implementation.',
        )
        self._transition(run_id, RunState.AWAITING_PROTOCOL_APPROVAL)

    @staticmethod
    def _proposal_from_bound_contract(
        descriptor: EvaluationContractDescriptor,
    ) -> EvaluationContractProposal:
        """Project an immutable legacy contract into the scientific proposal."""
        resources = descriptor.resource_constraints
        return EvaluationContractProposal.model_validate(
            {
                'evaluator_type': descriptor.contract_id,
                'primary_metric': {
                    'name': descriptor.manifest['primary_metric'],
                    'direction': descriptor.manifest[
                        'primary_metric_direction'
                    ],
                    'minimum_effect': 0.0,
                },
                'guardrails': [],
                'required_artifacts': descriptor.required_artifacts,
                'budget_mode': 'wallclock',
                'max_wallclock_minutes': resources.wallclock_minutes,
                'resource_constraints': resources.model_dump(mode='json'),
                'rationale': (
                    'This imported task is bound to an immutable '
                    'repository-controlled evaluation contract.'
                ),
            }
        )

    def approve_action(
        self,
        action_id: str,
        *,
        reviewer: str,
        reason: str,
    ) -> ActionRecord:
        with self._advance_lock:
            action = self.store.get_action(action_id)
            if action.approval_status == ApprovalStatus.APPROVED:
                # Re-approval is the crash-recovery retry: the first approval may
                # have committed the action but died before (or during) resume.
                # Re-running resume is safe because _resume_approved_action
                # re-checks the run state and becomes a no-op once the run has
                # moved past the awaiting state.
                self._resume_approved_action(action)
                return self.store.get_action(action_id)
            if action.approval_status != ApprovalStatus.PENDING:
                raise WorkflowError(
                    f'action is not pending: {action.approval_status.value}'
                )
            if (
                action.policy_classification
                == PolicyClassification.HONEYDEW_AND_HUMAN_APPROVAL
                and not action.honeydew_approved
            ):
                raise WorkflowError('Honeydew has not approved this action')
            approved = self.store.update_action(
                action_id,
                approval_status=ApprovalStatus.APPROVED,
                reviewer=reviewer,
                reason=reason,
            )
            self._event(
                action.run_id,
                source='orchestrator',
                event_type='action.approved',
                payload={'action_id': action_id, 'reviewer': reviewer},
            )
            try:
                self._resume_approved_action(approved)
            except Exception as exc:
                self._handle_approved_action_failure(approved, exc)
                raise
            return approved

    def _handle_approved_action_failure(
        self,
        action: ActionRecord,
        exc: Exception,
    ) -> None:
        jobs = [
            job
            for job in self.store.list_jobs(action.run_id)
            if job.action_id == action.action_id
        ]
        # A deterministic matrix failure (ValueError from expansion, no jobs
        # created yet) is the one failure the workflow can recover from without
        # a human: it is a defect in the proposed matrix, so Beaker revises.
        # Everything else pauses the run so the human can reconcile any partial
        # job/artifact state before deciding what happens next.
        deterministic_matrix_failure = (
            action.type == 'submit_experiment_matrix'
            and isinstance(exc, ValueError)
            and not jobs
        )
        resulting_state = (
            RunState.BEAKER_REVISING
            if deterministic_matrix_failure
            else RunState.PAUSED
        )
        if deterministic_matrix_failure:
            self.store.mark_action_execution_failed(
                action.action_id,
                reason=f'Approved action could not execute: {exc}',
            )
        current = self.store.get_run(action.run_id)
        self._event(
            action.run_id,
            source='orchestrator',
            event_type='action.execution_failed',
            payload={
                'action_id': action.action_id,
                'type': action.type,
                'objective': current.objective,
                'error': str(exc),
                'jobs_created': len(jobs),
                'artifacts_created': len(
                    [
                        artifact
                        for artifact in self.store.list_artifacts(action.run_id)
                        if artifact.job_id in {job.job_id for job in jobs}
                    ]
                ),
                'retryable': not deterministic_matrix_failure,
                'resulting_state': resulting_state.value,
                'next_step': (
                    'Beaker will revise the matrix before another approval.'
                    if deterministic_matrix_failure
                    else (
                        'The run is paused. Reconcile any recorded jobs, then '
                        'resume to retry the authoritative action.'
                    )
                ),
            },
        )
        if current.state in TERMINAL_STATES or current.state == RunState.PAUSED:
            return
        if deterministic_matrix_failure:
            self._transition(action.run_id, RunState.BEAKER_REVISING)
            self._beaker_revise(
                action.run_id,
                feedback=(
                    'The approved matrix failed deterministic execution '
                    f'validation: {exc}'
                ),
            )
            return
        self.pause_run(action.run_id)

    def _resume_approved_action(self, action: ActionRecord) -> None:
        run = self.store.get_run(action.run_id)
        # Every branch is gated on the run state, so a stale or duplicate
        # approval click is a silent no-op once the run has already advanced;
        # this is what makes re-approval safe as a recovery retry.
        if action.type == 'approve_protocol':
            if run.state != RunState.AWAITING_PROTOCOL_APPROVAL:
                return
            self.workspaces.freeze_protocol(action.run_id)
            if self._contract_binding_compatible(action.run_id):
                self._transition(action.run_id, RunState.BEAKER_PLANNING)
                self._beaker_plan(action.run_id)
            else:
                self._transition(
                    action.run_id,
                    RunState.BEAKER_DRAFTING_CONTRACT,
                )
                self._beaker_draft_contract(action.run_id)
        elif action.type == 'propose_evaluation_contract':
            if run.state != RunState.AWAITING_CONTRACT_PROMOTION:
                return
            self._promote_contract_candidate(action)
        elif action.type == 'submit_experiment_matrix':
            if run.state != RunState.AWAITING_EXECUTION_APPROVAL:
                return
            self._submit_matrix(action)
        elif action.type == 'accept_final_report':
            if run.state != RunState.AWAITING_FINAL_ACCEPTANCE:
                return
            self._transition(action.run_id, RunState.COMPLETE)
            self._event(
                action.run_id,
                source='orchestrator',
                event_type='run.completed',
                payload={'accepted_by': action.reviewer},
            )

    def reject_action(
        self,
        action_id: str,
        *,
        reviewer: str,
        reason: str,
    ) -> ActionRecord:
        with self._advance_lock:
            existing = self.store.get_action(action_id)
            if existing.approval_status == ApprovalStatus.REJECTED:
                self._event(
                    existing.run_id,
                    source='orchestrator',
                    event_type='action.rejection_resumed',
                    payload={
                        'action_id': action_id,
                        'reviewer': reviewer,
                        'reason': reason,
                    },
                )
                self._resume_rejected_action(existing, feedback=reason)
                return self.store.get_action(action_id)
            if existing.approval_status != ApprovalStatus.PENDING:
                raise WorkflowError(
                    f'action is not pending: {existing.approval_status.value}'
                )
            action = self.store.update_action(
                action_id,
                approval_status=ApprovalStatus.REJECTED,
                reviewer=reviewer,
                reason=reason,
            )
            self._event(
                action.run_id,
                source='orchestrator',
                event_type='action.rejected',
                payload={
                    'action_id': action_id,
                    'reviewer': reviewer,
                    'reason': reason,
                },
            )
            self._resume_rejected_action(action, feedback=reason)
            return action

    def _resume_rejected_action(
        self,
        action: ActionRecord,
        *,
        feedback: str,
    ) -> None:
        run = self.store.get_run(action.run_id)
        if action.type == 'approve_protocol':
            if run.state != RunState.AWAITING_PROTOCOL_APPROVAL:
                return
            self._transition(
                action.run_id,
                RunState.HONEYDEW_DRAFTING_PROTOCOL,
            )
            self._draft_protocol(action.run_id, feedback=feedback)
        elif action.type == 'submit_experiment_matrix':
            if run.state != RunState.AWAITING_EXECUTION_APPROVAL:
                return
            self._transition(action.run_id, RunState.BEAKER_REVISING)
            self._beaker_revise(action.run_id, feedback=feedback)
        elif action.type == 'accept_final_report':
            if run.state != RunState.AWAITING_FINAL_ACCEPTANCE:
                return
            self._transition(
                action.run_id,
                RunState.HONEYDEW_WRITING_REPORT,
            )
            self._write_report(action.run_id, feedback=feedback)
        elif action.type == 'propose_evaluation_contract':
            if run.state != RunState.AWAITING_CONTRACT_PROMOTION:
                return
            self._transition(
                action.run_id,
                RunState.BEAKER_DRAFTING_CONTRACT,
            )
            self._beaker_draft_contract(action.run_id, feedback=feedback)
        elif run.state not in TERMINAL_STATES:
            self._fail_run(
                action.run_id,
                WorkflowError(f'unhandled rejected action: {action.type}'),
            )

    def _beaker_draft_contract(
        self,
        run_id: str,
        *,
        feedback: str | None = None,
    ) -> None:
        proposal = self._latest_contract_proposal(run_id)
        if proposal is None:
            raise WorkflowError('run has no evaluation contract proposal')
        prompt = (
            'Draft an immutable evaluation-contract candidate for the approved '
            'program.md. Create a self-contained directory under '
            '`contract-candidate/<contract-id>/<version>/` containing '
            'contract.json, the execution wrapper, evaluator, input and output '
            'JSON schemas, and concise test vectors or tests. contract.json '
            'must be one JSON object with exactly these top-level keys and '
            'no others: contract_id (string; it must be EXACTLY the approved '
            'proposal evaluator_type value below, not a descriptive name), '
            'manifest (an inline JSON object with FLAT keys only: '
            'manifest.primary_metric must be the plain metric-name STRING '
            '(for example "roc_auc"), never a nested object; '
            'manifest.primary_metric_direction must be the plain STRING '
            '"maximize" or "minimize"; it may also hold '
            'methodology_requirements, budget, '
            'and guardrails; never a filename reference), execution_wrapper '
            '(relative path string to the wrapper .py file inside the '
            'candidate directory), evaluation_entry_point (relative path '
            'string to the evaluator .py file), expected_input_schema '
            '(relative path string to the input JSON schema file), '
            'expected_output_schema (relative path string to the output '
            'JSON schema file), required_artifacts (non-empty list of '
            'strings), resource_constraints (object with numeric cpu, '
            'memory_gib, gpus, wallclock_minutes), container_image_digest '
            '(set to null). The descriptor must not contain kind, '
            'candidate_path, rationale, or any other keys. When the binding '
            'task '
            'requires explicit methodological choices or comparisons, encode '
            'them as manifest.methodology_requirements entries with '
            'requirement_id, config_path, mode (`decision` or `comparison`), '
            'minimum_distinct_values, optional maximum_distinct_values, and '
            'description. Do not turn a required choice into a comparison. '
            'execution_wrapper and '
            'evaluation_entry_point must each be a relative path to a real '
            'Python file inside the candidate directory, not a command or '
            'module reference. Do not write '
            'contract.sha256; the orchestrator owns sealing. Run lightweight '
            'local checks with PYTHONDONTWRITEBYTECODE=1 and remove any '
            '__pycache__ directory before returning. Return exactly one '
            '`propose_evaluation_contract` '
            'action whose arguments contain exactly the keys `contract_id`, '
            '`version` (semantic-version string such as 1.0.0; the key is '
            '`version`, never `semantic_version`), `candidate_path` (relative '
            'to the Beaker workspace), and `rationale`. Do not request '
            'publication, Kubernetes, registry, or '
            'shared-storage access.\n\nApproved scientific proposal:\n'
            + json.dumps(proposal, indent=2, sort_keys=True)
        )
        if feedback:
            prompt += f'\n\nReview feedback:\n{feedback}'
        turn, result = self._run_agent_turn(
            run_id=run_id,
            agent=AgentName.BEAKER,
            prompt=prompt,
            expected_kind=TurnKind.CONTRACT_CANDIDATE,
            input_event={'proposal': proposal, 'feedback': feedback},
        )
        actions = self._record_requested_actions(
            run_id=run_id,
            agent=AgentName.BEAKER,
            result=result,
            turn_number=self.store.get_run(run_id).turn_number,
        )
        candidates = [
            action
            for action in actions
            if action.type == 'propose_evaluation_contract'
            and action.approval_status == ApprovalStatus.PENDING
        ]
        if len(candidates) != 1:
            denied = [
                action.reason
                for action in actions
                if action.type == 'propose_evaluation_contract'
            ]
            feedback_message = (
                'Beaker must propose exactly one valid contract candidate'
                + (
                    f': {"; ".join(denied)}'
                    if denied
                    else '; the previous response contained no '
                    'propose_evaluation_contract action'
                )
            )
            # Same deterministic-retry pattern as the seal-rejection path: the
            # agent re-drafts with the concrete failure as feedback. The chain
            # is bounded by the per-run turn budget (TIMED_OUT when exhausted),
            # so a stuck agent cannot loop forever.
            self._event(
                run_id,
                source='orchestrator',
                event_type='contract.candidate_rejected',
                payload={
                    'action_id': None,
                    'reason': feedback_message,
                    'retrying': True,
                },
            )
            self._beaker_draft_contract(
                run_id,
                feedback=feedback_message,
            )
            return
        action = candidates[0]
        request = ContractCandidateRequest.model_validate(action.arguments)
        workspace = Path(self.store.get_run(run_id).beaker_workspace).resolve()
        source = (workspace / request.candidate_path).resolve()
        if not source.is_relative_to(workspace):
            raise WorkflowError('contract candidate escapes Beaker workspace')
        # The orchestrator seals the candidate itself and validates the sealed
        # descriptor against the approved scientific proposal, so the agent can
        # neither invent an evaluation entry point nor slip an override past the
        # policy layer (which only checked the action schema).
        try:
            sealed = self.contract_candidates.seal(
                source=source,
                contract_id=request.contract_id,
                version=request.version,
            )
            descriptor = sealed.descriptor
            primary = proposal.get('primary_metric')
            resources = proposal.get('resource_constraints')
            required_artifacts = proposal.get('required_artifacts')
            mismatches: list[str] = []
            if request.contract_id != proposal.get('evaluator_type'):
                mismatches.append(
                    'contract_id must be exactly the approved proposal '
                    f'evaluator_type {proposal.get("evaluator_type")!r} but '
                    f'the action proposed {request.contract_id!r}'
                )
            if not isinstance(primary, dict):
                mismatches.append(
                    'approved proposal has no primary_metric object'
                )
            else:
                if descriptor.manifest.get('primary_metric') != primary.get(
                    'name'
                ):
                    mismatches.append(
                        'manifest.primary_metric must equal the approved '
                        f'primary metric name {primary.get("name")!r} but is '
                        f'{descriptor.manifest.get("primary_metric")!r}'
                    )
                if descriptor.manifest.get('primary_metric_direction') != (
                    primary.get('direction')
                ):
                    mismatches.append(
                        'manifest.primary_metric_direction must equal the '
                        'approved direction '
                        f'{primary.get("direction")!r} but is '
                        f'{descriptor.manifest.get("primary_metric_direction")!r}'
                    )
            if not isinstance(required_artifacts, list):
                mismatches.append(
                    'approved proposal has no required_artifacts list'
                )
            elif not set(required_artifacts).issubset(
                descriptor.required_artifacts
            ):
                missing = sorted(
                    set(required_artifacts) - set(descriptor.required_artifacts)
                )
                mismatches.append(
                    'descriptor.required_artifacts must include every '
                    f'approved artifact; missing: {missing}'
                )
            if not isinstance(resources, dict):
                mismatches.append(
                    'approved proposal has no resource_constraints object'
                )
            else:
                if float(resources.get('cpu', float('inf'))) > (
                    descriptor.resource_constraints.cpu
                ):
                    mismatches.append(
                        'resource_constraints.cpu is below the approved '
                        f'minimum {resources.get("cpu")}'
                    )
                if float(resources.get('memory_gib', float('inf'))) > (
                    descriptor.resource_constraints.memory_gib
                ):
                    mismatches.append(
                        'resource_constraints.memory_gib is below the '
                        f'approved minimum {resources.get("memory_gib")}'
                    )
                if int(resources.get('gpus', self.policy.maximum_gpus + 1)) > (
                    descriptor.resource_constraints.gpus
                ):
                    mismatches.append(
                        'resource_constraints.gpus is below the approved '
                        f'minimum {resources.get("gpus")}'
                    )
            if mismatches:
                raise WorkflowError(
                    'candidate descriptor does not implement the approved '
                    'scientific contract proposal: ' + '; '.join(mismatches)
                )
        except (OSError, ValueError, WorkflowError) as exc:
            feedback_message = (
                'The orchestrator rejected the candidate during deterministic '
                f'validation: {exc}'
            )
            self.store.update_action(
                action.action_id,
                approval_status=ApprovalStatus.REJECTED,
                reviewer='orchestrator',
                reason=feedback_message,
            )
            self._event(
                run_id,
                source='orchestrator',
                event_type='contract.candidate_rejected',
                payload={
                    'action_id': action.action_id,
                    'reason': feedback_message,
                    'retrying': True,
                },
            )
            # Rejection here is a deterministic retry: Beaker re-drafts with the
            # concrete failure as feedback. The retry chain is bounded by the
            # per-run turn budget, which eventually transitions the run to
            # TIMED_OUT if the candidate never becomes valid.
            self._beaker_draft_contract(
                run_id,
                feedback=feedback_message,
            )
            return
        review_path = self.workspaces.copy_contract_candidate_for_review(
            run_id=run_id,
            source=sealed.sealed_path,
            contract_id=sealed.contract_id,
            version=sealed.version,
            digest=sealed.digest,
        )
        uri = (
            f'artifact://{run_id}/contract-candidates/'
            f'{sealed.contract_id}/{sealed.version}@{sealed.digest}'
        )
        artifact = self._save_local_artifact(
            run_id=run_id,
            artifact_type='evaluation_contract_candidate',
            uri=uri,
            digest=sealed.digest,
            metadata={
                'action_id': action.action_id,
                'turn_id': turn.turn_id,
                'contract_id': sealed.contract_id,
                'version': sealed.version,
                'sealed_path': str(sealed.sealed_path),
                'review_path': str(review_path),
                'descriptor': descriptor.model_dump(mode='json'),
            },
        )
        self._event(
            run_id,
            source='orchestrator',
            event_type='contract.candidate_sealed',
            payload={
                'action_id': action.action_id,
                'artifact_id': artifact.artifact_id,
                'uri': uri,
                'contract_id': sealed.contract_id,
                'version': sealed.version,
                'digest': sealed.digest,
                'review_path': str(review_path),
            },
        )
        self._transition(run_id, RunState.HONEYDEW_REVIEWING_CONTRACT)
        self._honeydew_review_contract(
            run_id,
            action=action,
            artifact=artifact,
        )

    def _honeydew_review_contract(
        self,
        run_id: str,
        *,
        action: ActionRecord,
        artifact: ArtifactRecord,
    ) -> None:
        prompt = (
            'Review the sealed evaluation-contract candidate against the '
            'approved program.md and scientific proposal. Inspect every file '
            'under the read-only review path. Check metric semantics, schemas, '
            'required artifacts, leakage risks, deterministic behavior, '
            'resource limits, and whether the wrapper preserves the immutable '
            'evaluation boundary. Set done=true only if it is suitable for '
            'admin promotion. Set requested_actions to an empty list; the '
            'orchestrator already owns the promotion action. Do not propose '
            'file, approval, or transition actions. Do not edit the candidate.\n\n'
            f'Review path: {artifact.metadata["review_path"]}\n'
            f'Descriptor:\n'
            + json.dumps(
                artifact.metadata['descriptor'],
                indent=2,
                sort_keys=True,
            )
        )
        turn, result = self._run_agent_turn(
            run_id=run_id,
            agent=AgentName.HONEYDEW,
            prompt=prompt,
            expected_kind=TurnKind.METHODOLOGY_REVIEW,
            input_event={
                'action_id': action.action_id,
                'artifact_id': artifact.artifact_id,
            },
        )
        if not result.done:
            reason = result.summary
            self.store.update_action(
                action.action_id,
                approval_status=ApprovalStatus.REJECTED,
                reviewer='honeydew',
                reason=reason,
            )
            self._event(
                run_id,
                source='honeydew',
                event_type='action.rejected',
                payload={
                    'action_id': action.action_id,
                    'reason': reason,
                },
            )
            self._transition(run_id, RunState.BEAKER_DRAFTING_CONTRACT)
            self._beaker_draft_contract(
                run_id,
                feedback=result.message_to_other_agent or reason,
            )
            return
        approved = self.store.mark_action_honeydew_approved(
            action.action_id,
            review_turn_id=turn.turn_id,
        )
        # Honeydew review is recorded on the action itself (satisfying the
        # HONEYDEW_AND_HUMAN_APPROVAL gate in approve_action), then the run
        # moves to a human-wait state where promotion can be approved.
        self._event(
            run_id,
            source='honeydew',
            event_type='contract.candidate_reviewed',
            payload={
                'action_id': action.action_id,
                'artifact_id': artifact.artifact_id,
                'digest': artifact.sha256,
                'human_approval_still_required': True,
            },
        )
        self._transition(run_id, RunState.AWAITING_CONTRACT_PROMOTION)
        self._event(
            run_id,
            source='orchestrator',
            event_type='action.human_approval_requested',
            payload=self._approval_event_payload(
                approved,
                human_approval_ready=True,
            ),
        )

    def _promote_contract_candidate(self, action: ActionRecord) -> None:
        matches = [
            artifact
            for artifact in self.store.list_artifacts(action.run_id)
            if artifact.type == 'evaluation_contract_candidate'
            and artifact.metadata.get('action_id') == action.action_id
        ]
        if len(matches) != 1:
            raise WorkflowError(
                'approved contract action has no unique sealed candidate'
            )
        artifact = matches[0]
        destination = self.contract_candidates.promote(
            sealed_path=Path(str(artifact.metadata['sealed_path'])),
            expected_digest=artifact.sha256,
        )
        descriptor = EvaluationContractDescriptor.model_validate(
            artifact.metadata['descriptor']
        )
        current = self.store.get_run(action.run_id)
        # The run's binding is rewritten to the promoted contract; the resolve
        # below re-reads the trusted catalog so the digest check proves the
        # promoted artifact is the one actually installed.
        self.store.replace_run(
            current.model_copy(
                update={
                    'evaluation_contract_id': descriptor.contract_id,
                    'evaluation_contract_version': descriptor.version,
                    'evaluation_contract_digest': artifact.sha256,
                }
            ),
            expected_version=current.version,
        )
        resolved = self.contracts.resolve(
            descriptor.contract_id,
            descriptor.version,
        )
        if resolved.digest != artifact.sha256:
            raise WorkflowError('promoted contract cannot be resolved by digest')
        self._event(
            action.run_id,
            source='orchestrator',
            event_type='contract.promoted',
            payload={
                'action_id': action.action_id,
                'contract_id': descriptor.contract_id,
                'version': descriptor.version,
                'digest': artifact.sha256,
                'path': str(destination),
                'reviewer': action.reviewer,
            },
        )
        if not self._contract_binding_compatible(action.run_id):
            raise WorkflowError(
                'promoted contract remains incompatible with the protocol'
            )
        self._transition(action.run_id, RunState.BEAKER_PLANNING)
        self._beaker_plan(action.run_id)

    def _build_objective_execution(
        self,
        run: RunRecord,
        matrix: ExperimentMatrix,
    ) -> dict[str, object]:
        # Objective-driven runs have no imported task bundle. The deployed
        # workflow-api workspace contract requires both a task and a source
        # bundle, so synthesize a deterministic generic task bundle from the
        # objective text itself instead of weakening that contract (issue
        # #98). Beaker's worktree is the source bundle; datasets cited by
        # the objective are bound by their immutable registry records.
        problem = (
            '# Objective\n\n' + run.objective.strip() + '\n\n'
            '# Run binding\n\n'
            f'- run_id: {run.run_id}\n'
            '- evaluation_contract: '
            f'{run.evaluation_contract_id}@'
            f'{run.evaluation_contract_version}\n'
        )
        evaluator_prompt = (
            'Evaluate the artifacts emitted under GLASSLAB_OUTPUT_DIR for '
            'this run against its immutable evaluation contract.\n'
            'contract: '
            f'{run.evaluation_contract_id}@{run.evaluation_contract_version}\n'
            'Required artifacts are declared by the contract manifest; the '
            'workload must not create or score evaluation.json, '
            'integrity_pass, or rubric_score.\n'
        )
        bundle_root = (
            Path(self.settings.task_bundle_root) / 'objective'
        )
        bundle_root.mkdir(parents=True, exist_ok=True)
        staging_digest = sha256(
            (problem + evaluator_prompt + run.run_id).encode('utf-8')
        ).hexdigest()
        bundle_path = bundle_root / staging_digest / 'task.zip'
        if not bundle_path.is_file():
            bundle_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = bundle_path.with_suffix('.tmp')
            with zipfile.ZipFile(
                temporary,
                mode='w',
                compression=zipfile.ZIP_DEFLATED,
            ) as archive:
                for name, content in (
                    ('problem.md', problem),
                    ('eval_agent_prompt.md', evaluator_prompt),
                ):
                    info = zipfile.ZipInfo(name)
                    info.date_time = (1980, 1, 1, 0, 0, 0)
                    archive.writestr(
                        info,
                        content,
                        compress_type=zipfile.ZIP_DEFLATED,
                    )
            os.replace(temporary, bundle_path)
        shared_root = Path(self.settings.shared_mount_root).resolve()
        bundle_relative = bundle_path.resolve().relative_to(shared_root)

        source_path, source_digest = self.workspaces.package_source_bundle(
            run_id=run.run_id,
            source_subdirectory='.',
        )
        source_relative = source_path.resolve().relative_to(shared_root)
        self._save_local_artifact(
            run_id=run.run_id,
            artifact_type='source_bundle',
            uri='artifact://' + source_relative.as_posix(),
            digest=source_digest,
            metadata={'path': str(source_path)},
        )

        dataset_ids = sorted(
            set(
                re.findall(
                    r'glasslab-dataset://([0-9a-f]{64})',
                    run.objective,
                )
            )
        )
        records = [self.store.get_dataset(d) for d in dataset_ids]
        return {
            'workload_id': self.settings.cluster_execution_workload_id,
            'experiment_type': 'research-workspace-job',
            'task_bundle': {
                'uri': 's3://artifacts/' + bundle_relative.as_posix(),
                'sha256': self._file_sha256(bundle_path),
            },
            'source_bundle': {
                'uri': 's3://artifacts/' + source_relative.as_posix(),
                'sha256': source_digest,
            },
            'workspace_command': ['python3', 'run.py'],
            'dataset_contracts': [
                {
                    'name': record.name,
                    'role': record.role,
                    'contains_labels': record.contains_labels,
                    'asset': {
                        'uri': record.artifact_uri,
                        'sha256': record.sha256,
                    },
                }
                for record in records
            ],
            'dataset_bindings': {
                record.name: record.artifact_uri for record in records
            },
        }

    def _file_sha256(self, path: Path) -> str:
        digest = sha256()
        with path.open('rb') as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b''):
                digest.update(chunk)
        return digest.hexdigest()

    def _beaker_plan(self, run_id: str) -> None:
        run = self.store.get_run(run_id)
        task_context = ''
        if run.task_definition:
            task_context = (
                ' This is an imported task; inspect `benchmark-task/problem.md` '
                'and `benchmark-task/eval_agent_prompt.md` as binding inputs.'
            )
        _, result = self._run_agent_turn(
            run_id=run_id,
            agent=AgentName.BEAKER,
            prompt=(
                'Read the approved read-only program.md and inspect the existing '
                'worktree. Write implementation-plan.md using OpenCode\'s '
                'workspace file tool before returning structured output, then read '
                'the file back to verify it exists. A produced_files declaration '
                'is metadata and does not create the file. The file must contain '
                'a concise, '
                'task-specific sequence of independently checkable implementation '
                'steps, the files likely to change, lightweight local checks, and '
                'the intended experiment-matrix shape. Preserve freedom to revise '
                'the plan when repository evidence requires it. Do not implement '
                'the experiment in this turn, request actions, or execute cluster '
                'work. Return exactly that file with purpose "implementation" and '
                'kind "implementation_plan".'
                + task_context
            ),
            expected_kind=TurnKind.IMPLEMENTATION_PLAN,
            input_event={
                'protocol_path': run.protocol_path,
                'protocol_version': run.protocol_version,
            },
        )
        plans = [
            item
            for item in result.produced_files
            if item.path == 'implementation-plan.md'
            and item.purpose == 'implementation'
        ]
        if len(plans) != 1 or result.requested_actions:
            raise WorkflowError(
                'Beaker planning must produce implementation-plan.md and no actions'
            )
        plan_path = Path(run.beaker_workspace) / 'implementation-plan.md'
        if not plan_path.is_file():
            self._event(
                run_id,
                source='orchestrator',
                event_type='agent.file_repair_requested',
                payload={
                    'agent': AgentName.BEAKER.value,
                    'path': 'implementation-plan.md',
                    'reason': (
                        'The structured response declared the plan, but no '
                        'workspace file existed.'
                    ),
                },
            )
            _, repaired = self._run_agent_turn(
                run_id=run_id,
                agent=AgentName.BEAKER,
                prompt=(
                    'Focused workspace repair. Your previous structured response '
                    'declared `implementation-plan.md`, but the authoritative '
                    'workspace does not contain that file. Write '
                    'implementation-plan.md now using OpenCode\'s workspace file '
                    'tool, then read it back. Do not implement experiment code, '
                    'request actions, or repeat methodology work. Return kind '
                    '`implementation_plan`, declare exactly that file with purpose '
                    '`implementation`, and only claim completion after verifying '
                    'the file exists.'
                ),
                expected_kind=TurnKind.IMPLEMENTATION_PLAN,
                input_event={
                    'repair': 'missing_workspace_file',
                    'path': 'implementation-plan.md',
                },
            )
            repaired_plans = [
                item
                for item in repaired.produced_files
                if item.path == 'implementation-plan.md'
                and item.purpose == 'implementation'
            ]
            if len(repaired_plans) != 1 or repaired.requested_actions:
                raise WorkflowError(
                    'Beaker plan repair must produce implementation-plan.md '
                    'and no actions'
                )
            if not plan_path.is_file():
                raise WorkflowError(
                    'Beaker did not create implementation-plan.md after one '
                    'focused repair turn'
                )
            self._event(
                run_id,
                source='beaker',
                event_type='agent.file_repair_completed',
                payload={'path': 'implementation-plan.md'},
            )
        digest = sha256(plan_path.read_bytes()).hexdigest()
        self._event(
            run_id,
            source='beaker',
            event_type='agent.plan_created',
            payload={
                'path': 'implementation-plan.md',
                'sha256': digest,
            },
        )
        self._transition(run_id, RunState.BEAKER_IMPLEMENTING)
        self._beaker_implement(run_id)

    def _beaker_implement(self, run_id: str) -> None:
        run = self.store.get_run(run_id)
        plan_path = Path(run.beaker_workspace) / 'implementation-plan.md'
        if not plan_path.is_file():
            self._transition(run_id, RunState.BEAKER_PLANNING)
            self._beaker_plan(run_id)
            return
        task_context = ''
        if run.task_definition:
            source = run.task_definition['source_subdirectory']
            contract = self.contracts.resolve(
                run.evaluation_contract_id,
                run.evaluation_contract_version,
            )
            required_metric_keys = list(
                contract.descriptor.manifest.get('required_metric_keys', [])
            )
            dataset_binding_example = {
                dataset['name']: f'/mnt/datasets/{dataset["name"]}'
                for dataset in run.task_definition['datasets']
            }
            task_context = (
                '\nThis is an imported benchmark task. Read '
                '`benchmark-task/problem.md`, `benchmark-task/eval_agent_prompt.md`, '
                'and the frozen `program.md`. Put the complete executable '
                f'solution under `{source}/`, including `run.py`. The run must '
                'use only dataset paths supplied through '
                '`GLASSLAB_DATASET_BINDINGS_JSON`. That JSON object is keyed '
                'by each exact declared dataset `name`, not by its `role`; use '
                'the dataset list below to bind names to train, test, or other '
                'roles. Do not assume generic keys such as `train` or `test` '
                'unless those are the declared names. Write all outputs beneath '
                '`GLASSLAB_OUTPUT_DIR`, and make no network calls. Use the '
                'following object shape in the loader (paths are illustrative):\n'
                + json.dumps(dataset_binding_example, indent=2, sort_keys=True)
                + '\nRun a loader-only smoke check by setting that environment '
                'variable to tiny local fixture paths and verifying each '
                'declared name resolves. Do not run the full benchmark, '
                'hyperparameter search, bootstrap evaluation, or generate '
                'dataset-sized local fixtures; those belong in the approved '
                'cluster job. Do not install Python or system dependencies in '
                'the orchestrator container. The preselected runner image owns '
                'experiment dependencies. If an import is unavailable locally, '
                'use syntax-only or dependency-free validation and continue. Use the '
                '`metrics.json` document root for every evaluator-required '
                'metric key listed below. For multi-model workloads, place the '
                'selected headline model values at the root; nested per-model '
                'details may be additional fields but do not satisfy the root '
                'contract. Include row counts, bootstrap counts, and confidence '
                'bounds at the root when named here:\n'
                + json.dumps(required_metric_keys, indent=2)
                + '\nUse the '
                'exact preselected runner image and resource request:\n'
                + json.dumps(
                    {
                        'runner_image': run.task_definition['runner_image'],
                        'resources': run.task_definition['resources'],
                        'required_artifacts': (
                            run.task_definition['required_artifacts']
                        ),
                        'datasets': run.task_definition['datasets'],
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
        prompt = (
            'Read the approved read-only program.md and implementation-plan.md. '
            'Execute the task-specific plan in your isolated worktree, adapting '
            'it when repository evidence requires. Your only tools are bash, '
            'edit, glob, grep, read, todowrite, and write; there is no run or '
            'list tool, so execute every command and script through bash. '
            'Every ingested dataset '
            'cited by the objective is already copied read-only under '
            '`datasets/` in this worktree; load it from there and never make '
            'network calls. The approved cluster job executes this worktree '
            'with `python3 run.py`, so a repo-root `run.py` entrypoint is '
            'mandatory: read `GLASSLAB_GENERIC_CONFIG_JSON` for the variant '
            'name, seed, base_config path, and overrides; read '
            '`GLASSLAB_DATASET_BINDINGS_JSON` (dataset name to mounted path) '
            'for data access; write every output artifact, including '
            '`metrics.json`, under `GLASSLAB_OUTPUT_DIR`. Finish the bounded experiment '
            'runner, run the listed lightweight checks, and '
            'propose one submit_experiment_matrix action. The action arguments '
            'must match the ExperimentMatrix schema and must not contain an '
            'evaluation entry point, Kubernetes manifest, contract file, or '
            'contract override. Do not execute cluster work. The workload must '
            'emit metrics and evidence only. Do not create, read, or score '
            '`evaluation.json`, `rubric_score`, or `integrity_pass`; those '
            'outputs belong exclusively to the immutable evaluation contract.'
            f'\n\nPermitted runner images: '
            f'{json.dumps(sorted(self.policy.permitted_images))}'
            '\nResource ceilings: '
            f'cpu={self.policy.maximum_cpu}, '
            f'memory_gib={self.policy.maximum_memory_gib}, '
            f'gpus={self.policy.maximum_gpus}, '
            'maximum_parallel_jobs='
            f'{self.policy.maximum_parallel_jobs}.'
            '\n\nRequired submit_experiment_matrix action shape:\n'
            + json.dumps(
                self._matrix_action_template(run),
                indent=2,
                sort_keys=True,
            )
            + '\nKeep reason beside type and arguments. Put the ExperimentMatrix '
            'fields directly in arguments; do not add a matrix or evaluator_type '
            'wrapper. Matrix seeds create separate cluster jobs. If the workload '
            'already runs an internal seed list for stability analysis, keep '
            'that list in the implementation and use one outer matrix seed; do '
            'not mirror the internal list into the matrix.'
            + task_context
        )
        turn, result = self._run_agent_turn(
            run_id=run_id,
            agent=AgentName.BEAKER,
            prompt=prompt,
            expected_kind=TurnKind.IMPLEMENTATION_PROPOSAL,
            input_event={
                'protocol_path': run.protocol_path,
                'protocol_version': run.protocol_version,
            },
        )
        actions = self._record_requested_actions(
            run_id=run_id,
            agent=AgentName.BEAKER,
            result=result,
            turn_number=self.store.get_run(run_id).turn_number,
        )
        self._advance_after_matrix_proposal(
            run_id=run_id,
            implementation_turn_id=turn.turn_id,
            actions=actions,
        )

    def _beaker_finalize(self, run_id: str) -> None:
        run = self.store.get_run(run_id)
        if not self._has_imported_task_implementation(run):
            self._transition(run_id, RunState.BEAKER_IMPLEMENTING)
            self._beaker_implement(run_id)
            return
        task = run.task_definition
        assert task is not None
        config_path = Path(run.beaker_workspace) / 'configs' / 'candidate.yaml'
        matrix_only = config_path.is_file() and not config_path.is_symlink()
        if matrix_only:
            task_instruction = (
                'Finalize the existing imported benchmark structured handoff. '
                'The preserved runner and configs/candidate.yaml already exist '
                'from the previous bounded turn. Do not use any tools, inspect '
                'files, edit files, rerun checks, or repeat implementation work. '
                'Return one implementation_proposal result containing exactly '
                'one submit_experiment_matrix action in the required shape below.'
            )
        else:
            task_instruction = (
                'Finalize the existing imported benchmark implementation after '
                'an interrupted implementation turn. Inspect '
                'implementation-plan.md and '
                f'`{task["source_subdirectory"]}/run.py` as the preserved '
                'checkpoint. Do not redesign or broadly rewrite the solution and '
                'do not run the full benchmark. Run only narrow local validation '
                'such as Python compilation and the smallest available smoke '
                'check. Fix concrete blocking defects you observe, create '
                'configs/candidate.yaml if it is missing, and return exactly one '
                'submit_experiment_matrix action. Do not execute cluster work.'
            )
        prompt = (
            task_instruction
            + '\n\nThe workload must emit metrics and evidence only. Do not '
            'create, read, or score `evaluation.json`, `rubric_score`, or '
            '`integrity_pass`; the immutable contract owns those outputs.'
            + '\n\nUse exactly this imported-task execution boundary:\n'
            + json.dumps(
                {
                    'runner_image': task['runner_image'],
                    'resources': task['resources'],
                    'required_artifacts': task['required_artifacts'],
                    'datasets': task['datasets'],
                },
                indent=2,
                sort_keys=True,
            )
            + '\n\nPermitted runner images: '
            + json.dumps(sorted(self.policy.permitted_images))
            + '\nResource ceilings: '
            f'cpu={self.policy.maximum_cpu}, '
            f'memory_gib={self.policy.maximum_memory_gib}, '
            f'gpus={self.policy.maximum_gpus}, '
            'maximum_parallel_jobs='
            f'{self.policy.maximum_parallel_jobs}.'
            '\n\nRequired submit_experiment_matrix action shape:\n'
            + json.dumps(
                self._matrix_action_template(run),
                indent=2,
                sort_keys=True,
            )
            + '\nKeep reason beside type and arguments. Put the ExperimentMatrix '
            'fields directly in arguments; do not add a matrix or evaluator_type '
            'wrapper. Matrix seeds create separate cluster jobs. If the workload '
            'already runs an internal seed list for stability analysis, keep '
            'that list in the implementation and use one outer matrix seed; do '
            'not mirror the internal list into the matrix.'
        )
        turn, result = self._run_agent_turn(
            run_id=run_id,
            agent=AgentName.BEAKER,
            prompt=prompt,
            expected_kind=TurnKind.IMPLEMENTATION_PROPOSAL,
            input_event={
                'protocol_path': run.protocol_path,
                'protocol_version': run.protocol_version,
                'checkpoint': (
                    f'{task["source_subdirectory"]}/run.py'
                ),
                'recovery_mode': (
                    'emit_existing_matrix'
                    if matrix_only
                    else 'finalize_existing_implementation'
                ),
            },
        )
        actions = self._record_requested_actions(
            run_id=run_id,
            agent=AgentName.BEAKER,
            result=result,
            turn_number=self.store.get_run(run_id).turn_number,
        )
        self._advance_after_matrix_proposal(
            run_id=run_id,
            implementation_turn_id=turn.turn_id,
            actions=actions,
        )

    def _advance_after_matrix_proposal(
        self,
        *,
        run_id: str,
        implementation_turn_id: str,
        actions: list[ActionRecord],
    ) -> None:
        pending = any(
            action.type == 'submit_experiment_matrix'
            and action.approval_status == ApprovalStatus.PENDING
            for action in actions
        )
        if not pending:
            feedback = self._matrix_revision_feedback(actions)
            self._request_methodology_revision(
                run_id,
                feedback=feedback,
            )
            return
        self._transition(run_id, RunState.HONEYDEW_REVIEWING)
        self._honeydew_review(
            run_id,
            implementation_turn_id=implementation_turn_id,
        )

    @staticmethod
    def _has_imported_task_implementation(run: RunRecord) -> bool:
        if not run.task_definition:
            return False
        workspace = Path(run.beaker_workspace).resolve()
        source = (
            workspace / str(run.task_definition['source_subdirectory'])
        ).resolve()
        entrypoint = source / 'run.py'
        return (
            source.is_relative_to(workspace)
            and entrypoint.is_file()
            and not entrypoint.is_symlink()
        )

    def _matrix_action_template(self, run: RunRecord) -> dict[str, Any]:
        if run.task_definition:
            task = run.task_definition
            runner_image = str(task['runner_image'])
            resources = dict(task['resources'])
            required_artifacts = list(task['required_artifacts'])
        else:
            runner_image = sorted(self.policy.permitted_images)[0]
            resources = {
                'cpu': min(1.0, self.policy.maximum_cpu),
                'memory_gib': min(2.0, self.policy.maximum_memory_gib),
                'gpus': 0,
                'wallclock_minutes': 30,
            }
            required_artifacts = ['metrics.json']
        return {
            'type': 'submit_experiment_matrix',
            'arguments': {
                'base_config': 'configs/candidate.yaml',
                'variants': [
                    {
                        'name': 'candidate',
                        'overrides': {},
                    }
                ],
                'seeds': [17],
                'maximum_parallel_jobs': min(
                    1,
                    self.policy.maximum_parallel_jobs,
                ),
                'runner_image': runner_image,
                'resources': resources,
                'required_artifacts': required_artifacts,
            },
            'reason': (
                'Run the bounded candidate for methodology and human review.'
            ),
        }

    @staticmethod
    def _matrix_revision_feedback(actions: list[ActionRecord]) -> str:
        denied = [
            action.reason
            for action in actions
            if action.type == 'submit_experiment_matrix'
            and action.approval_status == ApprovalStatus.DENIED
        ]
        if denied:
            return (
                'The orchestrator policy denied the proposed experiment '
                f'matrix: {"; ".join(denied)}'
            )
        return (
            'No valid submit_experiment_matrix action was returned. Propose '
            'exactly one matrix that satisfies the supplied policy bounds.'
        )

    def _latest_pending_matrix_action(self, run_id: str) -> ActionRecord:
        matches = [
            action
            for action in self.store.list_actions(run_id)
            if action.type == 'submit_experiment_matrix'
            and action.approval_status == ApprovalStatus.PENDING
        ]
        if not matches:
            raise WorkflowError('run has no pending experiment matrix')
        return matches[-1]

    def _honeydew_review(
        self,
        run_id: str,
        *,
        implementation_turn_id: str,
    ) -> None:
        action = self._latest_pending_matrix_action(run_id)
        preflight = self._matrix_preflight_report(
            run_id=run_id,
            action=action,
        )
        if not preflight.passed:
            joined = '; '.join(preflight.errors)
            rejection_reason = (
                'Deterministic matrix preflight failed: '
                + joined
                + self._methodology_resolution_appendix(joined)
            )
            self.store.update_action(
                action.action_id,
                approval_status=ApprovalStatus.REJECTED,
                reviewer='orchestrator',
                reason=rejection_reason,
            )
            self._event(
                run_id,
                source='orchestrator',
                event_type='action.rejected',
                payload={
                    'action_id': action.action_id,
                    'reason': rejection_reason,
                    'preflight': preflight.model_dump(mode='json'),
                },
            )
            self._request_methodology_revision(
                run_id,
                feedback=rejection_reason,
            )
            return
        snapshot_path, snapshot_manifest = self._create_methodology_review_snapshot(
            run_id=run_id,
            action=action,
        )
        run = self.store.get_run(run_id)
        contract = self.contracts.resolve(
            run.evaluation_contract_id,
            run.evaluation_contract_version,
        )
        required_metric_keys = list(
            contract.descriptor.manifest.get('required_metric_keys', [])
        )
        prompt = (
            'Review Beaker\'s implementation and proposed experiment matrix. '
            'Check controls, confounds, data leakage, comparability, resource '
            'bounds, required artifacts, and alignment with program.md and the '
            'immutable evaluation contract. Beaker\'s files are available as a '
            'bounded snapshot under `.glasslab-review/` in your workspace. Read '
            '`.glasslab-review/manifest.json`, the candidate config, and the '
            'included implementation files before deciding. The snapshot is a '
            'copy for review; editing it cannot modify Beaker\'s worktree. Set '
            'done=true only when the matrix is methodologically acceptable. Do '
            'not submit the job. The deterministic preflight below is '
            'authoritative about which settings are required comparisons and '
            'which are one-time methodological decisions. Do not turn a '
            '`decision` into a required experiment axis unless the binding '
            'task explicitly requires that comparison. Inspect the actual '
            'metrics serialization code and reject the matrix unless every '
            'evaluator-required metric key below is emitted at the document '
            'root of `metrics.json`; nested copies do not satisfy the contract.\n'
            'Required root metric keys:\n'
            f'{json.dumps(required_metric_keys, indent=2)}\n\n'
            'Deterministic preflight:\n'
            f'{preflight.model_dump_json(indent=2)}\n\n'
            'Review snapshot manifest:\n'
            f'{json.dumps(snapshot_manifest, indent=2, sort_keys=True)}\n\n'
            f'Proposed matrix:\n{json.dumps(action.arguments, indent=2, sort_keys=True)}'
        )
        self._event(
            run_id,
            source='orchestrator',
            event_type='agent.review_snapshot_created',
            payload={
                'implementation_turn_id': implementation_turn_id,
                'action_id': action.action_id,
                'path': str(snapshot_path),
                'files': snapshot_manifest,
            },
        )
        turn, result = self._run_agent_turn(
            run_id=run_id,
            agent=AgentName.HONEYDEW,
            prompt=prompt,
            expected_kind=TurnKind.METHODOLOGY_REVIEW,
            input_event={
                'implementation_turn_id': implementation_turn_id,
                'action_id': action.action_id,
            },
        )
        if not result.done:
            rejection_reason = result.summary
            self.store.update_action(
                action.action_id,
                approval_status=ApprovalStatus.REJECTED,
                reviewer='honeydew',
                reason=rejection_reason,
            )
            self._event(
                run_id,
                source='honeydew',
                event_type='action.rejected',
                payload={
                    'action_id': action.action_id,
                    'reason': rejection_reason,
                },
            )
            # Honeydew's summary alone can drop the deterministic error
            # mechanics (e.g. how methodology requirements resolve against
            # base_config); the raw preflight text must survive into the
            # revision prompt or every revision burns out identically.
            feedback = self._methodology_feedback(result)
            preflight_error = self._matrix_preflight_error(
                run_id=run_id,
                action=action,
            )
            if preflight_error:
                feedback = (
                    feedback + '\n\nDeterministic preflight error: '
                    + preflight_error
                )
            self._request_methodology_revision(
                run_id,
                feedback=feedback,
            )
            return
        approved_action = self.store.mark_action_honeydew_approved(
            action.action_id,
            review_turn_id=turn.turn_id,
        )
        self._event(
            run_id,
            source='honeydew',
            event_type='action.approved',
            payload={
                'action_id': action.action_id,
                'approval_stage': 'methodology',
                'human_approval_still_required': True,
            },
        )
        self._transition(run_id, RunState.AWAITING_EXECUTION_APPROVAL)
        self._event(
            run_id,
            source='orchestrator',
            event_type='action.human_approval_requested',
            payload=self._approval_event_payload(
                approved_action,
                human_approval_ready=True,
            ),
        )

    def _create_methodology_review_snapshot(
        self,
        *,
        run_id: str,
        action: ActionRecord,
    ) -> tuple[Path, list[dict[str, object]]]:
        run = self.store.get_run(run_id)
        matrix = ExperimentMatrix.model_validate(action.arguments)
        workspace = Path(run.beaker_workspace).resolve()
        relative_paths = {
            matrix.base_config,
            'implementation-plan.md',
        }
        if run.task_definition:
            source = (
                workspace
                / str(run.task_definition['source_subdirectory'])
            ).resolve()
            if not source.is_relative_to(workspace) or not source.is_dir():
                raise WorkflowError(
                    'imported task source directory is missing from Beaker workspace'
                )
            relative_paths.update(
                path.relative_to(workspace).as_posix()
                for path in source.rglob('*')
                if path.is_file() and not path.is_symlink()
            )
        else:
            for command in (
                ['git', 'diff', '--name-only', '-z', 'HEAD'],
                ['git', 'ls-files', '--others', '--exclude-standard', '-z'],
            ):
                completed = subprocess.run(
                    command,
                    cwd=workspace,
                    capture_output=True,
                    check=False,
                    timeout=10,
                )
                if completed.returncode != 0:
                    raise WorkflowError(
                        completed.stderr.decode(errors='replace').strip()
                        or 'failed to enumerate Beaker review files'
                    )
                relative_paths.update(
                    item.decode(errors='strict')
                    for item in completed.stdout.split(b'\0')
                    if item
                )
        relative_paths = {
            relative
            for relative in relative_paths
            if '__pycache__' not in Path(relative).parts
            and Path(relative).suffix not in {'.pyc', '.pyo'}
        }
        try:
            return self.workspaces.create_review_snapshot(
                run_id=run_id,
                relative_paths=list(relative_paths),
            )
        except Exception as exc:
            raise WorkflowError(
                f'failed to create Honeydew review snapshot: {exc}'
            ) from exc

    def _matrix_preflight_report(
        self,
        *,
        run_id: str,
        action: ActionRecord,
    ) -> MatrixPreflightReport:
        errors: list[str] = []
        try:
            matrix = ExperimentMatrix.model_validate(action.arguments)
            run = self.store.get_run(run_id)
            if not self._contract_binding_compatible(run_id):
                errors.append(
                    'installed evaluation contract is incompatible with the '
                    'approved protocol; promote a matching contract first'
                )
            workspace = Path(run.beaker_workspace).resolve()
            base_config = (workspace / matrix.base_config).resolve()
            if (
                not base_config.is_relative_to(workspace)
                or not base_config.is_file()
            ):
                errors.append(
                    'base_config does not exist inside the Beaker workspace: '
                    f'{matrix.base_config}'
                )
            if run.task_definition:
                expected_image = str(run.task_definition['runner_image'])
                expected_resources = run.task_definition['resources']
                if matrix.runner_image != expected_image:
                    errors.append(
                        'imported benchmark requires runner_image '
                        f'{expected_image}'
                    )
                if matrix.resources.model_dump(mode='json') != expected_resources:
                    errors.append(
                        'imported benchmark resources must exactly match the '
                        'preselected task resource profile'
                    )
            contract = self.contracts.resolve(
                run.evaluation_contract_id,
                run.evaluation_contract_version,
            )
            if contract.digest != run.evaluation_contract_digest:
                errors.append('evaluation contract changed after run creation')
            expand_experiment_matrix(
                run_id=run_id,
                action_id=action.action_id,
                matrix=matrix,
                contract=contract,
            )
            report = preflight_matrix(
                run=run,
                matrix=matrix,
                contract=contract,
            )
            if errors:
                return report.model_copy(
                    update={
                        'passed': False,
                        'errors': [*errors, *report.errors],
                    }
                )
            return report
        except ValueError as exc:
            errors.append(str(exc))
        return MatrixPreflightReport(
            passed=False,
            job_count=0,
            errors=errors,
        )

    def _matrix_preflight_error(
        self,
        *,
        run_id: str,
        action: ActionRecord,
    ) -> str | None:
        report = self._matrix_preflight_report(
            run_id=run_id,
            action=action,
        )
        if not report.errors:
            return None
        joined = '; '.join(report.errors)
        return joined + self._methodology_resolution_appendix(joined)

    @staticmethod
    def _methodology_resolution_appendix(errors: str) -> str:
        if 'methodology setting' not in errors:
            return ''
        # The resolution mechanics (dotted paths into the base_config
        # document, list-of-distinct-values for comparisons) are not
        # guessable from the bare error; without this appendix every
        # revision burns out on the same gap (issue #98 run
        # 5fbf145886c84255b7af2e06ebded295).
        return (
            '. Methodology requirements resolve against the YAML file '
            'at matrix.base_config inside your worktree: the resolver '
            'splits each requirement config_path ONLY on "." characters; '
            'every other character, including "/", is a literal part of a '
            'YAML key name. So config_path "src/train.py" means: split on '
            'the dot -> first key is literally "src/train", then nested '
            'key "py". A comparison requirement needs that final node to '
            'be a list of at least minimum_distinct_values distinct '
            'strings; decision requirements need at least one value. '
            'Exact YAML for config_path "src/train.py" with three model '
            'families:\n'
            '"src/train":\n'
            '  "py":\n'
            '    - logistic_regression\n'
            '    - random_forest\n'
            '    - gradient_boosting\n'
            '(quote the "src/train" key; it contains a slash, not a dot). '
            'Edit the base_config file so every listed requirement '
            'resolves, then re-propose the same matrix.'
        )

    @staticmethod
    def _methodology_feedback(result: AgentTurnResult) -> str:
        sections = [result.summary]
        if result.claims:
            sections.append(
                'Concrete findings:\n'
                + '\n'.join(
                    f'- {claim.text}'
                    + (
                        f' Evidence: {", ".join(claim.evidence)}'
                        if claim.evidence
                        else ''
                    )
                    for claim in result.claims
                )
            )
        if result.message_to_other_agent.strip():
            sections.append(result.message_to_other_agent.strip())
        return '\n\n'.join(section for section in sections if section)

    def _request_methodology_revision(
        self,
        run_id: str,
        *,
        feedback: str,
    ) -> None:
        run = self.store.get_run(run_id)
        revision_count = run.methodology_revision_count + 1
        run = self.store.replace_run(
            run.model_copy(
                update={'methodology_revision_count': revision_count}
            ),
            expected_version=run.version,
        )
        if run.state != RunState.BEAKER_REVISING:
            self._transition(run_id, RunState.BEAKER_REVISING)
        self._event(
            run_id,
            source='orchestrator',
            event_type='methodology.revision_requested',
            payload={
                'revision_count': revision_count,
                'maximum_revisions': (
                    self.settings.maximum_methodology_revisions
                ),
                'feedback': feedback,
            },
        )
        if revision_count > self.settings.maximum_methodology_revisions:
            self._event(
                run_id,
                source='orchestrator',
                event_type='methodology.human_resolution_requested',
                payload={
                    'revision_count': revision_count,
                    'maximum_revisions': (
                        self.settings.maximum_methodology_revisions
                    ),
                    'feedback': feedback,
                },
            )
            self.pause_run(
                run_id,
                requested_by='orchestrator',
                reason=(
                    'Methodology review exceeded the automatic revision '
                    'limit. Human resolution is required.'
                ),
            )
            return
        self._beaker_revise(run_id, feedback=feedback)

    def _beaker_revise(self, run_id: str, *, feedback: str) -> None:
        run = self.store.get_run(run_id)
        task_context = ''
        if run.task_definition:
            task_context = (
                '\nPreserve the imported benchmark boundary: all executable '
                f'files stay under `{run.task_definition["source_subdirectory"]}`, '
                'datasets come only from the provided bindings, and the '
                'preselected image/resources must not change. The OpenCode '
                'runtime is not the approved experiment-runner image and may '
                'lack workload dependencies. Attempt each local command only '
                'once. If a check fails with ModuleNotFoundError, do not install '
                'packages, repeat the command, or inspect outputs that were not '
                'created. Run dependency-free checks such as Python compilation, '
                'record that the dependency-backed smoke check is deferred to '
                'the approved runner, and complete the structured handoff. Any '
                'claim evidence must use an allowed artifact://, git://, '
                'event://, job://, or contract:// URI rather than a bare path.'
            )
        preflight_focus = ''
        if feedback.startswith('Deterministic matrix preflight failed:'):
            rejected_matrices = [
                action
                for action in self.store.list_actions(run_id)
                if action.type == 'submit_experiment_matrix'
                and action.approval_status == ApprovalStatus.REJECTED
            ]
            base_config = (
                str(rejected_matrices[-1].arguments.get('base_config', ''))
                if rejected_matrices
                else ''
            )
            preflight_focus = (
                '\nThis is a focused deterministic-preflight correction, not '
                'a new implementation pass. The validator has already inspected '
                'the implementation. Read the rejected base config'
                + (f' `{base_config}`' if base_config else '')
                + ' and only the directly relevant protocol or source files. '
                'Every backticked config_path in the validator errors is an '
                'exact dotted path from the root of that config. For example, '
                '`experiment_dimensions.model` must be written beneath the '
                'top-level `experiment_dimensions` key, not beneath '
                '`methodology` or another wrapper. The exact path must directly '
                'contain its scalar or list of values; do not add `description` '
                'or `values` metadata wrappers. Correct every validator error '
                'exactly, then read back each required dotted path and run the '
                'narrowest applicable check before returning the replacement '
                'action. Do not browse unrelated repository files or redesign '
                'working code unless an error explicitly names that code.\n'
            )
        prompt = (
            'Revise the implementation and experiment matrix in response to '
            'the review below. Run local checks and return a replacement '
            'submit_experiment_matrix action. Do not execute cluster work.\n\n'
            + preflight_focus
            + 'The workload must emit metrics and evidence only. Remove any '
            'workload code that creates, reads, or scores `evaluation.json`, '
            '`rubric_score`, or `integrity_pass`; the immutable contract owns '
            'those outputs.\n\n'
            f'Permitted runner images: '
            f'{json.dumps(sorted(self.policy.permitted_images))}\n'
            'Resource ceilings: '
            f'cpu={self.policy.maximum_cpu}, '
            f'memory_gib={self.policy.maximum_memory_gib}, '
            f'gpus={self.policy.maximum_gpus}, '
            'maximum_parallel_jobs='
            f'{self.policy.maximum_parallel_jobs}.\n\n'
            'Required submit_experiment_matrix action shape:\n'
            + json.dumps(
                self._matrix_action_template(run),
                indent=2,
                sort_keys=True,
            )
            + '\nKeep reason beside type and arguments. Put the ExperimentMatrix '
            'fields directly in arguments; do not add a matrix or evaluator_type '
            'wrapper. Matrix seeds create separate cluster jobs. If the workload '
            'already runs an internal seed list for stability analysis, keep '
            'that list in the implementation and use one outer matrix seed; do '
            'not mirror the internal list into the matrix.\n\n'
            f'Review feedback:\n{feedback}'
            + task_context
        )
        turn, result = self._run_agent_turn(
            run_id=run_id,
            agent=AgentName.BEAKER,
            prompt=prompt,
            expected_kind=TurnKind.REVISION,
            input_event={'feedback': feedback},
        )
        actions = self._record_requested_actions(
            run_id=run_id,
            agent=AgentName.BEAKER,
            result=result,
            turn_number=self.store.get_run(run_id).turn_number,
        )
        pending = any(
            item.type == 'submit_experiment_matrix'
            and item.approval_status == ApprovalStatus.PENDING
            for item in actions
        )
        if not pending:
            self._request_methodology_revision(
                run_id,
                feedback=self._matrix_revision_feedback(actions),
            )
            return
        self._transition(run_id, RunState.HONEYDEW_REVIEWING)
        self._honeydew_review(run_id, implementation_turn_id=turn.turn_id)

    def _submit_matrix(self, action: ActionRecord) -> None:
        run = self.store.get_run(action.run_id)
        if run.state != RunState.AWAITING_EXECUTION_APPROVAL:
            raise WorkflowError(
                f'cannot submit matrix while run is {run.state.value}'
            )
        matrix = ExperimentMatrix.model_validate(action.arguments)
        contract = self.contracts.resolve(
            run.evaluation_contract_id,
            run.evaluation_contract_version,
        )
        if contract.digest != run.evaluation_contract_digest:
            raise WorkflowError('evaluation contract changed after run creation')
        execution: dict[str, object] | None = None
        if run.task_definition:
            task = run.task_definition
            source_path, source_digest = self.workspaces.package_source_bundle(
                run_id=run.run_id,
                source_subdirectory=str(task['source_subdirectory']),
            )
            shared_root = Path(self.settings.shared_mount_root).resolve()
            source_relative = source_path.resolve().relative_to(shared_root)
            execution = {
                'workload_id': task['workload_id'],
                'experiment_type': task['experiment_type'],
                'task_bundle': {
                    'uri': task['archive_uri'],
                    'sha256': task['digest'],
                },
                'source_bundle': {
                    'uri': 's3://artifacts/' + source_relative.as_posix(),
                    'sha256': source_digest,
                },
                'workspace_command': task['command'],
                'dataset_contracts': [
                    {
                        'name': dataset['name'],
                        'role': dataset['role'],
                        'contains_labels': dataset['contains_labels'],
                        'asset': {
                            'uri': dataset['uri'],
                            'sha256': dataset['sha256'],
                        },
                    }
                    for dataset in task['datasets']
                ],
                'dataset_bindings': {
                    dataset['name']: dataset['uri']
                    for dataset in task['datasets']
                },
                'task_spec': task.get('task_spec'),
            }
            self._save_local_artifact(
                run_id=run.run_id,
                artifact_type='source_bundle',
                uri='artifact://' + source_relative.as_posix(),
                digest=source_digest,
                metadata={'path': str(source_path)},
            )
        else:
            execution = self._build_objective_execution(run, matrix)
        specs = expand_experiment_matrix(
            run_id=run.run_id,
            action_id=action.action_id,
            matrix=matrix,
            contract=contract,
            execution=execution,
        )
        for spec in specs:
            record = JobRecord(
                job_id=spec.orchestrator_job_id,
                run_id=run.run_id,
                action_id=action.action_id,
                kubernetes_namespace=self.settings.kubernetes_namespace,
                status=JobStatus.QUEUED,
                requested_resources=spec.resources,
                evaluation_contract_id=spec.evaluation_contract_id,
                evaluation_contract_version=spec.evaluation_contract_version,
                evaluation_contract_digest=spec.evaluation_contract_digest,
                idempotency_key=spec.idempotency_key,
                variant_name=spec.variant_name,
                seed=spec.seed,
                spec=spec,
            )
            stored, created = self.store.create_job_if_absent(record)
            if created:
                self._event(
                    run.run_id,
                    source='orchestrator',
                    event_type='job.queued',
                    payload={
                        'job_id': stored.job_id,
                        'variant_name': stored.variant_name,
                        'seed': stored.seed,
                    },
                )
        self._transition(run.run_id, RunState.JOB_QUEUED)
        self._fill_job_capacity(run.run_id)

    def _fill_job_capacity(self, run_id: str) -> None:
        run = self.store.get_run(run_id)
        capacity_jobs = self.store.list_jobs(
            run_id,
            statuses={
                JobStatus.QUEUED,
                JobStatus.SUBMITTING,
                JobStatus.RUNNING,
                JobStatus.UNKNOWN,
            },
        )
        active = [
            job
            for job in capacity_jobs
            if job.status != JobStatus.QUEUED or job.external_run_id
        ]
        slots = max(0, run.maximum_parallel_jobs - len(active))
        queued = [
            job
            for job in capacity_jobs
            if job.status == JobStatus.QUEUED and not job.external_run_id
        ]
        for job in queued[:slots]:
            submitting = self.store.update_job(
                job.model_copy(update={'status': JobStatus.SUBMITTING})
            )
            try:
                submission = self.cluster.submit(submitting.spec)
                status = (
                    submission.status
                    if submission.status in {JobStatus.QUEUED, JobStatus.RUNNING}
                    else JobStatus.RUNNING
                )
                updated = self.store.update_job(
                    submitting.model_copy(
                        update={
                            'status': status,
                            'external_run_id': submission.external_run_id,
                            'job_name': submission.job_name,
                            'kubernetes_uid': submission.kubernetes_uid,
                        }
                    )
                )
                self._event(
                    run_id,
                    source='cluster',
                    event_type='job.submitted',
                    payload={
                        'job_id': updated.job_id,
                        'external_run_id': updated.external_run_id,
                        'job_name': updated.job_name,
                        'contract_digest': updated.evaluation_contract_digest,
                    },
                )
            except Exception as exc:
                self.store.update_job(
                    submitting.model_copy(
                        update={
                            'status': JobStatus.FAILED,
                            'exit_information': {'submission_error': str(exc)},
                        }
                    )
                )
                self._event(
                    run_id,
                    source='cluster',
                    event_type='job.failed',
                    payload={
                        'job_id': submitting.job_id,
                        'phase': 'submission',
                        'error': str(exc),
                    },
                )
        run = self.store.get_run(run_id)
        active = self.store.list_jobs(
            run_id,
            statuses={
                JobStatus.QUEUED,
                JobStatus.SUBMITTING,
                JobStatus.RUNNING,
                JobStatus.UNKNOWN,
            },
        )
        if active and run.state == RunState.JOB_QUEUED:
            self._transition(run_id, RunState.JOB_RUNNING)
        elif not active and run.state in {
            RunState.JOB_QUEUED,
            RunState.JOB_RUNNING,
        }:
            self._transition(run_id, RunState.BEAKER_ANALYZING)
            self._analyze_results(run_id)

    def reconcile_run(self, run_id: str) -> RunRecord:
        with self._advance_lock:
            run = self.store.get_run(run_id)
            if run.state not in {RunState.JOB_QUEUED, RunState.JOB_RUNNING}:
                return run
            jobs = self.store.list_jobs(
                run_id,
                statuses={
                    JobStatus.QUEUED,
                    JobStatus.SUBMITTING,
                    JobStatus.RUNNING,
                    JobStatus.UNKNOWN,
                },
            )
            for job in jobs:
                if job.status == JobStatus.QUEUED and not job.external_run_id:
                    continue
                if not job.external_run_id:
                    try:
                        submission = self.cluster.submit(job.spec)
                    except Exception as exc:
                        self.store.update_job(
                            job.model_copy(
                                update={
                                    'status': JobStatus.UNKNOWN,
                                    'exit_information': {
                                        'recovery_submission_error': str(exc)
                                    },
                                }
                            )
                        )
                        continue
                    job = self.store.update_job(
                        job.model_copy(
                            update={
                                'status': submission.status,
                                'external_run_id': submission.external_run_id,
                                'job_name': submission.job_name,
                                'kubernetes_uid': submission.kubernetes_uid,
                            }
                        )
                    )
                    self._event(
                        run_id,
                        source='cluster',
                        event_type='job.submitted',
                        payload={
                            'job_id': job.job_id,
                            'external_run_id': job.external_run_id,
                            'recovered': True,
                        },
                    )
                try:
                    snapshot = self.cluster.inspect(job.external_run_id)
                except Exception as exc:
                    self.store.update_job(
                        job.model_copy(
                            update={
                                'status': JobStatus.UNKNOWN,
                                'exit_information': {'inspection_error': str(exc)},
                            }
                        )
                    )
                    continue
                if snapshot.status == job.status:
                    continue
                updated = self.store.update_job(
                    job.model_copy(
                        update={
                            'status': snapshot.status,
                            'exit_information': snapshot.exit_information,
                        }
                    )
                )
                if snapshot.status in JOB_TERMINAL_STATUSES:
                    recorded_artifacts: list[ArtifactRecord] = []
                    for artifact in snapshot.artifacts:
                        artifact_id = uuid5(
                            NAMESPACE_URL,
                            (
                                f'{job.job_id}:{artifact.uri}:'
                                f'{artifact.sha256}'
                            ),
                        ).hex
                        recorded = self.store.save_artifact(
                            ArtifactRecord(
                                artifact_id=artifact_id,
                                run_id=run_id,
                                job_id=job.job_id,
                                type=artifact.type,
                                uri=artifact.uri,
                                sha256=artifact.sha256,
                                metadata={
                                    **artifact.metadata,
                                    'evaluation_contract_id': (
                                        job.evaluation_contract_id
                                    ),
                                    'evaluation_contract_version': (
                                        job.evaluation_contract_version
                                    ),
                                    'evaluation_contract_digest': (
                                        job.evaluation_contract_digest
                                    ),
                                },
                            )
                        )
                        recorded_artifacts.append(recorded)
                        self._event(
                            run_id,
                            source='cluster',
                            event_type='artifact.recorded',
                            payload={
                                'job_id': job.job_id,
                                'uri': artifact.uri,
                                'sha256': artifact.sha256,
                            },
                        )
                    event_type = (
                        'job.completed'
                        if updated.status == JobStatus.SUCCEEDED
                        else 'job.failed'
                    )
                    self._event(
                        run_id,
                        source='cluster',
                        event_type=event_type,
                        payload={
                            'job_id': updated.job_id,
                            'status': updated.status.value,
                            'exit_information': updated.exit_information,
                        },
                    )
                    if updated.status == JobStatus.SUCCEEDED:
                        self._generate_analysis_notebook(
                            run_id=run_id,
                            job=updated,
                            source_artifacts=recorded_artifacts,
                        )
                elif snapshot.status == JobStatus.RUNNING:
                    self._event(
                        run_id,
                        source='cluster',
                        event_type='job.started',
                        payload={'job_id': updated.job_id},
                    )
            self._fill_job_capacity(run_id)
            return self.store.get_run(run_id)

    def _generate_analysis_notebook(
        self,
        *,
        run_id: str,
        job: JobRecord,
        source_artifacts: list[ArtifactRecord],
    ) -> None:
        run = self.store.get_run(run_id)
        destination = Path(run.reports_path) / 'analysis.ipynb'
        try:
            digest, provenance = write_analysis_notebook(
                destination=destination,
                run_id=run_id,
                job_id=job.job_id,
                artifacts=source_artifacts,
                shared_mount_root=self.settings.shared_mount_root,
            )
        except (ArtifactDeliveryError, OSError, ValueError) as exc:
            self._event(
                run_id,
                source='orchestrator',
                event_type='artifact.generation_failed',
                payload={
                    'type': 'analysis_notebook',
                    'job_id': job.job_id,
                    'error': str(exc),
                },
            )
            return

        uri = f'artifact://{run_id}/reports/analysis.ipynb'
        artifact = self._save_local_artifact(
            run_id=run_id,
            artifact_type='analysis_notebook',
            uri=uri,
            digest=digest,
            metadata={
                'path': str(destination),
                'job_id': job.job_id,
                'derived_from': provenance,
                'authoritative': False,
            },
        )
        self._event(
            run_id,
            source='orchestrator',
            event_type='artifact.recorded',
            payload={
                'artifact_id': artifact.artifact_id,
                'type': artifact.type,
                'uri': artifact.uri,
                'path': str(destination),
                'sha256': artifact.sha256,
                'job_id': job.job_id,
                'authoritative': False,
            },
        )

    def _evidence_snapshot(
        self,
        run_id: str,
        phase: EvidencePhase = EvidencePhase.ANALYSIS,
    ) -> dict[str, Any]:
        """Bounded evidence summary for an agent turn, scoped by phase."""
        return build_evidence_snapshot(
            self.settings, self.store, run_id, phase=phase
        )

    def _analyze_results(self, run_id: str) -> None:
        evidence = self._evidence_snapshot(
            run_id, phase=EvidencePhase.ANALYSIS
        )
        _, result = self._run_agent_turn(
            run_id=run_id,
            agent=AgentName.BEAKER,
            prompt=(
                'Analyze the authoritative job and artifact records below. A '
                'failed job is an observation to explain, not proof that the '
                'research run failed. Cite evidence URIs for every material '
                'claim.\n\n'
                + serialize_evidence(evidence)
            ),
            expected_kind=TurnKind.EXPERIMENT_ANALYSIS,
            input_event=evidence,
        )
        self._record_requested_actions(
            run_id=run_id,
            agent=AgentName.BEAKER,
            result=result,
            turn_number=self.store.get_run(run_id).turn_number,
        )
        self._transition(run_id, RunState.HONEYDEW_VERIFYING)
        self._verify_results(run_id)

    def _verify_results(self, run_id: str) -> None:
        evidence = self._evidence_snapshot(
            run_id, phase=EvidencePhase.VERIFICATION
        )
        _, result = self._run_agent_turn(
            run_id=run_id,
            agent=AgentName.HONEYDEW,
            prompt=(
                'Independently verify Beaker\'s important claims against these '
                'authoritative records and the approved program.md. Set '
                'done=true only if the evidence supports a final report. Cite '
                'artifact, job, event, Git, or contract URIs.\n\n'
                + serialize_evidence(evidence)
            ),
            expected_kind=TurnKind.VERIFICATION,
            input_event=evidence,
        )
        if not result.done:
            self.store.reset_methodology_revision_budget(
                run_id,
                reason=(
                    'new cluster evidence requires a post-execution repair '
                    'cycle'
                ),
            )
            self._publish_latest(run_id)
            self._transition(run_id, RunState.BEAKER_REVISING)
            self._beaker_revise(
                run_id,
                feedback=result.message_to_other_agent or result.summary,
            )
            return
        self._transition(run_id, RunState.HONEYDEW_WRITING_REPORT)
        self._write_report(run_id)

    def _write_report(self, run_id: str, feedback: str | None = None) -> None:
        evidence = self._evidence_snapshot(run_id, phase=EvidencePhase.REPORT)
        prompt = (
            'Write report.md for the human. Separate observations from '
            'inferences, cite authoritative evidence URIs, include failed runs '
            'and limitations, and do not overstate single-run results. Create '
            'report.md yourself as a new file at the top level of your own '
            'workspace using the workspace file tool before returning the '
            'structured result. The produced_files entry with purpose '
            '"report" must name that newly created file, which must exist as '
            'a real file in your own workspace when the turn ends; do not '
            'reference job artifacts or files from other locations as your '
            'produced file.\n\n'
            + serialize_evidence(evidence)
        )
        if feedback:
            prompt += f'\n\nHuman rejection feedback:\n{feedback}'
        turn, result = self._run_agent_turn(
            run_id=run_id,
            agent=AgentName.HONEYDEW,
            prompt=prompt,
            expected_kind=TurnKind.FINAL_REPORT,
            input_event={'evidence': evidence, 'feedback': feedback},
        )
        report_files = [
            item for item in result.produced_files if item.purpose == 'report'
        ]
        if len(report_files) != 1:
            raise WorkflowError('Honeydew must produce exactly one report file')
        destination, digest = self.workspaces.copy_agent_output(
            run_id=run_id,
            agent=AgentName.HONEYDEW,
            relative_path=report_files[0].path,
            destination_kind='report',
        )
        uri = f'artifact://{run_id}/reports/report.md'
        self._save_local_artifact(
            run_id=run_id,
            artifact_type='report',
            uri=uri,
            digest=digest,
            metadata={
                'path': str(destination),
                'turn_id': turn.turn_id,
            },
        )
        self._event(
            run_id,
            source='honeydew',
            event_type='report.created',
            payload={
                'uri': uri,
                'path': str(destination),
                'sha256': digest,
                'turn_id': turn.turn_id,
            },
        )
        self._create_human_action(
            run_id=run_id,
            action_type='accept_final_report',
            reason='Human acceptance is required to complete the research run.',
        )
        self._transition(run_id, RunState.AWAITING_FINAL_ACCEPTANCE)

    def pause_run(
        self,
        run_id: str,
        *,
        requested_by: str | None = None,
        reason: str | None = None,
    ) -> RunRecord:
        # This deliberately does not take the advancement lock: pause must be
        # able to abort a model turn that currently owns that lock.
        run = self.store.get_run(run_id)
        if run.state in TERMINAL_STATES or run.state == RunState.PAUSED:
            return run
        self._abort_agent_turns(run)
        now = utc_now()
        active_runtime = run.active_runtime_seconds
        if run.active_since is not None:
            active_runtime += max(
                0.0,
                (now - run.active_since).total_seconds(),
            )
        paused = self._transition(
            run_id,
            RunState.PAUSED,
            updates={
                'resume_state': run.state,
                'active_runtime_seconds': active_runtime,
                'active_since': None,
            },
        )
        self._event(
            run_id,
            source='orchestrator',
            event_type='run.paused',
            payload={
                'resume_state': run.state.value,
                'requested_by': requested_by,
                'reason': reason,
            },
        )
        return paused

    def resume_run(
        self,
        run_id: str,
        *,
        requested_by: str | None = None,
        reason: str | None = None,
    ) -> RunRecord:
        with self._advance_lock:
            run = self.store.get_run(run_id)
            if run.state != RunState.PAUSED or run.resume_state is None:
                raise WorkflowError('run is not resumable')
            self._rotate_failed_session_before_resume(run)
            run = self.store.get_run(run_id)
            target = run.resume_state
            resumed = self._transition(
                run_id,
                target,
                updates={
                    'resume_state': None,
                    'active_since': utc_now(),
                },
            )
            self.workspaces.seed_agent_context(run_id)
            self._materialize_objective_datasets(run_id)
            self._event(
                run_id,
                source='orchestrator',
                event_type='run.resumed',
                payload={
                    'state': target.value,
                    'requested_by': requested_by,
                    'reason': reason,
                },
            )
            try:
                self._recover_run(run_id)
            except Exception as exc:
                self.pause_run(
                    run_id,
                    requested_by='orchestrator',
                    reason=f'Resumed workflow failed: {exc}',
                )
                raise
            return self.store.get_run(run_id)

    def _rotate_failed_session_before_resume(self, run: RunRecord) -> None:
        phase: dict[RunState, tuple[AgentName, TurnKind]] = {
            RunState.HONEYDEW_DRAFTING_PROTOCOL: (
                AgentName.HONEYDEW,
                TurnKind.PROTOCOL_DRAFT,
            ),
            RunState.BEAKER_DRAFTING_CONTRACT: (
                AgentName.BEAKER,
                TurnKind.CONTRACT_CANDIDATE,
            ),
            RunState.HONEYDEW_REVIEWING_CONTRACT: (
                AgentName.HONEYDEW,
                TurnKind.METHODOLOGY_REVIEW,
            ),
            RunState.BEAKER_PLANNING: (
                AgentName.BEAKER,
                TurnKind.IMPLEMENTATION_PLAN,
            ),
            RunState.BEAKER_IMPLEMENTING: (
                AgentName.BEAKER,
                TurnKind.IMPLEMENTATION_PROPOSAL,
            ),
            RunState.BEAKER_FINALIZING: (
                AgentName.BEAKER,
                TurnKind.IMPLEMENTATION_PROPOSAL,
            ),
            RunState.HONEYDEW_REVIEWING: (
                AgentName.HONEYDEW,
                TurnKind.METHODOLOGY_REVIEW,
            ),
            RunState.BEAKER_REVISING: (
                AgentName.BEAKER,
                TurnKind.REVISION,
            ),
            RunState.BEAKER_ANALYZING: (
                AgentName.BEAKER,
                TurnKind.EXPERIMENT_ANALYSIS,
            ),
            RunState.HONEYDEW_VERIFYING: (
                AgentName.HONEYDEW,
                TurnKind.VERIFICATION,
            ),
            RunState.HONEYDEW_WRITING_REPORT: (
                AgentName.HONEYDEW,
                TurnKind.FINAL_REPORT,
            ),
        }
        selected = phase.get(run.resume_state)
        if selected is None:
            return
        agent, expected_kind = selected
        session_id = (
            run.honeydew_session_id
            if agent == AgentName.HONEYDEW
            else run.beaker_session_id
        )
        if session_id is None:
            return
        failed_turns = [
            turn
            for turn in self.store.list_turns(run.run_id)
            if turn.agent == agent and turn.status == 'failed'
        ]
        if not failed_turns or failed_turns[-1].opencode_session_id != session_id:
            return
        self._rotate_agent_session(
            run_id=run.run_id,
            agent=agent,
            expected_kind=expected_kind,
            error=(
                'resume detected a failed turn still attached to the active '
                'OpenCode session'
            ),
        )

    def _abort_agent_turns(self, run: RunRecord) -> None:
        if run.honeydew_session_id:
            self.runtime.abort(
                run_id=run.run_id,
                agent=AgentName.HONEYDEW,
                session_id=run.honeydew_session_id,
            )
        if run.beaker_session_id:
            self.runtime.abort(
                run_id=run.run_id,
                agent=AgentName.BEAKER,
                session_id=run.beaker_session_id,
            )

    def cancel_run(
        self,
        run_id: str,
        *,
        requested_by: str | None = None,
        reason: str | None = None,
    ) -> RunRecord:
        # Like pause, cancellation must not wait for an active model turn.
        run = self.store.get_run(run_id)
        if run.state in TERMINAL_STATES:
            return run
        self._abort_agent_turns(run)
        cancellation_errors: list[str] = []
        for job in self.store.list_jobs(
            run_id,
            statuses={
                JobStatus.QUEUED,
                JobStatus.SUBMITTING,
                JobStatus.RUNNING,
                JobStatus.UNKNOWN,
            },
        ):
            if job.external_run_id:
                try:
                    self.cluster.cancel(job.external_run_id)
                except Exception as exc:
                    cancellation_errors.append(f'{job.job_id}: {exc}')
                    continue
            self.store.update_job(
                job.model_copy(
                    update={
                        'status': JobStatus.CANCELLED,
                        'exit_information': {
                            **job.exit_information,
                            'cancel_requested': True,
                        },
                    }
                )
            )
        if cancellation_errors:
            self._event(
                run_id,
                source='orchestrator',
                event_type='run.cancellation_failed',
                payload={
                    'cancellation_errors': cancellation_errors,
                    'requested_by': requested_by,
                    'reason': reason,
                },
            )
            raise WorkflowError(
                'cancellation could not be confirmed for every external job'
            )
        cancelled = self._transition(
            run_id,
            RunState.CANCELLED,
            payload={
                'cancellation_errors': cancellation_errors,
                'requested_by': requested_by,
                'reason': reason,
            },
        )
        self._event(
            run_id,
            source='orchestrator',
            event_type='run.cancelled',
            payload={
                'cancellation_errors': cancellation_errors,
                'requested_by': requested_by,
                'reason': reason,
            },
        )
        return cancelled

    def recover(self) -> list[str]:
        for run in self.store.list_runs():
            self._backfill_local_artifacts(run.run_id)
        recovered: list[str] = []
        for run in self.store.list_active_runs():
            # A retry child can survive a Discord outage during its initial
            # attach. On recovery, attach a fresh thread and project the last
            # durable event; Discord remains non-authoritative throughout.
            if run.parent_run_id and not run.discord_thread_id:
                attached = self._attach_discord_thread(run.run_id)
                if attached.discord_thread_id:
                    if not self._republish_pending_approval(run.run_id):
                        self._publish_latest(run.run_id)
            running_turns = [
                turn
                for turn in self.store.list_turns(run.run_id)
                if turn.status == 'running'
            ]
            interrupted = self.store.mark_running_turns_interrupted(run.run_id)
            if interrupted:
                self._event(
                    run.run_id,
                    source='orchestrator',
                    event_type='run.recovered',
                    payload={'interrupted_turns': interrupted},
                )
                started_events = {
                    str(event.payload.get('turn_id')): event
                    for event in self.store.list_events(run.run_id)
                    if event.event_type == 'agent.turn_started'
                }
                for turn in running_turns:
                    event = started_events.get(turn.turn_id)
                    raw_kind = (
                        event.payload.get('kind')
                        if event is not None
                        else TurnKind.IMPLEMENTATION_PROPOSAL.value
                    )
                    try:
                        expected_kind = TurnKind(str(raw_kind))
                    except ValueError:
                        expected_kind = TurnKind.IMPLEMENTATION_PROPOSAL
                    self._rotate_agent_session(
                        run_id=run.run_id,
                        agent=turn.agent,
                        expected_kind=expected_kind,
                        error=(
                            'orchestrator restarted during active agent turn'
                        ),
                    )
            try:
                self._recover_run(run.run_id)
            except Exception as exc:
                current = self.store.get_run(run.run_id)
                resumable_agent_states = {
                    RunState.HONEYDEW_DRAFTING_PROTOCOL,
                    RunState.BEAKER_DRAFTING_CONTRACT,
                    RunState.HONEYDEW_REVIEWING_CONTRACT,
                    RunState.BEAKER_PLANNING,
                    RunState.BEAKER_IMPLEMENTING,
                    RunState.BEAKER_FINALIZING,
                    RunState.HONEYDEW_REVIEWING,
                    RunState.BEAKER_REVISING,
                    RunState.BEAKER_ANALYZING,
                    RunState.HONEYDEW_VERIFYING,
                    RunState.HONEYDEW_WRITING_REPORT,
                }
                if current.state in resumable_agent_states:
                    self._event(
                        run.run_id,
                        source='orchestrator',
                        event_type='run.recovery_failed',
                        payload={
                            'state': current.state.value,
                            'error': str(exc),
                            'next_step': (
                                'The run is paused at the same phase. Resume '
                                'to retry the bounded agent turn.'
                            ),
                        },
                    )
                    self.pause_run(run.run_id)
                else:
                    self._fail_run(run.run_id, exc)
            recovered.append(run.run_id)
        return recovered

    def _backfill_local_artifacts(self, run_id: str) -> None:
        for event in self.store.list_events(run_id):
            payload = event.payload
            artifact_type = None
            if (
                event.event_type == 'artifact.recorded'
                and payload.get('type') == 'protocol'
            ):
                artifact_type = 'protocol'
            elif (
                event.event_type == 'artifact.recorded'
                and payload.get('type') == 'evaluation_contract_proposal'
            ):
                artifact_type = 'evaluation_contract_proposal'
            elif event.event_type == 'report.created':
                artifact_type = 'report'
            if artifact_type is None:
                continue
            uri = payload.get('uri')
            digest = payload.get('sha256')
            if not isinstance(uri, str) or not isinstance(digest, str):
                continue
            self._save_local_artifact(
                run_id=run_id,
                artifact_type=artifact_type,
                uri=uri,
                digest=digest,
                metadata={
                    key: value
                    for key, value in payload.items()
                    if key not in {'type', 'uri', 'sha256'}
                },
            )

    def _recover_run(self, run_id: str) -> None:
        run = self.store.get_run(run_id)
        state = run.state
        contract_sensitive_states = {
            RunState.BEAKER_PLANNING,
            RunState.BEAKER_IMPLEMENTING,
            RunState.BEAKER_FINALIZING,
            RunState.HONEYDEW_REVIEWING,
            RunState.BEAKER_REVISING,
            RunState.AWAITING_EXECUTION_APPROVAL,
        }
        if state in contract_sensitive_states and not self._contract_binding_compatible(
            run_id
        ):
            for action in self.store.list_actions(run_id):
                if (
                    action.type == 'submit_experiment_matrix'
                    and action.approval_status == ApprovalStatus.PENDING
                ):
                    self.store.update_action(
                        action.action_id,
                        approval_status=ApprovalStatus.REJECTED,
                        reviewer='orchestrator',
                        reason=(
                            'The approved protocol requires a different '
                            'evaluation contract.'
                        ),
                    )
            self._transition(run_id, RunState.BEAKER_DRAFTING_CONTRACT)
            self._beaker_draft_contract(
                run_id,
                feedback=(
                    'Recovery detected that the installed evaluation contract '
                    'does not implement the approved protocol. Draft the '
                    'required immutable contract candidate before continuing.'
                ),
            )
            return
        if state == RunState.PREPARING:
            if run.parent_run_id and run.retry_checkpoint_digest:
                self._resume_terminal_retry(run_id)
                return
            self._transition(run_id, RunState.HONEYDEW_DRAFTING_PROTOCOL)
            self._draft_protocol(run_id)
        elif state == RunState.HONEYDEW_DRAFTING_PROTOCOL:
            self._draft_protocol(run_id)
        elif state == RunState.BEAKER_DRAFTING_CONTRACT:
            self._beaker_draft_contract(run_id)
        elif state == RunState.HONEYDEW_REVIEWING_CONTRACT:
            candidates = [
                artifact
                for artifact in self.store.list_artifacts(run_id)
                if artifact.type == 'evaluation_contract_candidate'
            ]
            if not candidates:
                self._transition(run_id, RunState.BEAKER_DRAFTING_CONTRACT)
                self._beaker_draft_contract(
                    run_id,
                    feedback='The sealed contract candidate was not recoverable.',
                )
                return
            artifact = candidates[-1]
            action = self.store.get_action(str(artifact.metadata['action_id']))
            self._honeydew_review_contract(
                run_id,
                action=action,
                artifact=artifact,
            )
        elif state == RunState.AWAITING_CONTRACT_PROMOTION:
            approved = [
                action
                for action in self.store.list_actions(run_id)
                if action.type == 'propose_evaluation_contract'
                and action.approval_status == ApprovalStatus.APPROVED
            ]
            if approved:
                self._promote_contract_candidate(approved[-1])
        elif state == RunState.BEAKER_PLANNING:
            self._beaker_plan(run_id)
        elif state == RunState.BEAKER_IMPLEMENTING:
            pending = [
                action
                for action in self.store.list_actions(run_id)
                if action.type == 'submit_experiment_matrix'
                and action.approval_status == ApprovalStatus.PENDING
            ]
            if pending:
                self._transition(run_id, RunState.HONEYDEW_REVIEWING)
                self._honeydew_review(
                    run_id,
                    implementation_turn_id='recovered',
                )
            elif self._has_imported_task_implementation(run):
                self._transition(run_id, RunState.BEAKER_FINALIZING)
                self._beaker_finalize(run_id)
            else:
                self._beaker_implement(run_id)
        elif state == RunState.BEAKER_FINALIZING:
            self._beaker_finalize(run_id)
        elif state == RunState.HONEYDEW_REVIEWING:
            self._honeydew_review(
                run_id,
                implementation_turn_id='recovered',
            )
        elif state == RunState.BEAKER_REVISING:
            rejected = [
                action
                for action in self.store.list_actions(run_id)
                if action.type == 'submit_experiment_matrix'
                and action.approval_status == ApprovalStatus.REJECTED
            ]
            feedback = rejected[-1].reason if rejected else 'Resume the bounded revision.'
            self._beaker_revise(run_id, feedback=feedback)
        elif state == RunState.AWAITING_EXECUTION_APPROVAL:
            matrix_actions = [
                action
                for action in self.store.list_actions(run_id)
                if action.type == 'submit_experiment_matrix'
            ]
            if (
                matrix_actions
                and matrix_actions[-1].approval_status
                == ApprovalStatus.APPROVED
            ):
                self._submit_matrix(matrix_actions[-1])
        elif state in {RunState.JOB_QUEUED, RunState.JOB_RUNNING}:
            self.reconcile_run(run_id)
        elif state == RunState.BEAKER_ANALYZING:
            self._analyze_results(run_id)
        elif state == RunState.HONEYDEW_VERIFYING:
            self._verify_results(run_id)
        elif state == RunState.HONEYDEW_WRITING_REPORT:
            self._write_report(run_id)
        elif state == RunState.AWAITING_FINAL_ACCEPTANCE:
            approved = [
                action
                for action in self.store.list_actions(run_id)
                if action.type == 'accept_final_report'
                and action.approval_status == ApprovalStatus.APPROVED
            ]
            if approved:
                self._resume_approved_action(approved[-1])

    def _fail_run(self, run_id: str, exc: Exception) -> None:
        run = self.store.get_run(run_id)
        if run.state in TERMINAL_STATES or run.state == RunState.PAUSED:
            return
        try:
            self._transition(
                run_id,
                RunState.FAILED,
                payload={'error': str(exc)},
            )
        except (ConcurrencyConflict, ValueError):
            return
        self._event(
            run_id,
            source='orchestrator',
            event_type='run.failed',
            payload={'error': str(exc)},
        )
