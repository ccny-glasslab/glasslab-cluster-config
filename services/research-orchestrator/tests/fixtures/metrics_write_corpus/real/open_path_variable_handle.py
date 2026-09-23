# Real metrics-write excerpt, extracted from the frozen evidence corpus:
#   artifacts/2b645b35f583/beaker-worktree/run.py
# The excerpt isolates the ``metrics_summary`` dict literal and the write
# through a ``metrics_path = output_dir / "metrics.json"`` variable.
import json
from pathlib import Path


def save_metrics(cv_results, predictions, feature_cols, output_dir):
    metrics_summary = {
        'seed': 17,
        'cv_results': {
            'baseline': {
                'auc_mean': float(np.mean([r['auc'] for r in cv_results['baseline']])),
                'auc_std': float(np.std([r['auc'] for r in cv_results['baseline']])),
                'f1_mean': float(np.mean([r['f1'] for r in cv_results['baseline']])),
                'ece_mean': float(np.mean([r['ece'] for r in cv_results['baseline']])),
            },
        },
        'test_metrics': {
            name: {
                'auc': pred['auc'],
                'f1': pred['f1'],
                'ece': pred['ece'],
            }
            for name, pred in predictions.items()
        },
        'feature_importance': {
            name: {
                feature: float(predictions[name]['feature_importance'][i])
                for i, feature in enumerate(feature_cols)
            }
            for name in predictions.keys()
        },
    }

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    metrics_path = output_dir / "metrics.json"
    with open(metrics_path, "w") as f:
        json.dump(metrics_summary, f, indent=2)
    print(f"Metrics saved to: {metrics_path}")

    return metrics_summary
