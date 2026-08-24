"""Deterministic identity and acceptance tests for the frozen runtime-replay fixture."""

import hashlib
import json
from pathlib import Path

import pytest

from app.preflight import preflight_matrix
from app.schemas import (
    EvaluationContractDescriptor,
    ExperimentMatrix,
    ResolvedEvaluationContract,
    RunRecord,
)

FIXTURE_ROOT = (
    Path(__file__).resolve().parents[1] / 'fixtures' / 'runtime-replay' / 'wine-classification-v1'
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_fixture_manifest_matches_committed_bytes() -> None:
    manifest = json.loads((FIXTURE_ROOT / 'MANIFEST.json').read_text())
    assert manifest['case_id'] == 'wine-classification-v1'
    assert manifest['schema_version'] == 'glasslab-runtime-replay-fixture-v1'
    files = manifest['files']
    assert set(files) == {
        'PROMPT.txt',
        'matrix.json',
        'gold_repair.diff',
        'EXPECTED_PRE_REPAIR_FAILURES.json',
        'EXPECTED_PREFLIGHT.json',
        'workspace/configs/candidate.yaml',
        'contract/classification-metric-v1/1.0.0/contract.json',
    }
    for relative, expected_sha in files.items():
        assert _sha256(FIXTURE_ROOT / relative) == expected_sha, relative


def test_pre_repair_config_fails_frozen_requirements() -> None:
    report = acceptance_gate(FIXTURE_ROOT / 'workspace')
    expected_errors = json.loads(
        (FIXTURE_ROOT / 'EXPECTED_PRE_REPAIR_FAILURES.json').read_text()
    )
    assert report.passed is False
    for substring in expected_errors['error_substrings']:
        assert any(substring in error for error in report.errors), substring


def test_gold_repair_makes_preflight_pass(tmp_path: Path) -> None:
    repaired = tmp_path / 'workspace'
    shutil.copytree(FIXTURE_ROOT / 'workspace', repaired)
    apply_gold_repair(repaired)
    report = acceptance_gate(repaired)
    expected = json.loads((FIXTURE_ROOT / 'EXPECTED_PREFLIGHT.json').read_text())
    assert report.passed is True
    assert report.errors == []
    assert report.comparisons == expected['comparisons']


import shutil


def apply_gold_repair(workspace_root: Path) -> None:
    import subprocess

    subprocess.run(
        [
            'git',
            'apply',
            '--unsafe-paths',
            str(FIXTURE_ROOT / 'gold_repair.diff'),
        ],
        cwd=workspace_root,
        check=True,
    )


def acceptance_gate(workspace_root: Path):
    raw = (
        FIXTURE_ROOT
        / 'contract'
        / 'classification-metric-v1'
        / '1.0.0'
        / 'contract.json'
    ).read_bytes()
    contract = ResolvedEvaluationContract(
        descriptor=EvaluationContractDescriptor.model_validate_json(raw),
        digest=hashlib.sha256(raw).hexdigest(),
        root_path=str(workspace_root),
    )
    matrix = ExperimentMatrix.model_validate(
        json.loads((FIXTURE_ROOT / 'matrix.json').read_text())
    )
    run = RunRecord.model_construct(beaker_workspace=str(workspace_root))
    return preflight_matrix(run=run, matrix=matrix, contract=contract)


@pytest.mark.parametrize(
    'relative,marker',
    [
        ('PROMPT.txt', '"src/train"'),
        ('PROMPT.txt', 'Do not execute cluster work.'),
        ('workspace/configs/candidate.yaml', 'methodology:'),
    ],
)
def test_frozen_case_content_markers(relative: str, marker: str) -> None:
    text = (FIXTURE_ROOT / relative).read_text()
    assert marker in text
