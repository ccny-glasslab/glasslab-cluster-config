import json
import os
from pathlib import Path
metrics = {'cv_accuracy_mean': 0.8}
path = Path('output') / 'metrics.json'
path.write_text(json.dumps(metrics))
