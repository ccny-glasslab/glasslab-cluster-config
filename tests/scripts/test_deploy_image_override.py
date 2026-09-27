"""Issue #602: deploy-glasslab-v2.sh must deploy the checked-out commit.

The service manifests carry placeholder image tags (the pending-ci-build
marker or the last published main SHA). A plain ``kubectl apply`` of those
manifests would ship stale, pre-remediation code. These tests lock the
fail-closed contract:

1. Every service Deployment image is rewritten to the checked-out commit SHA
   before it reaches kubectl (same semantics as
   rollout-research-services.sh), and the placeholder tags are never applied.
2. ``GLASSLAB_V2_IMAGE_TAG`` overrides the resolved SHA for an explicit
   release.
3. A checkout without a resolvable HEAD fails before any cluster mutation.

The script is exercised end-to-end with a fake ``kubectl`` and the
``GLASSLAB_V2_LOCAL_SECRETS_DIR`` override it already honors.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY = REPO_ROOT / "scripts/deploy-glasslab-v2.sh"
CHECKER = REPO_ROOT / "scripts/check-secret-permissions.sh"

# (manifest path relative to the repo root, container name, image repository)
SERVICE_DEPLOYMENTS = (
    ("kubeadm/glasslab-v2/intake-agent/10-deployment.yaml", "intake-agent", "ghcr.io/ccny-glasslab/glasslab-intake-agent"),
    ("kubeadm/glasslab-v2/interpretation-agent/10-deployment.yaml", "interpretation-agent", "ghcr.io/ccny-glasslab/glasslab-interpretation-agent"),
    ("kubeadm/glasslab-v2/assessment-agent/10-deployment.yaml", "assessment-agent", "ghcr.io/ccny-glasslab/glasslab-assessment-agent"),
    ("kubeadm/glasslab-v2/design-agent/10-deployment.yaml", "design-agent", "ghcr.io/ccny-glasslab/glasslab-design-agent"),
    ("kubeadm/glasslab-v2/schedule-worker/10-deployment.yaml", "schedule-worker", "ghcr.io/ccny-glasslab/glasslab-schedule-worker"),
    ("kubeadm/glasslab-v2/workflow-api/20-deployment.yaml", "workflow-api", "ghcr.io/ccny-glasslab/glasslab-workflow-api"),
    ("kubeadm/glasslab-v2/research-orchestrator/20-deployment.yaml", "orchestrator", "ghcr.io/ccny-glasslab/glasslab-research-orchestrator"),
)

STALE_TAGS = ("pending-ci-build", "d2033ff8b683edaa9b5c3125b50e3a98a402bacc")

FAKE_KUBECTL = """#!/usr/bin/env bash
set -euo pipefail
if [[ -n "${KUBECTL_LOG:-}" ]]; then
  printf '%s\\n' "$*" >> "$KUBECTL_LOG"
fi
if [[ "${1:-}" == "apply" ]]; then
  # Drain stdin only for `apply -f -` (the set-image pipeline). A file apply
  # must not consume the caller loop's stdin or it would swallow the file list.
  for argument in "$@"; do
    if [[ "$argument" == "-" ]]; then
      cat >/dev/null
      break
    fi
  done
fi
exit 0
"""


def git_head() -> str:
    completed = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def write_secret(directory: Path, name: str, mode: int = 0o600) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text("apiVersion: v1\nkind: Secret\nstringData:\n  TOKEN: fixture\n", encoding="utf-8")
    path.chmod(mode)


class DeployImageOverrideTests(unittest.TestCase):
    """The deploy path must rewrite service images to the checked-out commit."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.secrets_dir = self.root / "secrets"
        write_secret(self.secrets_dir, "15-workflow-api.local.yaml")
        self.kubectl = self.root / "kubectl"
        self.kubectl.write_text(FAKE_KUBECTL, encoding="utf-8")
        self.kubectl.chmod(0o755)
        self.log = self.root / "kubectl.log"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _invocations(self) -> list[str]:
        if not self.log.exists():
            return []
        return [line for line in self.log.read_text(encoding="utf-8").splitlines() if line]

    def _run(self, script: Path = DEPLOY, **env: str) -> subprocess.CompletedProcess[str]:
        environment = {
            **os.environ,
            "KUBECTL": str(self.kubectl),
            "KUBECTL_LOG": str(self.log),
            "GLASSLAB_V2_LOCAL_SECRETS_DIR": str(self.secrets_dir),
            **env,
        }
        return subprocess.run(
            [str(script)],
            cwd=REPO_ROOT,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )

    def test_every_service_image_is_overridden_to_the_checked_out_commit(self):
        # Given: a clean checkout whose HEAD is the release candidate.
        head = git_head()
        # When: the plain deploy runs.
        completed = self._run()
        # Then: it succeeds and rewrites every service image to HEAD.
        self.assertEqual(completed.returncode, 0, completed.stderr)
        invocations = self._invocations()
        for manifest, container, repo in SERVICE_DEPLOYMENTS:
            with self.subTest(manifest=manifest):
                path = str(REPO_ROOT / manifest)
                expected = f"set image -f {path} {container}={repo}:{head} --local -o yaml"
                self.assertIn(expected, invocations)

    def test_placeholder_tags_never_reach_kubectl(self):
        # Given: a clean checkout.
        head = git_head()
        # When: the plain deploy runs.
        completed = self._run()
        # Then: no invocation still carries a placeholder tag, but HEAD does.
        self.assertEqual(completed.returncode, 0, completed.stderr)
        combined = "\n".join(self._invocations())
        for stale in STALE_TAGS:
            with self.subTest(stale=stale):
                self.assertNotIn(stale, combined)
        self.assertIn(head, combined)

    def test_explicit_image_tag_overrides_the_checked_out_commit(self):
        # Given: an operator pinning an explicit release tag.
        # When: the deploy runs with GLASSLAB_V2_IMAGE_TAG.
        completed = self._run(GLASSLAB_V2_IMAGE_TAG="release-tag-7")
        # Then: every service image uses the explicit tag.
        self.assertEqual(completed.returncode, 0, completed.stderr)
        invocations = self._invocations()
        for manifest, container, repo in SERVICE_DEPLOYMENTS:
            with self.subTest(manifest=manifest):
                path = str(REPO_ROOT / manifest)
                self.assertIn(f"set image -f {path} {container}={repo}:release-tag-7 --local -o yaml", invocations)

    def test_unresolvable_head_fails_before_any_kubectl_call(self):
        # Given: a checkout copy that is not a git repository and has no tag.
        fake_root = self.root / "checkout"
        (fake_root / "scripts").mkdir(parents=True)
        shutil.copy2(DEPLOY, fake_root / "scripts" / DEPLOY.name)
        shutil.copy2(CHECKER, fake_root / "scripts" / CHECKER.name)
        # When: the deploy runs there.
        completed = self._run(script=fake_root / "scripts" / DEPLOY.name)
        # Then: it refuses to deploy an unpinnable image and never calls kubectl.
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("cannot resolve", completed.stderr)
        self.assertEqual(self._invocations(), [])


if __name__ == "__main__":
    unittest.main()
