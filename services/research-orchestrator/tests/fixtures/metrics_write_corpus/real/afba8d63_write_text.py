# Real metrics-write excerpt, extracted verbatim from the frozen evidence
# corpus (read-only PVC, evidence-freeze/20260923):
#   artifacts/afba8d630c7e4dbba553ba200ee050f7/beaker-worktree/
#   research-workspace/task-d777d26b32557f22/run.py
# This is the run whose Path.write_text write was falsely rejected by the
# idiom-enumerating scanner before PR #529.
# The excerpt isolates the metrics dict literal and the write statement; the
# surrounding benchmark code is omitted.
import json
from pathlib import Path

SEED = 17


def write_metrics(output_dir, results, cv_folds):
    metrics = {
        'metric_name': 'cv_accuracy_mean',
        'cv_folds': cv_folds,
        'seed': SEED,
        'best_model': max(results, key=lambda m: results[m]['cv_accuracy_mean']),
        'best_metric': results[max(results, key=lambda m: results[m]['cv_accuracy_mean'])]['cv_accuracy_mean'],
        'models': results,
    }

    metrics_path = output_dir / 'metrics.json'
    metrics_path.write_text(json.dumps(metrics, indent=2, sort_keys=True) + '\n')
    assert metrics_path.exists(), 'metrics.json must be written'
