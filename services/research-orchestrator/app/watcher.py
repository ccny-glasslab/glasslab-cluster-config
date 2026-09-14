"""Poll runs in the job phase and reconcile them against the cluster.

Reconcile is a blocking, synchronous call, so it is dispatched to a worker
thread while the loop keeps polling. A reconcile failure is recorded as an
event and never crashes the loop; only runs in JOB_QUEUED or JOB_RUNNING are
reconciled, so paused or terminal runs are left for recovery and resume
paths. Cancelled runs get one final job sweep per poll so a job committed
RUNNING around the cancellation cannot keep consuming cluster resources
(issue #246).
"""

from __future__ import annotations

import asyncio

from .engine import ResearchOrchestrator
from .schemas import RunState


class JobWatcher:
    def __init__(
        self,
        engine: ResearchOrchestrator,
        *,
        poll_interval_seconds: float,
    ) -> None:
        self.engine = engine
        self.poll_interval_seconds = poll_interval_seconds
        self._stop = asyncio.Event()

    async def run(self) -> None:
        while not self._stop.is_set():
            for run in self.engine.store.list_runs():
                if run.state == RunState.CANCELLED:
                    # Final sweep for a just-cancelled run: a job left in a
                    # non-terminal status would otherwise keep running with
                    # nothing alive to cancel it.
                    try:
                        await asyncio.to_thread(
                            self.engine.sweep_cancelled_run_jobs,
                            run.run_id,
                        )
                    except Exception as exc:
                        # _event performs a blocking store write plus a Discord
                        # publish, so keep it off the event loop just like the
                        # reconcile work itself (issue #250).
                        await asyncio.to_thread(
                            self.engine._event,
                            run.run_id,
                            source='orchestrator',
                            event_type='job.cancellation_sweep_failed',
                            payload={'error': str(exc)},
                        )
                    continue
                # Paused and terminal runs must not be reconciled here; resume
                # and recovery drive their transitions, and reconcile assumes a
                # job the orchestrator still owns.
                if run.state not in {
                    RunState.JOB_QUEUED,
                    RunState.JOB_RUNNING,
                }:
                    continue
                try:
                    # reconcile_run is blocking cluster I/O, so keep it off the
                    # event loop even though the polling itself is async.
                    await asyncio.to_thread(
                        self.engine.reconcile_run,
                        run.run_id,
                    )
                except Exception as exc:
                    # Record the failure and keep polling: a transient cluster
                    # error must not kill the watcher, and the event log is the
                    # authoritative record of what went wrong. The write is
                    # blocking (store append + Discord publish), so offload it
                    # off the event loop (issue #250).
                    await asyncio.to_thread(
                        self.engine._event,
                        run.run_id,
                        source='orchestrator',
                        event_type='job.reconciliation_failed',
                        payload={'error': str(exc)},
                    )
            try:
                # Sleeping on the stop event instead of asyncio.sleep makes
                # stop() preempt the current poll interval immediately.
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=self.poll_interval_seconds,
                )
            except TimeoutError:
                pass

    def stop(self) -> None:
        self._stop.set()
