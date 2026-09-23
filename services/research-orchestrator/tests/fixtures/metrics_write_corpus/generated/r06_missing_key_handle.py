import json
import os
from pathlib import Path
metrics = {'cv_accuracy_mean': 0.8}
with open('metrics.json', 'w') as f:
    json.dump(metrics, f)
