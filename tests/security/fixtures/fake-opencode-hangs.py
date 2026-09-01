#!/usr/bin/env python3
from __future__ import annotations

import subprocess
import time
from pathlib import Path


child = subprocess.Popen(["sleep", "300"])
Path(".fake-child-pid").write_text(f"{child.pid}\n")
time.sleep(300)
