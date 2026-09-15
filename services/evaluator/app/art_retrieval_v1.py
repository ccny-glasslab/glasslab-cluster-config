"""Art-retrieval-specific run comparison for evaluator type art-retrieval-v1.

A specialization of the tabular comparison: it additionally reads a
workload-produced comparison.json from each run bundle and, when present, ranks
runs by the workload's own composite_score (higher wins) before falling back to
the generic primary-metric ordering. Kept separate from main.py so the generic
comparison stays stable while this variant owns its extra contract.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from services.common.schemas import Metrics, RunManifest, RunStatus

from .models import ComparedRun, ComparisonResult, RankedRun


def load_art_retrieval_bundle(bundle_dir: Path) -> ComparedRun:
    """Load a metric-search run bundle with art-retrieval specific fields."""
    manifest = RunManifest.model_validate_json((bundle_dir / 'run_manifest.json').read_text())
    metrics = Metrics.model_validate_json((bundle_dir / 'metrics.json').read_text())
    status = RunStatus.model_validate_json((bundle_dir / 'status.json').read_text())

    primary_metric = None
    if metrics.primary_metric:
        primary_metric = next((item for item in metrics.values if item.name == metrics.primary_metric), None)
    # A primary_metric name with no matching value row yields None rather than
    # an error: metric presence is itself part of the ranking, so a missing
    # value must be observable, not fatal.

    # Load art-retrieval specific outputs
    comparison_json_path = bundle_dir / 'comparison.json'
    comparison_output: dict[str, Any] | None = None
    # comparison.json is optional; a bundle without it simply ranks on the
    # generic primary metric below.
    if comparison_json_path.exists():
        comparison_output = json.loads(comparison_json_path.read_text())

    return ComparedRun(
        run_id=manifest.run_id,
        workflow_id=manifest.workflow_id,
        workflow_family=manifest.workflow_family,
        models=manifest.requested_models,
        status=status.status,
        primary_metric_name=metrics.primary_metric,
        primary_metric_value=primary_metric.value if primary_metric else None,
        primary_metric_direction=primary_metric.direction if primary_metric else None,
        runtime_seconds=metrics.runtime_seconds,
        art_retrieval_output=comparison_output,
    )


def rank_art_retrieval(runs: list[ComparedRun]) -> tuple[list[ComparedRun], str]:
    """Rank runs using art-retrieval specific logic (composite score)."""
    if not runs:
        return [], 'no runs supplied'

    # Sort by composite_score if available, otherwise fall back to primary metric
    def sort_key(item: ComparedRun):
        # Try to extract composite_score from art_retrieval_output
        composite_score = None
        if item.art_retrieval_output:
            ranking = item.art_retrieval_output.get('ranking', [])
            if ranking:
                composite_score = ranking[0].get('composite_score')

        status_weight = 0 if item.status == 'succeeded' else 1
        metric_missing = 1 if item.primary_metric_value is None else 0

        # Tuple order is significant: status first, then metric presence, so a
        # failed or metric-less run always sorts behind a complete succeeded one
        # regardless of how good its score is.
        if composite_score is not None:
            metric_sort = -composite_score  # higher is better
        elif item.primary_metric_value is not None:
            direction = item.primary_metric_direction or 'maximize'
            metric_sort = item.primary_metric_value if direction == 'minimize' else -item.primary_metric_value
        else:
            metric_sort = 0.0

        # Missing runtime sorts last as a tiebreak; run_id makes ties total.
        runtime_sort = item.runtime_seconds if item.runtime_seconds is not None else float('inf')
        return (status_weight, metric_missing, metric_sort, runtime_sort, item.run_id)

    ranked = sorted(runs, key=sort_key)

    # Build comparison basis
    basis_parts = ['succeeded status, metric presence']
    has_composite = any(r.art_retrieval_output and r.art_retrieval_output.get('ranking') for r in ranked)
    if has_composite:
        basis_parts.append('composite_score (art-retrieval)')
    else:
        basis_parts.append('primary metric')
    basis_parts.append('runtime seconds, run_id')
    
    basis = ', '.join(basis_parts)
    return ranked, basis


def compare_art_retrieval_bundles(bundle_dirs: list[Path]) -> ComparisonResult:
    """Compare metric-search run bundles using art-retrieval logic."""
    compared_runs = [load_art_retrieval_bundle(path) for path in bundle_dirs]
    ranked_runs, basis = rank_art_retrieval(compared_runs)
    ranking = [
        RankedRun(position=index, run_id=item.run_id, reason='ordered by succeeded status, metric presence, composite_score, runtime')
        for index, item in enumerate(ranked_runs, start=1)
    ]
    best_run_id = ranked_runs[0].run_id if ranked_runs else None
    
    return ComparisonResult(
        compared_runs=compared_runs,
        ranking=ranking,
        best_run_id=best_run_id,
        comparison_basis=basis,
    )


def render_art_retrieval_summary(result: ComparisonResult) -> str:
    """Render a summary for art-retrieval comparisons."""
    lines = ['# Art-Retrieval Comparison Summary', '']
    if result.best_run_id is None:
        lines.extend(['No runs were supplied.', ''])
        return '\n'.join(lines)

    lines.append(f'Best run: `{result.best_run_id}`')
    lines.append('')
    
    # Check if we have art-retrieval specific rankings
    has_composite = any(
        r.art_retrieval_output and r.art_retrieval_output.get('ranking') 
        for r in result.compared_runs
    )
    if has_composite:
        lines.append('## Composite Rankings')
        lines.append('')
        for run in result.compared_runs:
            if run.art_retrieval_output and run.art_retrieval_output.get('ranking'):
                ranking = run.art_retrieval_output['ranking']
                if ranking:
                    comp_score = ranking[0].get('composite_score', 'N/A')
                    lines.append(f'- `{run.run_id}`: composite_score={comp_score}')
        lines.append('')

    lines.append('## Compared Runs')
    lines.append('')
    for run in sorted(result.compared_runs, key=lambda item: item.run_id):
        lines.append(
            f'- `{run.run_id}` | workflow `{run.workflow_id}` | family `{run.workflow_family}` | '
            f'models `{", ".join(run.models)}` | status `{run.status}` | '
            f'primary metric `{run.primary_metric_name}`={run.primary_metric_value} | runtime `{run.runtime_seconds}`s'
        )
    lines.append('')
    lines.append(f'Comparison basis: {result.comparison_basis}')
    return '\n'.join(lines)


def write_art_retrieval_outputs(bundle_dirs: list[Path], output_dir: Path) -> ComparisonResult:
    """Write art-retrieval comparison outputs."""
    result = compare_art_retrieval_bundles(bundle_dirs)
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # Write comparison.json
    comparison_json = output_dir / 'comparison.json'
    comparison_data = result.model_dump(mode='json')
    comparison_json.write_text(json.dumps(comparison_data, indent=2) + '\n')
    
    # Write summary.md
    summary_md = output_dir / 'summary.md'
    summary_md.write_text(render_art_retrieval_summary(result) + '\n')

    return result
