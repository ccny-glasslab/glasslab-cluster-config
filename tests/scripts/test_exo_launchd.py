from __future__ import annotations

import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
RUN_SCRIPT = REPO_ROOT / "scripts/macos/glasslab-exo-run.sh"


class ExoLaunchdControlFlowTests(unittest.TestCase):
    def test_master_starts_before_worker_peer_wait(self) -> None:
        script = RUN_SCRIPT.read_text()

        master_branch = script.index('if [[ "$ROLE" == "master" ]]')
        peer_wait = script.index('until /sbin/ping -S "$SELF_IP"')
        worker_api_wait = script.index('master_id=""')

        self.assertLess(
            master_branch,
            peer_wait,
            "the master must start without waiting for the worker peer",
        )
        self.assertLess(
            peer_wait,
            worker_api_wait,
            "the worker must still wait for peer reachability before the master API",
        )


if __name__ == "__main__":
    unittest.main()
