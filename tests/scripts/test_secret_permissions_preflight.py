"""Preflight coverage for live *.local.yaml Secret manifest permissions.

Issue #599: the canonical checkout is group-traversable and the live
`15-workflow-api.local.yaml` Secret was mode 0664 with database DSNs and
object-store keys. These tests lock the fail-closed contract:

1. ``scripts/check-secret-permissions.sh`` refuses any manifest with group or
   other permission bits and never prints file contents.
2. ``scripts/deploy-glasslab-v2.sh`` refuses before its first ``kubectl`` call
   when a group-readable manifest is present, and proceeds with 0600.
3. ``scripts/rollout-research-services.sh`` refuses before its first mutation
   with the same fixture and proceeds with 0600.

The deploy and rollout scripts are exercised end-to-end with a fake ``kubectl``
and the ``GLASSLAB_V2_LOCAL_SECRETS_DIR`` override both scripts honor.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CHECKER = REPO_ROOT / "scripts/check-secret-permissions.sh"
DEPLOY = REPO_ROOT / "scripts/deploy-glasslab-v2.sh"
ROLLOUT = REPO_ROOT / "scripts/rollout-research-services.sh"
SENTINEL = "postgresql://fixture:preflight-sentinel@db.invalid/app"

FAKE_KUBECTL = """#!/usr/bin/env bash
set -euo pipefail
if [[ -n "${KUBECTL_LOG:-}" ]]; then
  printf '%s\\n' "$*" >> "$KUBECTL_LOG"
fi
if [[ "${1:-}" == "apply" ]]; then
  cat >/dev/null
fi
exit 0
"""


def write_secret(directory: Path, name: str, mode: int) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(
        "apiVersion: v1\nkind: Secret\nstringData:\n  DATABASE_DSN: " + SENTINEL + "\n",
        encoding="utf-8",
    )
    path.chmod(mode)
    return path


def run_checker(*directories: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(CHECKER), *map(str, directories)],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


class SecretPermissionCheckerTests(unittest.TestCase):
    """The checker refuses anything a non-owner can read or write."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_group_writable_secret_is_refused(self):
        # Given: a live Secret manifest at mode 0664 (the drift found on the
        # provisioner canonical checkout).
        secret = write_secret(self.root, "15-workflow-api.local.yaml", 0o664)
        # When: the preflight runs.
        completed = run_checker(self.root)
        # Then: it refuses, names the path and mode, and never echoes contents.
        self.assertEqual(completed.returncode, 1)
        self.assertIn(str(secret), completed.stderr)
        self.assertIn("664", completed.stderr)
        self.assertNotIn(SENTINEL, completed.stdout + completed.stderr)

    def test_group_readable_secret_is_refused(self):
        # Given: a manifest readable by the shared `glasslab` group (0640).
        secret = write_secret(self.root, "35-research-orchestrator.local.yaml", 0o640)
        # When: the preflight runs.
        completed = run_checker(self.root)
        # Then: group-readable is refused as well; only owner-only passes.
        self.assertEqual(completed.returncode, 1)
        self.assertIn(str(secret), completed.stderr)
        self.assertIn("640", completed.stderr)

    def test_owner_only_secret_passes(self):
        # Given: a live Secret manifest at mode 0600.
        write_secret(self.root, "15-workflow-api.local.yaml", 0o600)
        # When: the preflight runs.
        completed = run_checker(self.root)
        # Then: it passes silently.
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout, "")
        self.assertEqual(completed.stderr, "")

    def test_every_offending_manifest_is_reported(self):
        # Given: two loose manifests and one owner-only manifest.
        loose_a = write_secret(self.root, "15-workflow-api.local.yaml", 0o664)
        loose_b = write_secret(self.root, "25-postgres.local.yaml", 0o666)
        write_secret(self.root, "35-research-orchestrator.local.yaml", 0o600)
        # When: the preflight runs.
        completed = run_checker(self.root)
        # Then: both offenders are named, not just the first.
        self.assertEqual(completed.returncode, 1)
        self.assertIn(str(loose_a), completed.stderr)
        self.assertIn(str(loose_b), completed.stderr)
        self.assertIn("666", completed.stderr)

    def test_missing_or_empty_directory_passes(self):
        # Given: a checkout without local Secret manifests.
        empty = self.root / "empty"
        empty.mkdir()
        # When: the preflight runs against an absent and an empty directory.
        completed = run_checker(empty, self.root / "missing")
        # Then: there is nothing to refuse.
        self.assertEqual(completed.returncode, 0, completed.stderr)


