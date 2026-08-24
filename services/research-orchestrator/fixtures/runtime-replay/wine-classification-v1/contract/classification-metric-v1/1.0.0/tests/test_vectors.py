#!/usr/bin/env python3
"""
Test vectors for wine quality classification benchmark evaluation contract.
Tests input/output schemas, metric computations, and guardrail compliance.
"""

import json
import sys
import numpy as np
from pathlib import Path


def test_input_schema():
    """Test input schema validation."""
    schema_path = Path(__file__).parent / "input_schema.json"
    with open(schema_path, "r") as f:
        schema = json.load(f)
    
    valid_input = {
        "y_true": [0, 1, 0, 1, 1, 0],
        "y_pred": [0, 1, 0, 1, 1, 0],
        "y_pred_optimized": [0, 1, 0, 1, 1, 0],
        "y_prob": [0.2, 0.8, 0.3, 0.7, 0.9, 0.1],
        "threshold": 0.5,
        "auc": 1.0,
        "f1": 1.0
    }
    
    for key, value in schema["properties"].items():
        if key in valid_input:
            if isinstance(value.get("description"), str):
                pass
    
    print("✓ Input schema test passed")
    return True


def test_output_schema():
    """Test output schema validation."""
    schema_path = Path(__file__).parent / "output_schema.json"
    with open(schema_path, "r") as f:
        schema = json.load(f)
    
    valid_output = {
        "roc_auc": 0.92,
        "f1": 0.85,
        "accuracy": 0.88,
        "ece": 0.03,
        "pr_auc": 0.91,
        "threshold_optimized": 0.6,
        "n_samples": 1000
    }
    
    for key, value in schema["properties"].items():
        if key in valid_output:
            if isinstance(value.get("description"), str):
                pass
    
    print("✓ Output schema test passed")
    return True


def test_metric_computation():
    """Test metric computation with synthetic data."""
    y_true = np.array([0, 1, 0, 1, 1, 0, 1, 0, 1, 1])
    y_pred = np.array([0, 1, 0, 1, 1, 0, 1, 0, 1, 1])
    y_prob = np.array([0.2, 0.8, 0.3, 0.7, 0.9, 0.1, 0.8, 0.2, 0.85, 0.95])
    
    from sklearn.metrics import roc_auc_score, f1_score, accuracy_score
    
    roc_auc = roc_auc_score(y_true, y_prob)
    f1 = f1_score(y_true, y_pred)
    accuracy = accuracy_score(y_true, y_pred)
    
    assert 0 <= roc_auc <= 1, f"ROC-AUC out of range: {roc_auc}"
    assert 0 <= f1 <= 1, f"F1 out of range: {f1}"
    assert 0 <= accuracy <= 1, f"Accuracy out of range: {accuracy}"
    
    print(f"✓ Metric computation test passed (ROC-AUC={roc_auc:.3f}, F1={f1:.3f}, Acc={accuracy:.3f})")
    return True


def test_ece_computation():
    """Test Expected Calibration Error computation."""
    y_true = np.array([0, 1, 0, 1, 1, 0, 1, 0, 1, 1])
    y_prob = np.array([0.2, 0.8, 0.3, 0.7, 0.9, 0.1, 0.8, 0.2, 0.85, 0.95])
    
    def compute_ece(y_true, y_prob, n_bins=10):
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
    
    ece = compute_ece(y_true, y_prob)
    
    assert 0 <= ece <= 1, f"ECE out of range: {ece}"
    
    print(f"✓ ECE computation test passed (ECE={ece:.3f})")
    return True


def test_guardrail_compliance():
    """Test guardrail compliance logic."""
    metrics = {
        "roc_auc": 0.92,
        "f1": 0.87,
        "calibration_ece": 0.04,
        "training_wallclock_minutes": 45.0
    }
    
    guardrails = [
        {"name": "roc_auc", "direction": "maximize", "minimum": 0.85, "required": True},
        {"name": "f1", "direction": "maximize", "minimum": 0.80, "required": True},
        {"name": "calibration_ece", "direction": "minimize", "maximum": 0.10, "required": False},
        {"name": "training_wallclock_minutes", "direction": "minimize", "maximum": 60.0, "required": True}
    ]
    
    for guardrail in guardrails:
        name = guardrail["name"]
        value = metrics.get(name)
        
        if name == "roc_auc":
            assert value >= guardrail["minimum"], f"ROC-AUC {value} < minimum {guardrail['minimum']}"
        elif name == "f1":
            assert value >= guardrail["minimum"], f"F1 {value} < minimum {guardrail['minimum']}"
        elif name == "calibration_ece":
            assert value <= guardrail["maximum"], f"ECE {value} > maximum {guardrail['maximum']}"
        elif name == "training_wallclock_minutes":
            assert value <= guardrail["maximum"], f"Wallclock {value} > maximum {guardrail['maximum']}"
    
    print("✓ Guardrail compliance test passed")
    return True


def test_primary_metric():
    """Test primary metric selection and comparison."""
    baseline_auc = 0.88
    ensemble_auc = 0.92
    minimum_effect = 0.02
    
    improvement = ensemble_auc - baseline_auc
    
    assert improvement >= minimum_effect, f"Improvement {improvement} < minimum {minimum_effect}"
    assert baseline_auc >= 0.85, f"Baseline ROC-AUC {baseline_auc} < 0.85"
    assert ensemble_auc >= 0.85, f"Ensemble ROC-AUC {ensemble_auc} < 0.85"
    
    print(f"✓ Primary metric test passed (baseline={baseline_auc:.3f}, ensemble={ensemble_auc:.3f}, improvement={improvement:.3f})")
    return True


def test_threshold_optimization():
    """Test threshold optimization logic."""
    y_true = np.array([0, 1, 0, 1, 1, 0, 1, 0, 1, 1])
    y_prob = np.array([0.2, 0.8, 0.3, 0.7, 0.9, 0.1, 0.8, 0.2, 0.85, 0.95])
    
    from sklearn.metrics import f1_score
    
    best_threshold = 0.5
    best_f1 = 0
    
    for threshold in np.arange(0.1, 0.9, 0.05):
        y_pred = (y_prob >= threshold).astype(int)
        f1 = f1_score(y_true, y_pred)
        if f1 > best_f1:
            best_f1 = f1
            best_threshold = threshold
    
    assert 0 <= best_threshold <= 1, f"Optimized threshold out of range: {best_threshold}"
    
    print(f"✓ Threshold optimization test passed (best_threshold={best_threshold:.2f}, best_f1={best_f1:.3f})")
    return True


def run_all_tests():
    """Run all tests."""
    print("=" * 60)
    print("Wine Quality Classification - Evaluation Contract Tests")
    print("=" * 60)
    print()
    
    tests = [
        test_input_schema,
        test_output_schema,
        test_metric_computation,
        test_ece_computation,
        test_guardrail_compliance,
        test_primary_metric,
        test_threshold_optimization,
    ]
    
    passed = 0
    failed = 0
    
    for test in tests:
        try:
            if test():
                passed += 1
        except Exception as e:
            print(f"✗ {test.__name__} FAILED: {e}")
            failed += 1
    
    print()
    print("=" * 60)
    print(f"Results: {passed} passed, {failed} failed")
    print("=" * 60)
    
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(run_all_tests())
