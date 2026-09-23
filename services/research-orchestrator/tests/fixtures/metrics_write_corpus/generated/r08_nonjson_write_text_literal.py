import json
import os
from pathlib import Path
path = Path('output') / 'metrics.json'
path.write_text('not json')
