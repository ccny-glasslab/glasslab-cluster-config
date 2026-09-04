"""Scripted test runtime over the same AgentRuntime surface as production.

The smoke path and tests drive this class through the exact same
ensure_session/run_turn/abort/release call sites the real OpenCode runtime
uses. Prompts are matched by substring, and every branch writes the workspace
files it claims to have produced, mirroring how the real runtime materializes
files so downstream copy/freeze/packaging code sees the same shape.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from uuid import uuid4

from .opencode_runtime import AgentRuntime, RuntimeSession
from .schemas import (
    AgentName,
    AgentTurnResult,
    Citation,
    Claim,
    EvaluationContractProposal,
    ProducedFile,
    RequestedAction,
    ResearchAnswer,
    RunState,
    TaskSpecProposal,
    TurnKind,
)


class ScriptedMockRuntime(AgentRuntime):
    """Deterministic local runtime used only by tests and the smoke path."""

    def __init__(self, *, runner_image: str) -> None:
        self.runner_image = runner_image
        self.sessions: dict[tuple[str, AgentName], RuntimeSession] = {}
        self.turn_counts: defaultdict[AgentName, int] = defaultdict(int)
        self.aborted: list[tuple[str, AgentName, str]] = []
        self.released: list[tuple[str, AgentName]] = []
        self.prompts: list[tuple[AgentName, str]] = []

    def ensure_session(
        self,
        *,
        run_id: str,
        agent: AgentName,
        workspace: Path,
        existing_session_id: str | None,
    ) -> RuntimeSession:
        key = (run_id, agent)
        session = self.sessions.get(key)
        if session is None:
            # Sessions are keyed per (run, agent) and an existing_session_id is
            # honored, matching the real runtime's recovery semantics.
            session = RuntimeSession(
                runtime_id=f'mock-runtime-{agent.value}-{run_id[:8]}',
                session_id=existing_session_id
                or f'mock-session-{agent.value}-{uuid4().hex[:12]}',
            )
            self.sessions[key] = session
        return session

    def run_turn(
        self,
        *,
        run_id: str,
        agent: AgentName,
        workspace: Path,
        session_id: str,
        prompt: str,
    ) -> tuple[AgentTurnResult, str | None]:
        self.turn_counts[agent] += 1
        self.prompts.append((agent, prompt))
        message_id = f'mock-message-{uuid4().hex[:12]}'
        if agent == AgentName.HONEYDEW and 'research question' in prompt:
            return (
                AgentTurnResult(
                    kind=TurnKind.RESEARCH_ANSWER,
                    summary='Answered the research question from the corpus.',
                    research_answer=ResearchAnswer(
                        answer='Conformal prediction provides coverage '
                        'guarantees by construction on calibration data.',
                        citations=[
                            Citation(
                                knowledge_uri=(
                                    'knowledge://context/'
                                    '00000000000000000000000000000000'
                                ),
                                source='conformal-prediction-nixtla',
                                excerpt='Setting up Conformal Intervals '
                                'Parameter Requirements',
                            )
                        ],
                    ),
                    done=True,
                ),
                message_id,
            )
        if agent == AgentName.BEAKER and 'Write implementation-plan.md' in prompt:
            (workspace / 'implementation-plan.md').write_text(
                '# Implementation Plan\n\n'
                '1. Inspect the task inputs and existing repository boundaries.\n'
                '2. Implement the smallest complete experiment runner.\n'
                '3. Run lightweight local checks and propose the bounded matrix.\n'
            )
            return (
                AgentTurnResult(
                    kind=TurnKind.IMPLEMENTATION_PLAN,
                    summary='Planned a bounded task-specific implementation.',
                    produced_files=[
                        ProducedFile(
                            path='implementation-plan.md',
                            purpose='implementation',
                        )
                    ],
                    done=True,
                ),
                message_id,
            )
        if agent == AgentName.HONEYDEW and 'Compile this task archive' in prompt:
            return (
                AgentTurnResult(
                    kind=TurnKind.TASK_SPEC,
                    summary='Compiled the supplied task into a bounded CPU profile.',
                    task_spec_proposal=TaskSpecProposal(
                        schema_version='glasslab-task-spec-v1',
                        display_name='Generic Test Classification',
                        runtime_profile='cpu-ml-standard-v1',
                        assets=[],
                        required_artifacts=['tables/metrics.csv'],
                        required_metric_keys=['accuracy'],
                        missing_inputs=[],
                        rationale='The supplied task is a small tabular workload.',
                    ),
                    done=True,
                ),
                message_id,
            )
        if agent == AgentName.HONEYDEW and 'Draft a concrete program.md' in prompt:
            generic_task = 'generic-task-integrity-v1' in prompt
            (workspace / 'program.md').write_text(
                '# Program\n\n'
                '## Hypothesis\n\n'
                'The bounded candidate will pass the immutable evaluator.\n\n'
                '## Controls\n\n'
                '- Fixed evaluation contract\n'
                '- Fixed seed matrix\n'
                '- Required metrics and evaluation artifacts\n\n'
                '## Stopping condition\n\n'
                'Stop after the approved matrix finishes.\n'
            )
            return (
                AgentTurnResult(
                    kind=TurnKind.PROTOCOL_DRAFT,
                    summary='Drafted a bounded protocol.',
                    evaluation_contract_proposal=(
                        EvaluationContractProposal.model_validate(
                            {
                                'evaluator_type': (
                                    'generic-task-integrity-v1'
                                    if generic_task
                                    else 'example-research-v1'
                                ),
                                'primary_metric': {
                                    'name': (
                                        'rubric_score'
                                        if generic_task
                                        else 'score'
                                    ),
                                    'direction': 'maximize',
                                    'minimum_effect': 0.01,
                                },
                                'guardrails': [],
                                'required_artifacts': [
                                    'metrics.json',
                                    'evaluation.json',
                                ],
                                'budget_mode': 'wallclock',
                                'max_wallclock_minutes': 5,
                                'resource_constraints': {
                                    'cpu': 4 if generic_task else 1,
                                    'memory_gib': 8 if generic_task else 1,
                                    'gpus': 0,
                                    'wallclock_minutes': (
                                        60 if generic_task else 5
                                    ),
                                },
                                'rationale': (
                                    'Use the immutable smoke evaluator for '
                                    'the deterministic mock workflow.'
                                ),
                            }
                        )
                    ),
                    claims=[],
                    produced_files=[
                        ProducedFile(path='program.md', purpose='protocol')
                    ],
                    recommended_next_state=RunState.AWAITING_PROTOCOL_APPROVAL,
                    done=True,
                ),
                message_id,
            )
        if agent == AgentName.BEAKER and (
            'Implement the bounded' in prompt
            or 'Execute the task-specific plan' in prompt
            or 'Finalize the existing imported benchmark' in prompt
            or 'Revise the implementation' in prompt
        ):
            # The cluster executes objective worktrees via `python3 run.py`;
            # the mock mirrors that mandatory repo-root entrypoint.
            (workspace / 'run.py').write_text(
                'print("bounded mock run.py")\n'
            )
            (workspace / 'experiment.py').write_text(
                'print("bounded mock experiment")\n'
            )
            (workspace / 'configs').mkdir(exist_ok=True)
            (workspace / 'configs' / 'baseline.yaml').write_text(
                'learning_rate: 0.0001\n'
            )
            kind = (
                TurnKind.REVISION
                if 'Revise the implementation' in prompt
                else TurnKind.IMPLEMENTATION_PROPOSAL
            )
            return (
                AgentTurnResult(
                    kind=kind,
                    summary='Implemented and locally checked the bounded candidate.',
                    claims=[
                        Claim(
                            text='The local implementation file exists.',
                            evidence=['git://beaker/experiment.py'],
                        )
                    ],
                    requested_actions=[
                        RequestedAction(
                            type='submit_experiment_matrix',
                            arguments={
                                'base_config': 'configs/baseline.yaml',
                                'variants': [
                                    {
                                        'name': 'baseline',
                                        'overrides': {'learning_rate': 0.0001},
                                    },
                                    {
                                        'name': 'candidate',
                                        'overrides': {'learning_rate': 0.0003},
                                    },
                                ],
                                'seeds': [17],
                                'maximum_parallel_jobs': 2,
                                'runner_image': self.runner_image,
                                'resources': {
                                    'cpu': 1,
                                    'memory_gib': 1,
                                    'gpus': 0,
                                    'wallclock_minutes': 5,
                                },
                                'required_artifacts': ['metrics.json'],
                            },
                            reason='Execute the reviewed bounded matrix.',
                        )
                    ],
                    recommended_next_state=RunState.HONEYDEW_REVIEWING,
                    done=True,
                ),
                message_id,
            )
        if agent == AgentName.HONEYDEW and 'Review Beaker' in prompt:
            return (
                AgentTurnResult(
                    kind=TurnKind.METHODOLOGY_REVIEW,
                    summary='Controls and comparison are acceptable for the smoke test.',
                    claims=[],
                    message_to_other_agent='Proceed with the reviewed matrix.',
                    recommended_next_state=RunState.AWAITING_EXECUTION_APPROVAL,
                    done=True,
                ),
                message_id,
            )
        if agent == AgentName.BEAKER and 'Analyze the authoritative' in prompt:
            return (
                AgentTurnResult(
                    kind=TurnKind.EXPERIMENT_ANALYSIS,
                    summary='The fake jobs completed and emitted metrics.',
                    claims=[
                        Claim(
                            text='The fake jobs emitted metrics.',
                            evidence=['artifact://fake/metrics.json'],
                        )
                    ],
                    recommended_next_state=RunState.HONEYDEW_VERIFYING,
                    done=True,
                ),
                message_id,
            )
        if agent == AgentName.HONEYDEW and 'Independently verify' in prompt:
            return (
                AgentTurnResult(
                    kind=TurnKind.VERIFICATION,
                    summary='The authoritative fake artifacts support a report.',
                    claims=[
                        Claim(
                            text='The job evidence is internally consistent.',
                            evidence=[],
                        )
                    ],
                    recommended_next_state=RunState.HONEYDEW_WRITING_REPORT,
                    done=True,
                ),
                message_id,
            )
        if agent == AgentName.HONEYDEW and 'Write report.md' in prompt:
            (workspace / 'report.md').write_text(
                '# Report\n\n'
                'The mock workflow completed. This demonstrates orchestration '
                'behavior only and is not scientific evidence from a real GPU run.\n'
            )
            return (
                AgentTurnResult(
                    kind=TurnKind.FINAL_REPORT,
                    summary='Prepared the evidence-bounded report.',
                    claims=[
                        Claim(
                            text='The mock workflow reached report generation.',
                            evidence=[],
                        )
                    ],
                    produced_files=[
                        ProducedFile(path='report.md', purpose='report')
                    ],
                    recommended_next_state=RunState.AWAITING_FINAL_ACCEPTANCE,
                    done=True,
                ),
                message_id,
            )
        # An unmatched prompt fails loudly: silently skipping a turn would let
        # a smoke-path regression pass while the scripted flow no longer
        # matches the intended journey.
        raise AssertionError(
            f'unexpected mock turn for {agent.value}: {prompt[:120]}'
        )

    def abort(self, *, run_id: str, agent: AgentName, session_id: str) -> None:
        self.aborted.append((run_id, agent, session_id))

    def release(self, *, run_id: str, agent: AgentName) -> None:
        # Mirror the real runtime's lifecycle: releasing drops the session just
        # as OpenCodeProcessRuntime stops its per-run process.
        self.released.append((run_id, agent))
        self.sessions.pop((run_id, agent), None)
