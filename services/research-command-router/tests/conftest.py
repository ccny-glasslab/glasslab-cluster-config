"""Test path setup for the research-command-router service.

The service has no installable package metadata, so tests must be able to import
`app` from a plain checkout: the service root is inserted into sys.path before
any test module is imported, whether pytest is invoked from the service
directory or from the repository root.
"""

import sys
from pathlib import Path

SERVICE_ROOT = Path(__file__).resolve().parents[1]

if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))
