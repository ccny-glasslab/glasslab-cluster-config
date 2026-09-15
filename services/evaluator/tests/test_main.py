"""Tests for the evaluator's generic tabular-metric-max comparison.

write_bundle fabricates the three runner records a real bundle carries
(run_manifest.json, metrics.json, status.json) so write_outputs can be exercised
end-to-end against a temp directory without touching the cluster.
"""

import json
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.art_retrieval_v1 import write_art_retrieval_outputs
from app.main import load_bundle, main as evaluator_main, write_outputs


def write_bundle(path: Path, run_id: str, metric: float, runtime_seconds: float) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / 'run_manifest.json').write_text(
        json.dumps(
            {
                'run_id': run_id,
                'workflow_id': 'generic-tabular-benchmark',
                'workflow_family': 'tabular-benchmark',
                'display_name': 'Generic Tabular Benchmark',
                'objective': 'Benchmark approved models.',
                'submitted_by': 'tester',
                'submitted_at': '2026-03-16T18:00:00Z',
                'inputs': {'dataset_name': 'titanic'},
                'requested_models': ['random_forest'],
                'resource_profile': 'cpu-small',
                'runner_image': 'ghcr.io/ccny-glasslab/glasslab-tabular-runner:0.1.2',
                'runner_service_account_name': 'glasslab-gpu-runner',
                'evaluator_type': 'tabular-metric-max',
                'approval_tier': 'tier-2-approved-execution',
                'expected_artifacts': {'required': ['run_manifest.json'], 'optional': []},
            }
        )
    )
    (path / 'metrics.json').write_text(
        json.dumps(
            {
                'run_id': run_id,
                'primary_metric': 'validation_accuracy',
                'values': [
                    {
                        'name': 'validation_accuracy',
                        'value': metric,
                        'direction': 'maximize',
                        'split': 'validation',
                    }
                ],
                'runtime_seconds': runtime_seconds,
                'notes': [],
            }
        )
    )
    (path / 'status.json').write_text(
        json.dumps(
            {
                'run_id': run_id,
                'status': 'succeeded',
                'updated_at': '2026-03-16T18:01:00Z',
                'detail': 'ok',
            }
        )
    )


def test_write_outputs_ranks_best_run(tmp_path) -> None:
    bundle_a = tmp_path / 'run-a'
    bundle_b = tmp_path / 'run-b'
    output_dir = tmp_path / 'output'
    write_bundle(bundle_a, 'run-a', 0.81, 42.0)
    write_bundle(bundle_b, 'run-b', 0.85, 38.0)

    result = write_outputs([bundle_a, bundle_b], output_dir)

    assert result.best_run_id == 'run-b'
    comparison = json.loads((output_dir / 'comparison.json').read_text())
    assert comparison['ranking'][0]['run_id'] == 'run-b'
    summary = (output_dir / 'summary.md').read_text()
    assert 'Best run: `run-b`' in summary
    assert 'Comparison basis:' in summary


@pytest.mark.parametrize('bad_value', [float('nan'), float('inf'), float('-inf')])
def test_load_bundle_rejects_non_finite_metric(tmp_path, bad_value) -> None:
    bundle = tmp_path / 'run-bad'
    write_bundle(bundle, 'run-bad', 0.5, 10.0)
    metrics_path = bundle / 'metrics.json'
    metrics = json.loads(metrics_path.read_text())
    metrics['values'][0]['value'] = bad_value
    metrics_path.write_text(json.dumps(metrics))

    with pytest.raises(ValidationError):
        load_bundle(bundle)


def test_art_retrieval_outputs_write_computed_comparison(tmp_path) -> None:
    bundle = tmp_path / 'run-a'
    output_dir = tmp_path / 'output'
    write_bundle(bundle, 'run-a', 0.81, 42.0)
    (bundle / 'comparison.json').write_text(
        json.dumps({'ranking': [{'composite_score': 0.9}]})
    )

    write_art_retrieval_outputs([bundle], output_dir)

    comparison = json.loads((output_dir / 'comparison.json').read_text())
    assert comparison['best_run_id'] == 'run-a'
    assert 'compared_runs' in comparison


def test_cli_evaluator_type_dispatches_art_retrieval(tmp_path, monkeypatch) -> None:
    bundle_a = tmp_path / 'run-a'
    bundle_b = tmp_path / 'run-b'
    output_dir = tmp_path / 'output'
    write_bundle(bundle_a, 'run-a', 0.81, 42.0)
    write_bundle(bundle_b, 'run-b', 0.85, 38.0)
    monkeypatch.setattr(
        sys,
        'argv',
        [
            'evaluator',
            '--bundle-dir',
            str(bundle_a),
            '--bundle-dir',
            str(bundle_b),
            '--output-dir',
            str(output_dir),
            '--evaluator-type',
            'art_retrieval_v1',
        ],
    )

    assert evaluator_main() == 0
    summary = (output_dir / 'summary.md').read_text()
    assert summary.startswith('# Art-Retrieval Comparison Summary')
