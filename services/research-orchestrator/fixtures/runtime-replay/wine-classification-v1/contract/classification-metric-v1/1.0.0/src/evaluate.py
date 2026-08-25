#!/usr/bin/env python3
"""
Evaluation script for wine quality classification benchmark.
Evaluates trained models and computes metrics per evaluation contract.
"""

import json
import argparse
import numpy as np
from pathlib import Path
from sklearn.metrics import (
    roc_auc_score, f1_score, accuracy_score, 
    precision_recall_curve, brier_score_loss
)
from sklearn.calibration import calibration_curve


def compute_ece(y_true, y_prob, n_bins=10):
    """Compute Expected Calibration Error (ECE)."""
    bin_boundaries = np.linspace(0, 1, n_bins + 1)
    ece = 0.0
    total_samples = len(y_true)
    
    for i in range(n_bins):
        bin_lower = bin_boundaries[i]
        bin_upper = bin_boundaries[i + 1]
        
        in_bin = (y_prob > bin_lower) & (y_prob <= bin_upper)
        prop_in_bin = in_bin.sum() / total_samples
        
        if in_bin.sum() > 0:
            avg_confidence = y_prob[in_bin].mean()
            avg_accuracy = y_true[in_bin].mean()
            ece += np.abs(avg_accuracy - avg_confidence) * prop_in_bin
    
    return ece


def compute_metrics(y_true, y_pred, y_prob, threshold=0.5):
    """Compute all evaluation metrics."""
    metrics = {}
    
    y_pred_binary = (y_prob >= threshold).astype(int)
    
    metrics['roc_auc'] = roc_auc_score(y_true, y_prob)
    metrics['f1'] = f1_score(y_true, y_pred_binary)
    metrics['accuracy'] = accuracy_score(y_true, y_pred_binary)
    metrics['ece'] = compute_ece(y_true, y_prob)
    
    precision, recall, _ = precision_recall_curve(y_true, y_prob)
    metrics['pr_auc'] = np.trapz(precision, recall)
    
    return metrics


def load_predictions(data_dir):
    """Load model predictions from files."""
    predictions_path = data_dir / "predictions.json"
    
    if not predictions_path.exists():
        raise FileNotFoundError(f"Predictions file not found: {predictions_path}")
    
    with open(predictions_path, "r") as f:
        return json.load(f)


def evaluate_and_save(data_dir, output_dir):
    """Run evaluation and save metrics."""
    data_dir = Path(data_dir)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    
    predictions = load_predictions(data_dir)
    
    y_true = np.array(predictions['y_true'])
    y_pred = np.array(predictions['y_pred'])
    y_prob = np.array(predictions['y_prob'])
    
    metrics = compute_metrics(y_true, y_pred, y_prob)
    
    metrics['threshold_optimized'] = predictions.get('threshold', 0.5)
    metrics['n_samples'] = len(y_true)
    
    metrics_path = output_dir / "metrics_table.csv"
    with open(metrics_path, "w") as f:
        f.write("metric,value\n")
        for key, value in metrics.items():
            f.write(f"{key},{value}\n")
    
    print("Evaluation Metrics:")
    for key, value in metrics.items():
        print(f"  {key}: {value:.4f}")
    
    return metrics


def main():
    parser = argparse.ArgumentParser(description="Evaluate wine quality classification")
    parser.add_argument("--data-dir", type=str, required=True, help="Directory containing predictions")
    parser.add_argument("--output-dir", type=str, required=True, help="Directory to save metrics")
    
    args = parser.parse_args()
    
    try:
        metrics = evaluate_and_save(args.data_dir, args.output_dir)
        return 0
    except Exception as e:
        print(f"Error during evaluation: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
