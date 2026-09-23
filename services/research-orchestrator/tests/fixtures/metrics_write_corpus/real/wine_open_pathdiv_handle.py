# Real metrics-write excerpt, extracted from the frozen evidence corpus:
#   artifacts/88d2ba581d15/beaker-worktree/benchmark-workspace/adult-income/run.py
# The excerpt isolates the metrics dict literal and the ``with open(<path
# division>, 'w')`` write; the surrounding experiment code is omitted.
import json


def emit_metrics(output_dir, cv_results, guardrails):
    metrics = {
        "accuracy_mean_5cv": cv_results["accuracy_mean_5cv"],
        "accuracy_std_5cv": cv_results["accuracy_std_5cv"],
        "roc_auc_mean_5cv": cv_results["roc_auc_mean_5cv"],
        "roc_auc_std_5cv": cv_results["roc_auc_std_5cv"],
        "f1_macro_mean_5cv": cv_results["f1_macro_mean_5cv"],
        "f1_macro_std_5cv": cv_results["f1_macro_std_5cv"],
        "fold_metrics": cv_results["fold_metrics"],
        "leakage_gap": guardrails["leakage_gap"],
        "overfitting_flag": guardrails["overfitting_flag"],
        "train_accuracy": guardrails["train_accuracy"],
        "test_accuracy": guardrails["test_accuracy"],
    }

    with open(output_dir / "metrics.json", "w") as f:
        json.dump(metrics, f, indent=2)
