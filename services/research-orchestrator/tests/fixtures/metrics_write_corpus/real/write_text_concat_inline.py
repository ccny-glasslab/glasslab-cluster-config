# Real metrics-write excerpt, extracted from the frozen evidence corpus:
#   artifacts/7e59acd748d4/beaker-worktree/benchmark-workspace/adult-income/run.py
# The excerpt isolates the ``final_metrics`` dict literal and the
# ``(output_dir / "metrics.json").write_text(json.dumps(...) + "\n")`` write.
import json
from pathlib import Path

N_BOOTSTRAP = 2000


def write_metrics(output_dir, metrics_lr, test_rows):
    final_metrics = {
        "accuracy": metrics_lr["accuracy"],
        "balanced_accuracy": metrics_lr["balanced_accuracy"],
        "precision": metrics_lr["precision"],
        "recall": metrics_lr["recall"],
        "f1": metrics_lr["f1"],
        "roc_auc": metrics_lr["roc_auc"],
        "headline_ci_low": metrics_lr["roc_auc_ci_low"],
        "headline_ci_high": metrics_lr["roc_auc_ci_high"],
        "bootstrap_resamples": N_BOOTSTRAP,
        "test_rows": test_rows,
    }

    output_dir = Path(output_dir)
    (output_dir / "metrics.json").write_text(
        json.dumps(final_metrics, indent=2, sort_keys=True) + "\n"
    )
