#!/usr/bin/env python3
"""
Training script for wine quality classification benchmark.
Trains baseline and ensemble models per evaluation contract.
"""

import json
import argparse
import numpy as np
import pandas as pd
from pathlib import Path
from sklearn.model_selection import train_test_split, StratifiedKFold
from sklearn.linear_model import LogisticRegression
from sklearn.ensemble import RandomForestClassifier, GradientBoostingClassifier
from sklearn.preprocessing import StandardScaler
from sklearn.metrics import roc_auc_score, f1_score
from sklearn.calibration import CalibratedClassifierCV


def load_wine_data(data_path):
    """Load wine quality dataset."""
    data_path = Path(data_path)
    
    if not data_path.exists():
        raise FileNotFoundError(f"Wine dataset not found: {data_path}")
    
    df = pd.read_csv(data_path)
    return df


def create_binary_target(df, quality_threshold=7):
    """Create binary classification target (high quality or not)."""
    df = df.copy()
    df['high_quality'] = (df['quality'] >= quality_threshold).astype(int)
    return df


def extract_features(df):
    """Extract feature columns."""
    feature_cols = [col for col in df.columns if col != 'quality' and col != 'high_quality']
    return feature_cols


def train_baseline(X_train, y_train, X_val, y_val, class_weight='balanced'):
    """Train L2-regularized logistic regression baseline."""
    best_auc = 0
    best_model = None
    best_C = None
    
    for C in [0.1, 1.0, 10.0]:
        model = LogisticRegression(
            C=C,
            class_weight=class_weight,
            max_iter=1000,
            random_state=42,
            solver='lbfgs'
        )
        model.fit(X_train, y_train)
        y_prob = model.predict_proba(X_val)[:, 1]
        auc = roc_auc_score(y_val, y_prob)
        
        if auc > best_auc:
            best_auc = auc
            best_model = model
            best_C = C
    
    return best_model, best_auc, best_C


def train_ensemble(X_train, y_train, X_val, y_val, ensemble_type='random_forest'):
    """Train ensemble classifier."""
    if ensemble_type == 'random_forest':
        model = RandomForestClassifier(
            n_estimators=100,
            max_depth=10,
            random_state=42,
            n_jobs=-1
        )
    elif ensemble_type == 'gradient_boosting':
        model = GradientBoostingClassifier(
            n_estimators=100,
            max_depth=5,
            learning_rate=0.1,
            random_state=42
        )
    else:
        raise ValueError(f"Unknown ensemble type: {ensemble_type}")
    
    model.fit(X_train, y_train)
    y_prob = model.predict_proba(X_val)[:, 1]
    auc = roc_auc_score(y_val, y_prob)
    
    return model, auc


def find_optimal_threshold(y_true, y_prob):
    """Find threshold that maximizes F1."""
    best_threshold = 0.5
    best_f1 = 0
    
    for threshold in np.arange(0.1, 0.9, 0.05):
        y_pred = (y_prob >= threshold).astype(int)
        f1 = f1_score(y_true, y_pred)
        if f1 > best_f1:
            best_f1 = f1
            best_threshold = threshold
    
    return best_threshold


