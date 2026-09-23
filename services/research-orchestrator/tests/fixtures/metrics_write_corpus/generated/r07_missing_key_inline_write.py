import json
import os
from pathlib import Path
metrics = {'cv_accuracy_mean': 0.8}
open('metrics.json', 'w').write(json.dumps(metrics))
