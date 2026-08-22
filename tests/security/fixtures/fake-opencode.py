#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import sys


prompt = sys.argv[-1]
base = re.search(r"^BASE_COMMIT=(.+)$", prompt, re.MULTILINE).group(1)
result = {
    "mode": "discover",
    "base_commit": base,
    "scope": "fixture",
    "inspected": ["tracked.txt"],
    "findings": [],
}
answer = f"```json\n{json.dumps(result)}\n```\nNo candidate findings."
print(json.dumps({"type": "text", "part": {"text": answer}}))