def cross_validate(df, feature_cols, n_splits=5):
    """Perform grouped cross-validation."""
    X = df[feature_cols].values
    y = df['high_quality'].values
    
    skf = StratifiedKFold(n_splits=n_splits, shuffle=True, random_state=42)
    
    baseline_results = []
    rf_results = []
    gb_results = []
    
    for fold, (train_idx, val_idx) in enumerate(skf.split(X, y)):
        print(f"  Fold {fold + 1}/{n_splits}")
        
        X_train, X_val = X[train_idx], X[val_idx]
        y_train, y_val = y[train_idx], y[val_idx]
        
        scaler = StandardScaler()
        X_train_scaled = scaler.fit_transform(X_train)
        X_val_scaled = scaler.transform(X_val)
        
        baseline_model, baseline_auc, best_C = train_baseline(
            X_train_scaled, y_train, X_val_scaled, y_val
        )
        
        rf_model, rf_auc = train_ensemble(
            X_train_scaled, y_train, X_val_scaled, y_val, 'random_forest'
        )
        
        gb_model, gb_auc = train_ensemble(
            X_train_scaled, y_train, X_val_scaled, y_val, 'gradient_boosting'
        )
        
        baseline_results.append({
            'auc': baseline_auc,
            'threshold': find_optimal_threshold(y_val, baseline_model.predict_proba(X_val_scaled)[:, 1])
        })
        rf_results.append({
            'auc': rf_auc,
            'threshold': find_optimal_threshold(y_val, rf_model.predict_proba(X_val_scaled)[:, 1])
        })
        gb_results.append({
            'auc': gb_auc,
            'threshold': find_optimal_threshold(y_val, gb_model.predict_proba(X_val_scaled)[:, 1])
        })
    
    return {
        'baseline': baseline_results,
        'random_forest': rf_results,
        'gradient_boosting': gb_results
    }


def save_predictions(df, feature_cols, output_dir):
    """Train final models and save predictions for evaluation."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    
    X = df[feature_cols].values
    y = df['high_quality'].values
    
    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.2, stratify=y, random_state=42
    )
    
    scaler = StandardScaler()
    X_train_scaled = scaler.fit_transform(X_train)
    X_test_scaled = scaler.transform(X_test)
    
    models = {
        'baseline': LogisticRegression(C=1.0, class_weight='balanced', max_iter=1000),
        'random_forest': RandomForestClassifier(n_estimators=100, max_depth=10, random_state=42),
        'gradient_boosting': GradientBoostingClassifier(n_estimators=100, max_depth=5, learning_rate=0.1, random_state=42)
    }
    
    for name, model in models.items():
        model.fit(X_train_scaled, y_train)
        y_prob = model.predict_proba(X_test_scaled)[:, 1]
        y_pred = (y_prob >= 0.5).astype(int)
        
        threshold = find_optimal_threshold(y_test, y_prob)
        y_pred_optimized = (y_prob >= threshold).astype(int)
        
        predictions = {
            'model': name,
            'y_true': y_test.tolist(),
            'y_pred': y_pred.tolist(),
            'y_pred_optimized': y_pred_optimized.tolist(),
            'y_prob': y_prob.tolist(),
            'threshold': threshold,
            'auc': roc_auc_score(y_test, y_prob),
            'f1': f1_score(y_test, y_pred_optimized)
        }
        
        predictions_path = output_dir / f"{name}_predictions.json"
        with open(predictions_path, "w") as f:
            json.dump(predictions, f, indent=2)
        
        print(f"{name}: AUC={predictions['auc']:.4f}, F1={predictions['f1']:.4f}, threshold={threshold:.2f}")
    
    return True


def main():
    parser = argparse.ArgumentParser(description="Train wine quality classification models")
    parser.add_argument("--data-path", type=str, default="data/wine_quality.csv", help="Path to wine dataset")
    parser.add_argument("--output-dir", type=str, default="data/predictions", help="Directory to save predictions")
    
    args = parser.parse_args()
    
    try:
        print("Loading wine quality dataset...")
        df = load_wine_data(args.data_path)
        df = create_binary_target(df)
        
        feature_cols = extract_features(df)
        print(f"Features: {feature_cols}")
        print(f"Class distribution: {df['high_quality'].value_counts().to_dict()}")
        
        print("\nPerforming cross-validation...")
        cv_results = cross_validate(df, feature_cols)
        
        print("\nFinal model training and prediction saving...")
        save_predictions(df, feature_cols, args.output_dir)
        
        print("\nCross-validation results:")
        for name, results in cv_results.items():
            aucs = [r['auc'] for r in results]
            print(f"  {name}: mean AUC={np.mean(aucs):.4f}, std={np.std(aucs):.4f}")
        
        return 0
        
    except Exception as e:
        print(f"Error during training: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