class DeployScriptPreflightTests(unittest.TestCase):
    """deploy-glasslab-v2.sh must gate every secret apply behind the preflight."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.secrets_dir = self.root / "secrets"
        self.kubectl = self.root / "kubectl"
        self.kubectl.write_text(FAKE_KUBECTL, encoding="utf-8")
        self.kubectl.chmod(0o755)
        self.log = self.root / "kubectl.log"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run(self) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment["KUBECTL"] = str(self.kubectl)
        environment["KUBECTL_LOG"] = str(self.log)
        environment["GLASSLAB_V2_LOCAL_SECRETS_DIR"] = str(self.secrets_dir)
        return subprocess.run(
            [str(DEPLOY)],
            cwd=REPO_ROOT,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )

    def _invocations(self) -> list[str]:
        if not self.log.exists():
            return []
        return [line for line in self.log.read_text(encoding="utf-8").splitlines() if line]

    def test_refuses_group_writable_secret_before_any_kubectl_call(self):
        # Given: a 0664 live Secret manifest.
        write_secret(self.secrets_dir, "15-workflow-api.local.yaml", 0o664)
        # When: the deploy runs.
        completed = self._run()
        # Then: it stops at the preflight, before registry validation or apply.
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("check-secret-permissions", completed.stderr)
        self.assertIn("664", completed.stderr)
        self.assertNotIn("validating workflow registry definitions", completed.stdout)
        self.assertEqual(self._invocations(), [])
        self.assertNotIn(SENTINEL, completed.stdout + completed.stderr)

    def test_proceeds_with_owner_only_secret(self):
        # Given: the same manifest locked to 0600.
        write_secret(self.secrets_dir, "15-workflow-api.local.yaml", 0o600)
        # When: the deploy runs.
        completed = self._run()
        # Then: the preflight passes and the deploy reaches kubectl apply.
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("validating workflow registry definitions", completed.stdout)
        self.assertTrue(any(line.startswith("apply ") for line in self._invocations()), self._invocations())


class RolloutScriptPreflightTests(unittest.TestCase):
    """rollout-research-services.sh must gate every mutation behind the preflight."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "scripts").mkdir()
        for script in (ROLLOUT, CHECKER):
            shutil.copy2(script, self.root / "scripts" / script.name)
        self.rollout = self.root / "scripts" / ROLLOUT.name
        manifests = self.root / "kubeadm" / "glasslab-v2" / "research-orchestrator"
        manifests.mkdir(parents=True)
        (manifests / "20-deployment.yaml").write_text(
            "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: fixture\n",
            encoding="utf-8",
        )
        self.secrets_dir = self.root / "kubeadm" / "glasslab-v2" / "secrets"
        self.kubectl = self.root / "kubectl"
        self.kubectl.write_text(FAKE_KUBECTL, encoding="utf-8")
        self.kubectl.chmod(0o755)
        self.log = self.root / "kubectl.log"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run(self) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment["KUBECTL"] = str(self.kubectl)
        environment["KUBECTL_LOG"] = str(self.log)
        environment["GLASSLAB_V2_LOCAL_SECRETS_DIR"] = str(self.secrets_dir)
        return subprocess.run(
            [
                str(self.rollout),
                "--service", "research-orchestrator",
                "--tag", "0000000000000000000000000000000000000000",
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

    def _invocations(self) -> list[str]:
        if not self.log.exists():
            return []
        return [line for line in self.log.read_text(encoding="utf-8").splitlines() if line]

    def test_refuses_group_writable_secret_before_any_mutation(self):
        # Given: a 0664 live Secret manifest in the (fixture) checkout.
        write_secret(self.secrets_dir, "15-workflow-api.local.yaml", 0o664)
        # When: the rollout runs.
        completed = self._run()
        # Then: it refuses before touching git or the cluster.
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("check-secret-permissions", completed.stderr)
        self.assertIn("664", completed.stderr)
        self.assertEqual(self._invocations(), [])
        self.assertNotIn(SENTINEL, completed.stdout + completed.stderr)

    def test_proceeds_with_owner_only_secret(self):
        # Given: the same manifest locked to 0600.
        write_secret(self.secrets_dir, "15-workflow-api.local.yaml", 0o600)
        # When: the rollout runs.
        completed = self._run()
        # Then: the preflight passes and the orchestrator image is rolled out.
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue(
            any(line.startswith("set image ") for line in self._invocations()),
            self._invocations(),
        )


if __name__ == "__main__":
    unittest.main()
