"""Preflight coverage for the canonical checkout's write permissions.

Issue #599: the canonical checkout's group (glasslab) includes every
contributor account, and 1800+ tracked paths (manifests, scripts, .git/) were
group-writable, so any group member could substitute what the next human
rollout applies as a cluster admin. These tests lock the contract:

1. ``scripts/check-checkout-permissions.sh`` refuses other-writable paths and
   group-writable paths through a shared group (``--strict-group`` treats every
   group as shared), including under ``.git/``; symlinks are skipped.
2. ``scripts/rollout-research-services.sh`` refuses before any ``kubectl``
   call when its checkout is writable beyond its owner.
3. Both deploy scripts set ``umask 022`` so a ``--sync`` cannot reintroduce
   group-writable files.
"""

from __future__ import annotations

import grp
import os
import pwd
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.scripts.test_secret_permissions_preflight import (
    CHECKER,
    CHECKOUT_CHECKER,
    FAKE_KUBECTL,
    ROLLOUT,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY = REPO_ROOT / "scripts/deploy-glasslab-v2.sh"


def run_checker(root: Path, *flags: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(CHECKOUT_CHECKER), *flags, str(root)],
        check=False,
        capture_output=True,
        text=True,
    )


def owner_group_is_private() -> bool:
    user = pwd.getpwuid(os.getuid()).pw_name
    group = grp.getgrgid(os.getgid())
    return group.gr_name == user and not group.gr_mem


class CheckoutPermissionCheckerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "checkout"
        manifests = self.root / "kubeadm" / "glasslab-v2"
        manifests.mkdir(parents=True)
        (self.root / ".git" / "hooks").mkdir(parents=True)
        self.manifest = manifests / "20-deployment.yaml"
        self.manifest.write_text("kind: Deployment\n", encoding="utf-8")
        self.hook = self.root / ".git" / "hooks" / "post-merge"
        self.hook.write_text("#!/bin/sh\n", encoding="utf-8")
        for path in [self.root, *self.root.rglob("*")]:
            path.chmod(0o755 if path.is_dir() else 0o644)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_owner_only_tree_passes(self):
        completed = run_checker(self.root, "--strict-group")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stderr, "")

    def test_other_writable_file_is_refused(self):
        self.manifest.chmod(0o646)
        completed = run_checker(self.root)
        self.assertEqual(completed.returncode, 1)
        self.assertIn(str(self.manifest), completed.stderr)
        self.assertIn("other-writable", completed.stderr)
        self.assertIn("chmod -R go-w", completed.stderr)

    def test_group_writable_git_hook_is_refused_under_strict_group(self):
        self.hook.chmod(0o664)
        completed = run_checker(self.root, "--strict-group")
        self.assertEqual(completed.returncode, 1)
        self.assertIn(str(self.hook), completed.stderr)
        self.assertIn("group-writable", completed.stderr)

    def test_group_writable_directory_is_refused_under_strict_group(self):
        directory = self.manifest.parent
        directory.chmod(0o775)
        completed = run_checker(self.root, "--strict-group")
        self.assertEqual(completed.returncode, 1)
        self.assertIn(str(directory), completed.stderr)

    @unittest.skipUnless(owner_group_is_private(), "needs a user-private primary group")
    def test_user_private_group_write_passes_by_default(self):
        # A umask 002 workstation checkout is group-writable only to its owner.
        self.manifest.chmod(0o664)
        completed = run_checker(self.root)
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_symlinks_are_skipped(self):
        (self.root / "link").symlink_to(self.manifest)
        completed = run_checker(self.root, "--strict-group")
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_reports_every_offender_count(self):
        for index in range(25):
            path = self.root / f"loose-{index}.yaml"
            path.write_text("x\n", encoding="utf-8")
            path.chmod(0o666)
        completed = run_checker(self.root)
        self.assertEqual(completed.returncode, 1)
        self.assertIn("25 path(s)", completed.stderr)
        self.assertEqual(completed.stderr.count("refusing:"), 20)

    def test_never_prints_file_contents(self):
        self.manifest.write_text("SENTINEL-SECRET\n", encoding="utf-8")
        self.manifest.chmod(0o666)
        completed = run_checker(self.root)
        self.assertNotIn("SENTINEL-SECRET", completed.stdout + completed.stderr)


class RolloutCheckoutPermissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "scripts").mkdir()
        for script in (ROLLOUT, CHECKER, CHECKOUT_CHECKER):
            shutil.copy2(script, self.root / "scripts" / script.name)
        self.manifest = (
            self.root / "kubeadm" / "glasslab-v2" / "research-orchestrator" / "20-deployment.yaml"
        )
        self.manifest.parent.mkdir(parents=True)
        self.manifest.write_text("kind: Deployment\n", encoding="utf-8")
        self.kubectl = self.root / "kubectl"
        self.kubectl.write_text(FAKE_KUBECTL, encoding="utf-8")
        self.log = self.root / "kubectl.log"
        for path in [self.root, *self.root.rglob("*")]:
            path.chmod(0o755 if path.is_dir() or path.suffix == ".sh" or path == self.kubectl else 0o644)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_refuses_other_writable_manifest_before_any_kubectl_call(self):
        self.manifest.chmod(0o666)
        environment = os.environ.copy()
        environment["KUBECTL"] = str(self.kubectl)
        environment["KUBECTL_LOG"] = str(self.log)
        environment["GLASSLAB_V2_LOCAL_SECRETS_DIR"] = str(self.root / "no-secrets")
        completed = subprocess.run(
            [
                str(self.root / "scripts" / ROLLOUT.name),
                "--service", "research-orchestrator",
                "--tag", "0" * 40,
                "--no-wait-for-image",
                "--skip-smoke",
                "--skip-image-prune",
            ],
            cwd=self.root,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("check-checkout-permissions", completed.stderr)
        self.assertIn(str(self.manifest), completed.stderr)
        self.assertFalse(self.log.exists(), self.log.read_text() if self.log.exists() else "")


class UmaskTests(unittest.TestCase):
    def test_deploy_scripts_set_umask_022_before_any_work(self):
        for script in (DEPLOY, ROLLOUT):
            lines = script.read_text(encoding="utf-8").splitlines()
            code = [line for line in lines[:12] if line and not line.startswith("#")]
            self.assertEqual(code[:2], ["set -euo pipefail", "umask 022"], script.name)


if __name__ == "__main__":
    unittest.main()
