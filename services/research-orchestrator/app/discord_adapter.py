"""Discord transcript projection.

Discord is an interface, not authoritative memory or state. Every publish and
thread-creation call is guarded by the engine so a Discord outage can only
silence the transcript, never fail or block the workflow. Rendering is a
pure function over the append-only event log; the bot token is used only to
post, and approval decisions are persisted by the engine, not derived from
button clicks.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

from .schemas import EventRecord


@dataclass(frozen=True)
class DiscordMessage:
    identity: str
    content: str
    # Status messages are edited in place rather than posted anew so a run's
    # status line does not spam the thread; the status_message_id is persisted
    # on the run record by the engine.
    is_status: bool = False
    components: list[dict[str, Any]] | None = None


def _action_title(action_type: str) -> str:
    return {
        'approve_protocol': 'Approve research protocol',
        'submit_experiment_matrix': 'Approve cluster experiment matrix',
        'accept_final_report': 'Accept final research report',
        'propose_evaluation_contract': 'Promote evaluation contract',
    }.get(action_type, action_type.replace('_', ' ').capitalize())


def _button_label(action_type: str, arguments: dict[str, Any]) -> str:
    if action_type == 'approve_protocol':
        return 'Approve protocol'
    if action_type == 'accept_final_report':
        return 'Accept report'
    if action_type == 'submit_experiment_matrix':
        variants = arguments.get('variants', [])
        seeds = arguments.get('seeds', [])
        job_count = (
            len(variants) * len(seeds)
            if isinstance(variants, list) and isinstance(seeds, list)
            else 0
        )
        return f'Approve {job_count} jobs' if job_count else 'Approve matrix'
    if action_type == 'propose_evaluation_contract':
        return 'Promote contract'
    return 'Approve'


def _render_action_context(payload: dict[str, Any]) -> str:
    action_type = str(payload.get('type', 'action'))
    arguments = payload.get('arguments')
    if not isinstance(arguments, dict):
        arguments = {}
    ready = bool(payload.get('human_approval_ready', False))
    heading = (
        'Approval requested'
        if ready
        else 'Proposed action under methodology review'
    )
    lines = [
        f'**{heading}: {_action_title(action_type)}**',
        '',
        '**Research objective**',
        str(payload.get('objective', 'Not recorded.')),
    ]

    if action_type == 'approve_protocol':
        artifact = payload.get('artifact')
        if not isinstance(artifact, dict):
            artifact = {}
        lines.extend(
            [
                '',
                '**What you are reviewing**',
                (
                    f"Protocol v{payload.get('protocol_version', '?')} "
                    f"at `{artifact.get('uri', 'program.md')}` "
                    f"(SHA-256 `{str(artifact.get('sha256', 'unknown'))[:12]}...`)."
                ),
            ]
        )
        proposal = payload.get('contract_proposal')
        binding = payload.get('contract_binding')
        if isinstance(proposal, dict):
            primary = proposal.get('primary_metric')
            resources = proposal.get('resource_constraints')
            if not isinstance(primary, dict):
                primary = {}
            if not isinstance(resources, dict):
                resources = {}
            guardrails = proposal.get('guardrails')
            if not isinstance(guardrails, list):
                guardrails = []
            guardrail_names = [
                str(item.get('name'))
                for item in guardrails
                if isinstance(item, dict) and item.get('name')
            ]
            lines.extend(
                [
                    '',
                    "**Honeydew's evaluation contract proposal**",
                    (
                        f"Evaluator: `{proposal.get('evaluator_type', 'unknown')}`. "
                        f"Primary metric: `{primary.get('name', 'unknown')}` "
                        f"({primary.get('direction', '?')}, minimum effect "
                        f"{primary.get('minimum_effect', '?')})."
                    ),
                    (
                        f"Guardrails: {', '.join(guardrail_names) or 'none'}. "
                        f"Budget: {proposal.get('budget_mode', 'unknown')}."
                    ),
                    (
                        f"Ceiling: {resources.get('gpus', '?')} GPU, "
                        f"{resources.get('cpu', '?')} CPU, "
                        f"{resources.get('memory_gib', '?')} GiB RAM, "
                        f"{resources.get('wallclock_minutes', '?')} minutes."
                    ),
                ]
            )
        if isinstance(binding, dict):
            lines.extend(
                [
                    (
                        'Immutable harness binding: '
                        f"`{binding.get('contract_id', 'unknown')}` "
                        f"v{binding.get('version', '?')} "
                        f"(`{binding.get('status', 'unknown')}`)."
                    )
                ]
            )
    elif action_type == 'propose_evaluation_contract':
        candidate = payload.get('contract_candidate')
        artifact = payload.get('artifact')
        if not isinstance(candidate, dict):
            candidate = {}
        if not isinstance(artifact, dict):
            artifact = {}
        descriptor = candidate.get('descriptor')
        if not isinstance(descriptor, dict):
            descriptor = {}
        manifest = descriptor.get('manifest')
        if not isinstance(manifest, dict):
            manifest = {}
        lines.extend(
            [
                '',
                '**What you are reviewing**',
                (
                    f"Sealed contract `{candidate.get('contract_id', 'unknown')}` "
                    f"version `{candidate.get('version', 'unknown')}` "
                    f"(SHA-256 `{str(artifact.get('sha256', 'unknown'))[:12]}...`)."
                ),
                (
                    f"Primary metric: `{manifest.get('primary_metric', 'unknown')}` "
                    f"({manifest.get('primary_metric_direction', 'unknown')}). "
                    f"Honeydew approval: `{payload.get('honeydew_approved', False)}`."
                ),
                '',
                '**What approval does**',
                str(payload.get('effect', 'Promote the sealed contract.')),
            ]
        )
    elif action_type == 'submit_experiment_matrix':
        variants = arguments.get('variants', [])
        seeds = arguments.get('seeds', [])
        resources = arguments.get('resources', {})
        if not isinstance(variants, list):
            variants = []
        if not isinstance(seeds, list):
            seeds = []
        if not isinstance(resources, dict):
            resources = {}
        variant_names = [
            str(item.get('name'))
            for item in variants
            if isinstance(item, dict) and item.get('name')
        ]
        job_count = len(variant_names) * len(seeds)
        lines.extend(
            [
                '',
                '**Experiment scope**',
                (
                    f"{job_count} jobs: {', '.join(variant_names) or 'unnamed variants'} "
                    f"across seeds {', '.join(map(str, seeds)) or 'not recorded'}."
                ),
                (
                    f"Per job: {resources.get('gpus', '?')} GPU, "
                    f"{resources.get('cpu', '?')} CPU, "
                    f"{resources.get('memory_gib', '?')} GiB RAM, "
                    f"up to {resources.get('wallclock_minutes', '?')} minutes."
                ),
                (
                    f"Concurrency: up to "
                    f"{arguments.get('maximum_parallel_jobs', '?')} jobs. "
                    f"Image: `{arguments.get('runner_image', 'not recorded')}`."
                ),
            ]
        )
        preflight = payload.get('preflight')
        if isinstance(preflight, dict):
            checks = preflight.get('checks')
            comparisons = preflight.get('comparisons')
            decisions = preflight.get('decisions')
            errors = preflight.get('errors')
            if not isinstance(checks, list):
                checks = []
            if not isinstance(comparisons, dict):
                comparisons = {}
            if not isinstance(decisions, dict):
                decisions = {}
            if not isinstance(errors, list):
                errors = []
            methodology = [
                f"{name}=[{', '.join(map(str, values))}]"
                for name, values in {**comparisons, **decisions}.items()
                if isinstance(values, list)
            ]
            lines.extend(
                [
                    '',
                    '**Deterministic preflight**',
                    (
                        f"{'Passed' if preflight.get('passed') else 'Failed'}; "
                        f"{preflight.get('job_count', job_count)} job(s). "
                        f"{'; '.join(map(str, checks)) or 'No checks recorded.'}"
                    ),
                    (
                        'Methodology: '
                        + (', '.join(methodology) or 'no declared dimensions')
                    ),
                ]
            )
            if errors:
                lines.append('Blocking findings: ' + '; '.join(map(str, errors)))
    elif action_type == 'accept_final_report':
        artifact = payload.get('artifact')
        if not isinstance(artifact, dict):
            artifact = {}
        lines.extend(
            [
                '',
                '**Report being accepted**',
                (
                    f"`{artifact.get('uri', 'report.md')}` "
                    f"(SHA-256 `{str(artifact.get('sha256', 'unknown'))[:12]}...`)."
                ),
            ]
        )

    contract = payload.get('evaluation_contract')
    if isinstance(contract, dict):
        lines.extend(
            [
                '',
                '**Evaluation contract**',
                (
                    f"`{contract.get('contract_id', 'unknown')}` "
                    f"v{contract.get('version', '?')} "
                    f"(digest `{str(contract.get('digest', 'unknown'))[:12]}...`)."
                ),
            ]
        )
    lines.extend(
        [
            '',
            '**Why this gate exists**',
            str(payload.get('reason', 'Human review is required.')),
            '',
            '**Approval authorizes**',
            str(payload.get('effect', 'The stored action.')),
        ]
    )
    content = '\n'.join(lines)
    if len(content) <= 1900:
        return content
    # Discord caps a message at 2000 chars and the webhook path prefixes a
    # username; truncate below the cap so a rendered review is never rejected.
    return content[:1885].rstrip() + '\n...[details truncated]'


class DiscordRenderer:
    IDENTITIES = {
        'honeydew': 'Honeydew',
        'beaker': 'Beaker',
        'orchestrator': 'Orchestrator',
        'cluster': 'Orchestrator',
    }

    def render(self, event: EventRecord) -> DiscordMessage | None:
        identity = self.IDENTITIES.get(event.source, 'Orchestrator')
        event_type = event.event_type
        payload = event.payload
        if event_type == 'run.created':
            return DiscordMessage(
                'Orchestrator',
                f"Research run created: {payload.get('objective', '')}",
            )
        if event_type == 'run.retry_created':
            return DiscordMessage(
                'Orchestrator',
                'Terminal retry checkpoint created: '
                f"parent `{payload.get('parent_run_id')}`, child "
                f"`{payload.get('child_run_id')}`. The child requires fresh approvals.",
            )
        if event_type == 'run.state_changed':
            return DiscordMessage(
                'Orchestrator',
                f"State: {payload.get('from')} -> {payload.get('to')}",
                is_status=True,
            )
        if event_type == 'agent.turn_completed':
            content = str(payload.get('summary', 'Agent turn completed.'))
            handoff = str(payload.get('message_to_other_agent', '')).strip()
            if handoff:
                recipient = 'Beaker' if identity == 'Honeydew' else 'Honeydew'
                content = f'{content}\n\n**To {recipient}:** {handoff}'
            return DiscordMessage(
                identity,
                content,
            )
        if event_type in {
            'action.proposed',
            'action.human_approval_requested',
        }:
            components = None
            # Default to human-reviewable unless the policy explicitly marks
            # the action as requiring only Honeydew review first; buttons are
            # only rendered once the action is actually awaiting approval.
            human_approval_ready = bool(
                payload.get(
                    'human_approval_ready',
                    payload.get('policy_classification')
                    != 'honeydew_and_human_approval',
                )
            )
            if (
                payload.get('approval_status') == 'pending'
                and human_approval_ready
            ):
                action_id = str(payload.get('action_id', ''))
                arguments = payload.get('arguments')
                if not isinstance(arguments, dict):
                    arguments = {}
                components = [
                    {
                        'type': 1,
                        'components': [
                            {
                                'type': 2,
                                'style': 3,
                                'label': _button_label(
                                    str(payload.get('type', '')),
                                    arguments,
                                ),
                                'custom_id': (
                                    f'glasslab:approve:{action_id}'
                                ),
                            },
                            {
                                'type': 2,
                                'style': 4,
                                'label': 'Reject',
                                'custom_id': (
                                    f'glasslab:reject:{action_id}'
                                ),
                            },
                        ],
                    }
                ]
            return DiscordMessage(
                identity,
                _render_action_context(payload),
                components=components,
            )
        if event_type in {
            'action.approved',
            'action.rejected',
            'action.rejection_resumed',
        }:
            if event_type in {'action.rejected', 'action.rejection_resumed'}:
                heading = (
                    'Rejected action recovery resumed'
                    if event_type == 'action.rejection_resumed'
                    else 'Action rejected'
                )
                return DiscordMessage(
                    'Orchestrator',
                    '\n'.join(
                        [
                            f'**{heading}**',
                            '',
                            f"Action: `{payload.get('action_id')}`",
                            f"Reviewer: {payload.get('reviewer', 'unknown')}",
                            f"Revision requested: {payload.get('reason', 'none')}",
                            (
                                'The run will return to the responsible agent '
                                'for a revised proposal.'
                            ),
                        ]
                    ),
                )
            return DiscordMessage(
                'Orchestrator',
                f"{event_type}: {payload.get('action_id')}",
            )
        if event_type == 'action.execution_failed':
            jobs_created = int(payload.get('jobs_created', 0))
            artifacts_created = int(payload.get('artifacts_created', 0))
            return DiscordMessage(
                'Orchestrator',
                '\n'.join(
                    [
                        '**Approved action could not be executed**',
                        '',
                        f"Action: `{payload.get('type', 'unknown')}`",
                        f"Error: {payload.get('error', 'Unknown error.')}",
                        (
                            'Recorded side effects: '
                            f'{jobs_created} job(s), '
                            f'{artifacts_created} artifact(s).'
                        ),
                        (
                            'Run state: '
                            f"`{payload.get('resulting_state', 'PAUSED')}`."
                        ),
                        f"Next step: {payload.get('next_step', 'Operator review.')}",
                    ]
                ),
            )
        if event_type in {'job.submitted', 'job.completed', 'job.failed'}:
            return DiscordMessage(
                'Orchestrator',
                f"{event_type}: {payload.get('job_id')}",
            )
        if event_type == 'report.created':
            return DiscordMessage(
                'Honeydew',
                f"Report ready: {payload.get('uri')}",
            )
        if event_type in {
            'run.paused',
            'run.resumed',
            'run.cancelled',
            'run.completed',
        }:
            return DiscordMessage(
                'Orchestrator',
                event_type.replace('.', ' ').capitalize(),
                is_status=True,
            )
        if event_type == 'run.failed':
            error = str(payload.get('error', 'No failure detail was recorded.'))
            return DiscordMessage(
                'Orchestrator',
                '\n'.join(
                    [
                        '**Run failed**',
                        '',
                        f'Cause: {error}',
                        (
                            'No cluster job is implied by this status; inspect '
                            'the run events to identify the last completed stage.'
                        ),
                    ]
                ),
                is_status=True,
            )
        return None


class DiscordAdapter(ABC):
    @abstractmethod
    def create_thread(self, *, run_id: str, objective: str) -> str | None:
        raise NotImplementedError

    @abstractmethod
    def publish(
        self,
        *,
        thread_id: str | None,
        status_message_id: str | None,
        event: EventRecord,
    ) -> str | None:
        raise NotImplementedError


class DisabledDiscordAdapter(DiscordAdapter):
    def create_thread(self, *, run_id: str, objective: str) -> str | None:
        return None

    def publish(
        self,
        *,
        thread_id: str | None,
        status_message_id: str | None,
        event: EventRecord,
    ) -> str | None:
        return status_message_id


class DiscordHttpAdapter(DiscordAdapter):
    """Minimal REST projection. It is not workflow state or agent memory."""

    def __init__(
        self,
        *,
        bot_token: str,
        channel_id: str,
        webhook_url: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.bot_token = bot_token
        self.channel_id = channel_id
        self.webhook_url = webhook_url
        # Test seam: lets the test suite inject a mock transport and assert on
        # request shape without network access.
        self.transport = transport
        self.renderer = DiscordRenderer()

    def _client(self) -> httpx.Client:
        return httpx.Client(
            base_url='https://discord.com/api/v10',
            headers={'Authorization': f'Bot {self.bot_token}'},
            timeout=15,
            transport=self.transport,
        )

    def create_thread(self, *, run_id: str, objective: str) -> str | None:
        name = f'research-{run_id[:8]}'
        with self._client() as client:
            response = client.post(
                f'/channels/{self.channel_id}/threads',
                json={
                    'name': name,
                    'type': 11,
                    'auto_archive_duration': 1440,
                },
                headers={
                    'X-Audit-Log-Reason': quote(
                        f'Glasslab research run: {objective[:400]}'
                    ),
                },
            )
            response.raise_for_status()
            return str(response.json()['id'])

    def _publish_webhook(
        self,
        *,
        thread_id: str,
        message: DiscordMessage,
    ) -> None:
        if self.webhook_url is None:
            raise RuntimeError('Discord webhook URL is not configured')
        with httpx.Client(timeout=15, transport=self.transport) as client:
            response = client.post(
                self.webhook_url,
                params={'wait': 'true', 'thread_id': thread_id},
                json={
                    'username': message.identity,
                    'content': message.content[:2000],
                    'allowed_mentions': {'parse': []},
                },
            )
        if response.is_error:
            raise RuntimeError(
                f'Discord webhook returned HTTP {response.status_code}'
            )

    def publish(
        self,
        *,
        thread_id: str | None,
        status_message_id: str | None,
        event: EventRecord,
    ) -> str | None:
        if thread_id is None:
            # Run has no attached Discord thread yet (disabled or creation
            # failed); keep the caller's status id so a later attach still
            # works. No exception: this path must never fail the workflow.
            return status_message_id
        message = self.renderer.render(event)
        if message is None:
            return status_message_id
        if (
            self.webhook_url
            and not message.is_status
            and not message.components
        ):
            # Plain non-status messages go through the webhook so they are
            # authored with a friendly username. Status edits and component
            # rows cannot use webhooks (they need the bot API), so those fall
            # through to the bot path.
            self._publish_webhook(thread_id=thread_id, message=message)
            return status_message_id
        content = f'**{message.identity}:** {message.content}'[:2000]
        payload: dict[str, Any] = {'content': content}
        if message.components is not None:
            payload['components'] = message.components
        with self._client() as client:
            if message.is_status and status_message_id:
                response = client.patch(
                    f'/channels/{thread_id}/messages/{status_message_id}',
                    json=payload,
                )
            else:
                response = client.post(
                    f'/channels/{thread_id}/messages',
                    json=payload,
                )
            response.raise_for_status()
            if message.is_status and not status_message_id:
                return str(response.json()['id'])
        return status_message_id
