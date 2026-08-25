"""Regression: the async task-bundle import must not block the event loop.

An uncached compile runs a 40-90s synchronous OpenCode agent turn. When the
async ``POST /task-bundles/import`` handler ran that synchronously, the whole
event loop was blocked: the k8s /ready probe timed out and the discord.py
Gateway task starved, so interactions could not ACK within Discord's 3-second
deadline ("The application did not respond"). The handler must offload the
compile to a worker thread via ``asyncio.to_thread``.
"""

from __future__ import annotations

import threading
import time

from fastapi.testclient import TestClient

from app.main import create_app
from app.task_bundles import TaskBundleRecord


def _fake_record(task_id: str = "task-fake") -> TaskBundleRecord:
    return TaskBundleRecord(
        task_id=task_id,
        display_name="Fake",
        digest="f" * 64,
        archive_uri="s3://artifacts/fake.zip",
        archive_path="/fake/archive.zip",
        problem_path="/fake/problem.md",
        evaluator_prompt_path="/fake/eval.md",
        workload_id="research-workspace-cpu-v1",
        experiment_type="research-workspace-job",
        runner_image="ghcr.io/ccny-glasslab/glasslab-test-runner:test",
        command=["python3", "-m", "runner"],
        source_subdirectory=".",
        default_contract_id="generic-task-integrity-v1",
        default_contract_version="1.0.0",
        resources={},
        required_artifacts=["report.md"],
        datasets=[],
    )


def test_import_offloads_slow_compile_off_the_event_loop(
    orchestrator_bundle,
) -> None:
    settings, store, cluster, runtime, engine = orchestrator_bundle

    def slow_import(*, filename: str, content: bytes) -> TaskBundleRecord:
        # Stands in for the 40-90s OpenCode compile; 2s is enough to prove
        # the event loop stays responsive while the compile is in flight.
        time.sleep(2.0)
        return _fake_record()

    engine.import_task_bundle = slow_import  # type: ignore[method-assign]
    app = create_app(settings, engine=engine, start_watcher=False)
    with TestClient(app) as client:
        result: dict[str, object] = {}

        def do_import() -> None:
            result["status"] = client.post(
                "/task-bundles/import",
                files={"archive": ("fixture.zip", b"PK", "application/zip")},
            ).status_code

        thread = threading.Thread(target=do_import)
        thread.start()
        started = time.monotonic()
        ready = client.get("/ready", timeout=1.0)
        elapsed = time.monotonic() - started
        thread.join(timeout=10)
        assert not thread.is_alive(), "import handler did not finish"
        # /ready must answer promptly while the compile is in flight.
        assert elapsed < 1.5, f"/ready blocked {elapsed:.2f}s during import"
        assert ready.status_code == 200
        assert result.get("status") == 201