"""Default deployment coverage of runtime-profile runner images (issue #502).

The code default for ``permitted_job_images`` listed only the CPU runner, so a
default deployment silently could not run ``gpu-ml-standard-v1`` tasks: task
preflight rejected the compiled GPU runner image with a task-authoring-looking
error. These tests pin the replacement behavior: the default derives from
``RUNTIME_PROFILES``, and a real-execution deployment whose allowlist omits a
profile image fails fast at composition time instead of at task preflight.
"""

from __future__ import annotations

import io
from pathlib import Path
import zipfile

import pytest

from app.cluster import FakeClusterExecutor
from app.config import SERVICE_ROOT, Settings
from app.main import build_engine
from app.schemas import TaskSpecProposal
from app.task_bundles import (
    RUNTIME_PROFILES,
    TaskBundleError,
    TaskBundleManager,
    require_profile_runner_images,
)


def _archive() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as handle:
        # issue #496 enforces the guide's mandatory problem.md sections at
        # import, so this fixture must be structurally valid for the bundle to
        # compile; the content itself is irrelevant to the image-coverage check.
        handle.writestr(
            'task/problem.md',
            '# Task\n\n'
            '## Objective\n\nRun a GPU-profile task under the default '
            'deployment.\n\n'
            '## Inputs\n\nA small generated dataset.\n\n'
            '## Method and architecture\n\nA single training script.\n\n'
            '## Hyperparameter search space\n\nOne fixed configuration.\n\n'
            '## Evaluation rubric\n\nReport the primary metric.\n\n'
            '## Evidence artifacts\n\nmetrics.json.\n',
        )
    return output.getvalue()


def _manager(tmp_path: Path) -> TaskBundleManager:
    catalog_path = tmp_path / 'catalog.json'
    catalog_path.write_text('{}')
    return TaskBundleManager(
        root=str(tmp_path / 'task-bundles'),
        shared_mount_root=str(tmp_path),
        dataset_catalog_path=str(catalog_path),
        task_asset_root=str(tmp_path / 'task-assets'),
    )


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    return Settings(
        database_path=str(tmp_path / 'orchestrator.db'),
        workspace_root=str(tmp_path / 'runs'),
        artifact_root=str(tmp_path / 'artifacts'),
        approved_repo_path=str(tmp_path / 'repo'),
        approved_repo_ref='main',
        evaluation_contract_root=str(SERVICE_ROOT / 'evaluation-contracts'),
        promoted_contract_root=str(tmp_path / 'trusted-contracts'),
        sealed_contract_candidate_root=str(tmp_path / 'contract-candidates'),
        trusted_contract_catalog_path=str(
            tmp_path / 'trusted-contracts' / 'catalog.json'
        ),
        shared_mount_root=str(tmp_path),
        task_bundle_root=str(tmp_path / 'task-bundles'),
        task_asset_root=str(tmp_path / 'task-assets'),
        dataset_upload_root=str(tmp_path / 'dataset-uploads'),
        benchmark_dataset_catalog_path=str(tmp_path / 'datasets' / 'catalog.json'),
        one_active_run=False,
        **overrides,
    )


def test_default_settings_permit_every_profile_runner_image() -> None:
    permitted = set(Settings().permitted_job_images)

    assert {
        profile.runner_image for profile in RUNTIME_PROFILES.values()
    } <= permitted


def test_default_settings_preflight_a_gpu_profile_task(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    proposal = TaskSpecProposal(
        schema_version='glasslab-task-spec-v1',
        display_name='Default GPU Runtime Task',
        runtime_profile='gpu-ml-standard-v1',
        rationale='Verify the default deployment can run GPU-profile tasks.',
    )
    record = manager.compile(
        manager.stage_archive(filename='task.zip', content=_archive()),
        proposal,
    )

    preflight = manager.preflight(
        record,
        permitted_images=set(Settings().permitted_job_images),
        evaluator_ready=True,
    )

    assert preflight.runtime_ready
    assert preflight.ready


def test_require_profile_runner_images_rejects_partial_allowlist() -> None:
    cpu_only = {RUNTIME_PROFILES['cpu-ml-standard-v1'].runner_image}

    with pytest.raises(TaskBundleError, match='gpu-ml-standard-v1'):
        require_profile_runner_images(cpu_only)


def test_require_profile_runner_images_accepts_full_allowlist() -> None:
    require_profile_runner_images(
        {profile.runner_image for profile in RUNTIME_PROFILES.values()}
    )


def test_build_engine_fails_fast_when_real_deployment_cannot_run_a_profile(
    tmp_path: Path,
) -> None:
    settings = _settings(
        tmp_path,
        cluster_execution_mode='workflow-api',
        permitted_job_images=[RUNTIME_PROFILES['cpu-ml-standard-v1'].runner_image],
    )

    with pytest.raises(TaskBundleError, match='gpu-ml-standard-v1'):
        build_engine(settings, cluster=FakeClusterExecutor())
