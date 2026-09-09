"""Tests for the JobWatcher poll/reconcile loop.

The watcher dispatches blocking reconcile work to a worker thread so the
asyncio event loop (shared with the discord.py gateway, FastAPI, /ready, and
SSE streams) is never blocked by cluster or Discord I/O. Its error path must
follow the same rule: durable event writes and Discord publishes must be
offloaded off the loop too (issue #250).
"""

from __future__ import annotations

import asyncio
import threading

from app.schemas import RunCreateRequest, RunState
from app.watcher import JobWatcher


def test_watcher_error_path_offloads_event_write(orchestrator_bundle) -> None:
    # A reconcile failure must record the event off the event loop: the write
    # (store append + Discord publish) is blocking, so it must run on a worker
    # thread, never on the loop shared with the gateway and API.
    _, store, _, _, engine = orchestrator_bundle
    run = engine.create_run(
        RunCreateRequest(objective='Watcher error-path offload.')
    )
    # Force the run into the job phase so the watcher reconciles it.
    store.replace_run(
        run.model_copy(update={'state': RunState.JOB_QUEUED}),
        expected_version=run.version,
    )

    loop_thread = threading.get_ident()

    def failing_reconcile(run_id: str):
        raise RuntimeError('cluster unreachable')

    event_threads: list[int] = []
    event_types: list[str] = []

    def recording_event(run_id, *, source, event_type, payload=None):
        event_threads.append(threading.get_ident())
        event_types.append(event_type)

    engine.reconcile_run = failing_reconcile
    engine._event = recording_event

    watcher = JobWatcher(
        engine,
        poll_interval_seconds=0.01,
    )

    async def drive() -> None:
        task = asyncio.create_task(watcher.run())
        # Give the loop a few polls so the failing reconcile path runs.
        for _ in range(50):
            await asyncio.sleep(0.01)
            if event_types:
                break
        watcher.stop()
        await task

    asyncio.run(drive())

    assert event_types == ['job.reconciliation_failed']
    # The event write must run on a worker thread, not the event loop thread.
    assert event_threads
    assert all(t != loop_thread for t in event_threads)
