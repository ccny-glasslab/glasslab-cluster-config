"""Security invariants for orchestrator image supply chain and provisioner unit.

Issue #603 (low, defense in depth): the Hermes bootstrap executed as root at
image build time must be pinned by sha256 and its workspace patch must fail
closed instead of silently no-oping. Issue #611: that bootstrap is vendored
in-tree (no build-time network fetch) and the pin must match the vendored
bytes. The provisioner's kubectl port-forward service must not run as root
when the kubeconfig it reads is already owned by the glasslab account.
"""

from __future__ import annotations

import hashlib
import re
import unittest
from pathlib import Path

import yaml


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = REPOSITORY_ROOT / "services" / "research-orchestrator" / "Dockerfile"
VENDORED_INSTALLER = (
    REPOSITORY_ROOT
    / "services"
    / "research-orchestrator"
    / "vendor"
    / "install-hermes.sh"
)
INSTALLER_URL = "hermes-agent.nousresearch.com/install.sh"
PROVISIONER_PLAYBOOK = REPOSITORY_ROOT / "ansible" / "playbooks" / "provisioner-admin.yml"
FORWARD_UNIT_TASK = "Install the research orchestrator port-forward unit"
LEGACY_WORKSPACE_GUARD = 'if [ -f "$INSTALL_DIR/package.json" ]; then'


class OrchestratorImageSupplyChainTests(unittest.TestCase):
    def dockerfile(self) -> str:
        return DOCKERFILE.read_text(encoding="utf-8")

    def test_hermes_bootstrap_is_vendored_not_fetched(self):
        # issue #611: stop curling the root-executed installer at build time.
        dockerfile = self.dockerfile()
        self.assertNotIn(
            INSTALLER_URL,
            dockerfile,
            "the orchestrator image must not fetch the Hermes installer",
        )
        self.assertIn(
            "COPY services/research-orchestrator/vendor/install-hermes.sh"
            " /tmp/install-hermes.sh",
            dockerfile,
            "the build must COPY the vendored installer into the image",
        )

    def test_hermes_bootstrap_is_pinned_by_sha256(self):
        dockerfile = self.dockerfile()
        match = re.search(
            r"^ARG HERMES_INSTALL_SHA256=([0-9a-f]{64})$", dockerfile, re.MULTILINE
        )
        self.assertIsNotNone(match, "the installer must be pinned by full sha256")
        self.assertIn("sha256sum -c -", dockerfile)
        # issue #611: the pin must be the hash of the in-repo vendored bytes so
        # the build-time assertion guards the file that is actually COPYed.
        self.assertTrue(
            VENDORED_INSTALLER.is_file(), "the installer must be vendored in-tree"
        )
        vendored_hash = hashlib.sha256(VENDORED_INSTALLER.read_bytes()).hexdigest()
        self.assertEqual(match.group(1), vendored_hash)

    def test_workspace_patch_is_fail_closed(self):
        dockerfile = self.dockerfile()
        # issue #603: a bare sed no-ops when upstream changes, so both the
        # presence check and the post-patch verification must guard the patch.
        self.assertIn(f"grep -qF '{LEGACY_WORKSPACE_GUARD}'", dockerfile)
        self.assertIn(f"! grep -qF '{LEGACY_WORKSPACE_GUARD}'", dockerfile)

    def test_pinned_revision_gets_only_supported_flags(self):
        dockerfile = self.dockerfile()
        run_block = dockerfile.split("RUN apt-get update", 1)[1]
        self.assertIn("--commit", run_block)
        self.assertIn("--skip-setup", run_block)
        self.assertIn("--skip-browser", run_block)
        # The pinned bootstrap rejects unknown options, so the removed
        # --no-skills flag would abort the build rather than degrade.
        self.assertNotIn("--no-skills", dockerfile)
        self.assertIn("grep -qF -- '--skip-browser'", run_block)


class ProvisionerForwardUnitTests(unittest.TestCase):
    def forward_unit_content(self) -> str:
        playbook = yaml.safe_load(PROVISIONER_PLAYBOOK.read_text(encoding="utf-8"))
        task = next(
            item
            for item in playbook[0]["tasks"]
            if item.get("name") == FORWARD_UNIT_TASK
        )
        content = task["ansible.builtin.copy"]["content"]
        self.assertIsInstance(content, str)
        return content

    def test_unit_runs_as_glasslab_with_its_kubeconfig(self):
        content = self.forward_unit_content()
        self.assertIn("User=glasslab", content)
        self.assertIn("Group=glasslab", content)
        self.assertIn(
            "Environment=KUBECONFIG=/home/glasslab/.kube/config", content
        )
        self.assertNotIn("User=root", content)

    def test_unit_keeps_the_loopback_only_forward(self):
        content = self.forward_unit_content()
        self.assertIn("--address 127.0.0.1", content)
        self.assertIn("svc/glasslab-research-orchestrator 18080:8080", content)


if __name__ == "__main__":
    unittest.main()
