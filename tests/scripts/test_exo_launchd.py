from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


REPO_ROOT = Path(__file__).resolve().parents[2]
RUN_SCRIPT = REPO_ROOT / "scripts/macos/glasslab-exo-run.sh"


class ExoLaunchdControlFlowTests(unittest.TestCase):
    def _run_role(self, role: str) -> tuple[subprocess.CompletedProcess[str], list[str]]:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            event_log = root / "events"
            exo_repo = root / "exo"
            exo_bin = exo_repo / ".venv/bin/exo"
            exo_bin.parent.mkdir(parents=True)
            exo_bin.write_text("#!/bin/bash\nexit 0\n")
            exo_bin.chmod(0o755)

            shims = root / "shims"
            shims.mkdir()
            commands = {
                "ifconfig": "printf 'inet %s \\n' \"$GLASSLAB_EXO_SELF_IP\"\n",
                "ping": (
                    "echo ping >>\"$EVENT_LOG\"\n"
                    "count=$(cat \"$PING_COUNT\" 2>/dev/null || echo 0)\n"
                    "count=$((count + 1))\n"
                    "echo \"$count\" >\"$PING_COUNT\"\n"
                    "test \"$count\" -ge 2\n"
                ),
                "curl": (
                    "echo curl >>\"$EVENT_LOG\"\n"
                    "count=$(cat \"$CURL_COUNT\" 2>/dev/null || echo 0)\n"
                    "count=$((count + 1))\n"
                    "echo \"$count\" >\"$CURL_COUNT\"\n"
                    "if test \"$count\" -ge 2; then echo '\"peer-id\"'; fi\n"
                ),
                "caffeinate": "printf 'caffeinate %s\\n' \"$*\" >>\"$EVENT_LOG\"\n",
                "sleep": "echo sleep >>\"$EVENT_LOG\"\n",
            }
            for name, body in commands.items():
                path = shims / name
                path.write_text(f"#!/bin/bash\nset -eu\n{body}")
                path.chmod(0o755)

            script = RUN_SCRIPT.read_text()
            replacements = {
                "/sbin/ifconfig": str(shims / "ifconfig"),
                "/sbin/ping": str(shims / "ping"),
                "/usr/bin/curl": str(shims / "curl"),
                "/usr/bin/caffeinate": str(shims / "caffeinate"),
            }
            for production_path, shim_path in replacements.items():
                self.assertIn(production_path, script)
                script = script.replace(production_path, shim_path)
            harness = root / "glasslab-exo-run"
            harness.write_text(script)
            harness.chmod(0o755)

            completed = subprocess.run(
                [str(harness)],
                env={
                    **os.environ,
                    "PATH": f"{shims}:{os.environ['PATH']}",
                    "EVENT_LOG": str(event_log),
                    "PING_COUNT": str(root / "ping-count"),
                    "CURL_COUNT": str(root / "curl-count"),
                    "GLASSLAB_EXO_ROLE": role,
                    "GLASSLAB_EXO_SELF_IP": "192.168.0.2" if role == "master" else "192.168.0.1",
                    "GLASSLAB_EXO_PEER_IP": "192.168.0.1" if role == "master" else "192.168.0.2",
                    "GLASSLAB_EXO_HOME": str(root / "home"),
                    "GLASSLAB_EXO_REPO": str(exo_repo),
                },
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=5,
                check=False,
            )
            events = event_log.read_text().splitlines() if event_log.exists() else []
            return completed, events

    def test_master_starts_without_contacting_peer(self) -> None:
        completed, events = self._run_role("master")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(len(events), 1, events)
        self.assertNotIn("ping", events)
        self.assertNotIn("curl", events)
        self.assertIn(" -m --api-port 52415", events[0])
        self.assertNotIn("--no-api", events[0])

    def test_worker_waits_for_peer_then_api_before_starting(self) -> None:
        completed, events = self._run_role("worker")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            events[:5],
            ["ping", "sleep", "ping", "curl", "sleep"],
            events,
        )
        self.assertEqual(events[5], "curl", events)
        self.assertTrue(events[6].startswith("caffeinate "), events)
        self.assertIn("--no-api", events[6])
        self.assertIn(
            "--bootstrap-peers /ip4/192.168.0.2/tcp/54216/p2p/peer-id",
            events[6],
        )
        self.assertNotIn(" -m ", events[6])


if __name__ == "__main__":
    unittest.main()
