# Real metrics-write excerpt, extracted from the frozen evidence corpus:
#   artifacts/4735ccf943ae/beaker-worktree/benchmark-workspace/adult-income/run.py
# (hash 4735ccf943ae, 290 LOC). The write serializes ``final_metrics``, which
# on the primary path is bound to the ``metrics`` dict literal shown here; the
# excerpt keeps that binding and the ``os.path.join`` write target verbatim and
# omits the fallback branch that rebinds ``final_metrics`` from model results.
import json
import os


def run_calibration_experiment(output_dir, model, X_val, y_val):
    y_pred = model.predict(X_val)
    y_pred_proba = model.predict_proba(X_val)[:, 1]

    metrics = {
        'accuracy': accuracy_score(y_val, y_pred),
        'roc_auc': roc_auc_score(y_val, y_pred_proba),
        'precision': precision_score(y_val, y_pred, zero_division=0),
        'recall': recall_score(y_val, y_pred, zero_division=0),
        'f1': f1_score(y_val, y_pred, zero_division=0),
    }
    final_metrics = metrics

    metrics_path = os.path.join(output_dir, 'metrics.json')
    with open(metrics_path, 'w') as f:
        json.dump(final_metrics, f, indent=2)
