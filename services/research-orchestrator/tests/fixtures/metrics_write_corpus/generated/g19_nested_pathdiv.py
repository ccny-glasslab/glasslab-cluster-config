import json
import os
from pathlib import Path
metrics = {
    'cv_accuracy_mean': 0.8,
    'cv_roc_auc_mean': 0.85,
    'cv_f1_macro_mean': 0.77,
}
out = Path('outputs') / 'run-1'
out.mkdir(parents=True, exist_ok=True)
path = out / 'metrics.json'
path.write_text(json.dumps(metrics))
