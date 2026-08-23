"""Single source of truth for legal run-state transitions.

validate_transition is invoked inside storage.transition_run's transaction, so
an illegal move is rejected atomically with the state write. The one-active-run
constraint is not part of this graph: it is enforced separately in
storage.create_run, guarded by the one_active_run setting.
"""

from __future__ import annotations

from .schemas import RunState, TERMINAL_STATES


# States where no agent or cluster work is in flight; the engine stops the
# active-runtime clock while a run waits on a human (see transition_run's
# active_since bookkeeping).
HUMAN_WAIT_STATES = {
    RunState.AWAITING_PROTOCOL_APPROVAL,
    RunState.AWAITING_CONTRACT_PROMOTION,
    RunState.AWAITING_EXECUTION_APPROVAL,
    RunState.AWAITING_FINAL_ACCEPTANCE,
}


TRANSITIONS: dict[RunState, set[RunState]] = {
    RunState.CREATED: {RunState.PREPARING, RunState.CANCELLED, RunState.FAILED},
    RunState.PREPARING: {
        RunState.HONEYDEW_DRAFTING_PROTOCOL,
        # A terminal retry with a verified immutable protocol intentionally
        # waits for a fresh human protocol approval rather than redrafting it.
        RunState.AWAITING_PROTOCOL_APPROVAL,
        RunState.PAUSED,
        RunState.CANCELLED,
        RunState.FAILED,
    },
    RunState.HONEYDEW_DRAFTING_PROTOCOL: {
        RunState.AWAITING_PROTOCOL_APPROVAL,
        RunState.PAUSED,
        RunState.CANCELLED,
        RunState.FAILED,
        RunState.TIMED_OUT,
    },
    RunState.AWAITING_PROTOCOL_APPROVAL: {
        RunState.HONEYDEW_DRAFTING_PROTOCOL,
        RunState.BEAKER_DRAFTING_CONTRACT,
        RunState.BEAKER_PLANNING,
        RunState.PAUSED,
        RunState.CANCELLED,
        RunState.FAILED,
        RunState.TIMED_OUT,
    },
    RunState.BEAKER_DRAFTING_CONTRACT: {
        RunState.HONEYDEW_REVIEWING_CONTRACT,
        RunState.PAUSED,
        RunState.CANCELLED,
        RunState.FAILED,
        RunState.TIMED_OUT,
    },
    RunState.HONEYDEW_REVIEWING_CONTRACT: {
        RunState.BEAKER_DRAFTING_CONTRACT,
        RunState.AWAITING_CONTRACT_PROMOTION,
        RunState.PAUSED,
        RunState.CANCELLED,
        RunState.FAILED,
        RunState.TIMED_OUT,
    },
    RunState.AWAITING_CONTRACT_PROMOTION: {
        RunState.BEAKER_DRAFTING_CONTRACT,
        RunState.BEAKER_PLANNING,
        RunState.PAUSED,
        RunState.CANCELLED,
        RunState.FAILED,
        RunState.TIMED_OUT,
    },
    RunState.BEAKER_PLANNING: {
        RunState.BEAKER_DRAFTING_CONTRACT,
        RunState.BEAKER_IMPLEMENTING,
        RunState.PAUSED,
        RunState.CANCELLED,
        RunState.FAILED,
        RunState.TIMED_OUT,
    },
    RunState.BEAKER_IMPLEMENTING: {
        RunState.BEAKER_PLANNING,
        RunState.BEAKER_DRAFTING_CONTRACT,
        RunState.BEAKER_FINALIZING,
        RunState.BEAKER_REVISING,
        RunState.HONEYDEW_REVIEWING,
        RunState.PAUSED,
        RunState.CANCELLED,
        RunState.FAILED,
        RunState.TIMED_OUT,
    },
    RunState.BEAKER_FINALIZING: {
        RunState.BEAKER_DRAFTING_CONTRACT,
        RunState.BEAKER_REVISING,
        RunState.HONEYDEW_REVIEWING,
        RunState.PAUSED,
        RunState.CANCELLED,
        RunState.FAILED,
        RunState.TIMED_OUT,
    },
    RunState.HONEYDEW_REVIEWING: {
        RunState.BEAKER_DRAFTING_CONTRACT,
        RunState.BEAKER_REVISING,
        RunState.AWAITING_EXECUTION_APPROVAL,
        RunState.PAUSED,
        RunState.CANCELLED,
        RunState.FAILED,
        RunState.TIMED_OUT,
    },
    RunState.BEAKER_REVISING: {
        RunState.BEAKER_DRAFTING_CONTRACT,
        RunState.HONEYDEW_REVIEWING,
        RunState.PAUSED,
        RunState.CANCELLED,
        RunState.FAILED,
        RunState.TIMED_OUT,
    },
    RunState.AWAITING_EXECUTION_APPROVAL: {
        RunState.BEAKER_DRAFTING_CONTRACT,
        RunState.JOB_QUEUED,
        RunState.BEAKER_REVISING,
        RunState.PAUSED,
        RunState.CANCELLED,
        RunState.FAILED,
        RunState.TIMED_OUT,
    },
    RunState.JOB_QUEUED: {
        RunState.JOB_RUNNING,
        # BEAKER_ANALYZING is reachable directly from JOB_QUEUED: if every job
        # finishes before a poll ever observes RUNNING, the run can still
        # proceed to analysis without being stuck.
        RunState.BEAKER_ANALYZING,
        RunState.PAUSED,
        RunState.CANCELLED,
        RunState.FAILED,
        RunState.TIMED_OUT,
    },
    RunState.JOB_RUNNING: {
        RunState.BEAKER_ANALYZING,
        RunState.PAUSED,
        RunState.CANCELLED,
        RunState.FAILED,
        RunState.TIMED_OUT,
    },
    RunState.BEAKER_ANALYZING: {
        RunState.HONEYDEW_VERIFYING,
        RunState.BEAKER_REVISING,
        RunState.PAUSED,
        RunState.CANCELLED,
        RunState.FAILED,
        RunState.TIMED_OUT,
    },
    RunState.HONEYDEW_VERIFYING: {
        RunState.BEAKER_REVISING,
        RunState.HONEYDEW_WRITING_REPORT,
        RunState.PAUSED,
        RunState.CANCELLED,
        RunState.FAILED,
        RunState.TIMED_OUT,
    },
    RunState.HONEYDEW_WRITING_REPORT: {
        RunState.AWAITING_FINAL_ACCEPTANCE,
        RunState.PAUSED,
        RunState.CANCELLED,
        RunState.FAILED,
        RunState.TIMED_OUT,
    },
    RunState.AWAITING_FINAL_ACCEPTANCE: {
        RunState.COMPLETE,
        RunState.HONEYDEW_WRITING_REPORT,
        RunState.PAUSED,
        RunState.CANCELLED,
        RunState.FAILED,
        RunState.TIMED_OUT,
    },
    # Pause can happen in any non-terminal phase and resume re-enters the phase
    # recorded in resume_state. Operators must also be able to discard a paused
    # run without resuming it and accidentally starting another agent turn.
    RunState.PAUSED: set(RunState)
    - {RunState.PAUSED, RunState.COMPLETE, RunState.FAILED, RunState.TIMED_OUT},
    # Terminal states have no outgoing edges: once COMPLETE/FAILED/CANCELLED/
    # TIMED_OUT, no further transition can pass validation.
    RunState.COMPLETE: set(),
    RunState.FAILED: set(),
    RunState.CANCELLED: set(),
    RunState.TIMED_OUT: set(),
}


class InvalidTransition(ValueError):
    pass


def validate_transition(current: RunState, target: RunState) -> None:
    if target not in TRANSITIONS[current]:
        raise InvalidTransition(f'invalid run transition: {current} -> {target}')
