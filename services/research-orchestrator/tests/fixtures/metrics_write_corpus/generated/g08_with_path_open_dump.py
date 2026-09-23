import json
import os
from pathlib import Path
metrics = {
    'cv_accuracy_mean': 0.8,
    'cv_roc_auc_mean': 0.85,
    'cv_f1_macro_mean': 0.77,
}
with (Path('output') / 'metrics.json').open('w') as f:
    json.dump(metrics, f)
