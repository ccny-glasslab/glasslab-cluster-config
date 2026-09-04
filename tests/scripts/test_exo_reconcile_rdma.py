"""Reconcile RDMA-aware placement decision tests.

The exo reconcile must not submit two-node JACCL placements when the local
Thunderbolt RDMA verbs device is not registered: a placement over a dead
queue-pair "verifies" (1-token probe succeeds) while real generations hang.
These tests lock the binary-path fix (the scripts hardcoded a non-existent
/usr/sbin/ibv_devices) and the RDMA gate on the topology decision.
"""

from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


REPO_ROOT = Path(__file__).resolve().parents[2]
RECONCILE_SCRIPT = REPO_ROOT / "scripts/macos/glasslab-exo-reconcile.sh"
GUARD_SCRIPT = REPO_ROOT / "scripts/macos/glasslab-exo-rdma-guard.sh"


class ExoReconcileRdmaGateTests(unittest.TestCase):
    def _source_functions(self) -> str:
        """Return the reconcile script's function definitions (pre-loop)."""
        script = RECONCILE_SCRIPT.read_text()
        # The reconcile script sources nothing; its functions + top-level
        # state precede the infinite `while true` loop. Cut the loop.
        return script.split("while true; do", 1)[0]

    def _run_function(
        self,
        fn_body: str,
        fn_call: str,
        ibv_body: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            shims = root / "shims"
            shims.mkdir()

            def _shim(name: str, body: str) -> None:
                path = shims / name
                path.write_text(f"#!/bin/bash\nset -eu\n{body}")
                path.chmod(0o755)

            _shim("curl", "exit 0\n")
            _shim("jq", "cat >/dev/null; exit 0\n")
            # Default ibv_devices shim: report rdma_en5 registered.
            _shim(
                "ibv_devices",
                ibv_body or "printf '    device        node GUID\\nrdma_en5    0123456789\\n'\n",
            )

            # Point the hardcoded binaries at the shims (real temp path).
            replacements = {
                "/usr/bin/curl": str(shims / "curl"),
                "/usr/bin/jq": str(shims / "jq"),
                "/usr/sbin/ibv_devices": str(shims / "ibv_devices"),
                "/usr/bin/ibv_devices": str(shims / "ibv_devices"),
            }
            for production_path, shim_path in replacements.items():
                if production_path in fn_body:
                    fn_body = fn_body.replace(production_path, shim_path)

            harness = root / "harness.sh"
            harness.write_text(
                "#!/bin/bash\n"
                "set -u\n"
                f"{fn_body}\n"
                f"{fn_call}\n"
            )
            harness.chmod(0o755)
            return subprocess.run(
                [str(harness)],
                env={
                    **os.environ,
                    "PATH": f"{shims}:{os.environ['PATH']}",
                    "GLASSLAB_EXO_API_BASE": "http://127.0.0.1:52415",
                    "GLASSLAB_EXO_MODEL": "mlx-community/Qwen3-Coder-Next-4bit",
                    "GLASSLAB_EXO_RDMA_IFACE": "rdma_en5",
                },
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
                check=False,
            )

    def test_reconcile_uses_correct_ibv_devices_path(self) -> None:
        script = RECONCILE_SCRIPT.read_text()
        self.assertIn("/usr/bin/ibv_devices", script)
        self.assertNotIn("/usr/sbin/ibv_devices", script)

    def test_guard_uses_correct_ibv_devices_path(self) -> None:
        script = GUARD_SCRIPT.read_text()
        self.assertIn("/usr/bin/ibv_devices", script)
        self.assertNotIn("/usr/sbin/ibv_devices", script)

    def test_topology_ok_true_when_rdma_registered_and_pair_up(self) -> None:
        fn_body = self._source_functions()
        state = (
            '{"topology":{"nodes":["a","b"],"connections":{"n":{'
            '"sourceRdmaIface":"rdma_en5","sinkRdmaIface":"rdma_en5"}}}}'
        )
        completed = self._run_function(fn_body, f"topology_ok '{state}'")
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_topology_ok_false_when_rdma_missing_but_pair_reported(self) -> None:
        """A 2-node libp2p topology must NOT be trusted when the local RDMA
        verbs device is unregistered — that is the exact wedge condition."""
        fn_body = self._source_functions()
        state = (
            '{"topology":{"nodes":["a","b"],"connections":{"n":{'
            '"sourceRdmaIface":"rdma_en5","sinkRdmaIface":"rdma_en5"}}}}'
        )
        completed = self._run_function(
            fn_body, f"topology_ok '{state}'", ibv_body="exit 1\n"
        )
        self.assertNotEqual(completed.returncode, 0, completed.stdout)

    def test_rdma_registered_helper_true_with_device(self) -> None:
        fn_body = self._source_functions()
        completed = self._run_function(fn_body, "rdma_registered")
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_rdma_registered_helper_false_without_device(self) -> None:
        fn_body = self._source_functions()
        completed = self._run_function(
            fn_body, "rdma_registered", ibv_body="exit 1\n"
        )
        self.assertNotEqual(completed.returncode, 0, completed.stdout)


if __name__ == "__main__":
    unittest.main()