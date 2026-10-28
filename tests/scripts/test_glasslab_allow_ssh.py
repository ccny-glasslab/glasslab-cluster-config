"""Coverage for the gateway's glasslab-allow-ssh helper.

The helper is the only thing a passwordless rule lets an operator (or an
agent) run to change the gateway's sshd allowlist, so its input validation is
the security boundary. The tests run it as a normal user against a scratch
file through its ``--hosts-file`` test hook.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
HELPER = REPO_ROOT / "live-config/gateway/usr/local/sbin/glasslab-allow-ssh"
SUDOERS = REPO_ROOT / "live-config/gateway/etc/sudoers.d/91-glasslab-allow-ssh"

BASE = "ALL: 134.74. 192.168.1.\nsshd: 203.0.113.10\n"


@unittest.skipIf(os.geteuid() == 0, "the --hosts-file test hook is refused as root")
class GlasslabAllowSshTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.hosts = self.tmp / "hosts.allow"
        self.hosts.write_text(BASE, encoding="utf-8")
        self.hosts.chmod(0o644)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp)

    def run_helper(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(HELPER), "--hosts-file", str(self.hosts), *args],
            capture_output=True,
            text=True,
            check=False,
        )

    def backups(self) -> list[Path]:
        return sorted(self.tmp.glob("hosts.allow.bak-*"))

    def test_appends_one_address_with_the_comment_on_its_own_line(self) -> None:
        result = self.run_helper("203.0.113.7", "researcher home (example ISP)")
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = self.hosts.read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines[:2], BASE.splitlines())
        self.assertEqual(len(lines), 4)
        self.assertRegex(
            lines[2],
            r"^# researcher home \(example ISP\), added \d{4}-\d{2}-\d{2} by \S+ via glasslab-allow-ssh$",
        )
        self.assertEqual(lines[3], "sshd: 203.0.113.7")
        (backup,) = self.backups()
        self.assertEqual(backup.read_text(encoding="utf-8"), BASE)
        self.assertEqual(stat.S_IMODE(self.hosts.stat().st_mode), 0o644)

    def test_forced_command_mode_takes_one_string(self) -> None:
        result = self.run_helper("--ssh-command", "203.0.113.7 researcher home")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.hosts.read_text(encoding="utf-8").endswith("sshd: 203.0.113.7\n"))

    def test_rejects_anything_but_a_plain_public_ipv4_address(self) -> None:
        for bad in (
            "203.0.113.0/24",
            "203.0.113.",
            ".example.net",
            "ALL",
            "203.0.113.7 EXCEPT 1.2.3.4",
            "203.0.113.007",
            "256.1.1.1",
            "1.2.3",
            "0.0.0.0",
            "127.0.0.1",
            "224.0.0.1",
            "::1",
            "203.0.113.7\nsshd: ALL",
            "",
        ):
            with self.subTest(ip=bad):
                result = self.run_helper(bad, "comment")
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(self.hosts.read_text(encoding="utf-8"), BASE)
        self.assertEqual(self.backups(), [])

    def test_rejects_unsafe_comments(self) -> None:
        for bad in (
            "",
            " leading space",
            "x" * 81,
            "home\nsshd: ALL",
            "home; rm -rf /",
            "home $(id)",
            "home `id`",
            "home # ALL",
            "home: ALL",
        ):
            with self.subTest(comment=bad):
                result = self.run_helper("203.0.113.7", bad)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(self.hosts.read_text(encoding="utf-8"), BASE)

    def test_forced_command_mode_rejects_extra_or_smuggled_fields(self) -> None:
        for bad in (
            "203.0.113.7",
            "203.0.113.7 home; sudo -n id",
            "203.0.113.7 home && id",
            "203.0.113.7 home\nsshd: ALL",
            "ALL comment",
            "  203.0.113.7 home",
        ):
            with self.subTest(request=bad):
                result = self.run_helper("--ssh-command", bad)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(self.hosts.read_text(encoding="utf-8"), BASE)

    def test_refuses_an_address_that_is_already_allowed(self) -> None:
        result = self.run_helper("203.0.113.10", "already there")
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertEqual(self.hosts.read_text(encoding="utf-8"), BASE)
        self.assertEqual(self.backups(), [])

    def test_a_similar_address_is_not_a_duplicate(self) -> None:
        result = self.run_helper("203.0.113.1", "different host")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_does_not_glue_onto_an_unterminated_last_line(self) -> None:
        self.hosts.write_text("sshd: 203.0.113.10", encoding="utf-8")
        result = self.run_helper("203.0.113.7", "home")
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = self.hosts.read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines[0], "sshd: 203.0.113.10")
        self.assertEqual(lines[-1], "sshd: 203.0.113.7")

    def test_refuses_a_symlinked_hosts_file(self) -> None:
        target = self.tmp / "elsewhere"
        target.write_text(BASE, encoding="utf-8")
        self.hosts.unlink()
        self.hosts.symlink_to(target)
        result = self.run_helper("203.0.113.7", "home")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(target.read_text(encoding="utf-8"), BASE)


class GlasslabAllowSshPackagingTests(unittest.TestCase):
    def test_helper_is_executable_and_strict(self) -> None:
        self.assertTrue(os.access(HELPER, os.X_OK))
        text = HELPER.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("#!/bin/bash\n"))
        self.assertIn("set -euo pipefail", text)
        subprocess.run(["bash", "-n", str(HELPER)], check=True)

    def test_sudoers_grants_only_the_helper(self) -> None:
        rules = [
            line
            for line in SUDOERS.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        self.assertEqual(
            rules,
            ["gr66ss-glasslab ALL=(root) NOPASSWD: /usr/local/sbin/glasslab-allow-ssh"],
        )


if __name__ == "__main__":
    unittest.main()
