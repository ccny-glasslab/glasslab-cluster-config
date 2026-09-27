"""Security invariants for orchestrator image supply chain and provisioner unit.

Issue #603 (low, defense in depth): the Hermes bootstrap executed as root at
image build time must be pinned by sha256 and its workspace patch must fail
closed instead of silently no-oping; and the provisioner's kubectl
port-forward service must not run as root when the kubeconfig it reads is
already owned by the glasslab account.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

import yaml


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = REPOSITORY_ROOT / "services" / "research-orchestrator" / "Dockerfile"
PROVISIONER_PLAYBOOK = REPOSITORY_ROOT / "ansible" / "playbooks" / "provisioner-admin.yml"
FORWARD_UNIT_TASK = "Install the research orchestrator port-forward unit"
LEGACY_WORKSPACE_GUARD = 'if [ -f "$INSTALL_DIR/package.json" ]; then'


class OrchestratorImageSupplyChainTests(unittest.TestCase):
    def dockerfile(self) -> str:
        return DOCKERFILE.read_text(encoding="utf-8")

    def test_hermes_bootstrap_is_pinned_by_sha256(self):
        dockerfile = self.dockerfile()
        match = re.search(
            r"^ARG HERMES_INSTALL_SHA256=([0-9a-f]{64})$", dockerfile, re.MULTILINE
        )
        self.assertIsNotNone(match, "the installer must be pinned by full sha256")
        self.assertIn("sha256sum -c -", dockerfile)

    def test_workspace_patch_is_fail_closed(self):
        dockerfile = self.dockerfile()
        # issue #603: a bare sed no-ops when upstream changes, so both the
        # presence check and the post-patch verification must guard the patch.
        self.assertIn(f"grep -qF '{LEGACY_WORKSPACE_GUARD}'", dockerfile)
        self.assertIn(f"! grep -qF '{LEGACY_WORKSPACE_GUARD}'", dockerfile)

    def test_pinned_revision_gets_only_supported_flags(self):
        dockerfile = self.dockerfile()
        run_block = dockerfile.split("curl -fsSL", 1)[1]
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
