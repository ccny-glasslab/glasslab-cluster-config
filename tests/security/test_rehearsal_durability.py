"""Durability invariants for the research-orchestrator rehearsal driver.

The rehearsal driver persists its checkpoint and SQLite store under
``REHEARSE_ROOT``. Inside the orchestrator pod ``/tmp`` is an ``emptyDir``,
so the deployment must point ``REHEARSE_ROOT`` at the shared artifacts PVC
or a rollout destroys an in-progress rehearsal.
"""

from __future__ import annotations

import unittest
from pathlib import Path

import yaml


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
KUBE_ROOT = REPOSITORY_ROOT / "kubeadm" / "glasslab-v2"
DEPLOYMENT = KUBE_ROOT / "research-orchestrator" / "20-deployment.yaml"
ORCHESTRATOR_CONTAINER = "orchestrator"
SHARED_PVC_MOUNT = "/mnt/artifacts"
EXPECTED_REHEARSE_ROOT = "/mnt/artifacts/research-orchestrator/rehearsal"


def documents(path: Path) -> list[dict]:
    return [item for item in yaml.safe_load_all(path.read_text(encoding="utf-8")) if item]


def container_for(path: Path, name: str) -> dict:
    deployment = next(item for item in documents(path) if item["kind"] == "Deployment")
    return next(
        item
        for item in deployment["spec"]["template"]["spec"]["containers"]
        if item["name"] == name
    )


def env_by_name(container: dict) -> dict[str, dict]:
    return {item["name"]: item for item in container.get("env", [])}


class RehearsalDurabilityManifestTests(unittest.TestCase):
    def test_orchestrator_env_pins_rehearse_root_on_shared_pvc(self):
        container = container_for(DEPLOYMENT, ORCHESTRATOR_CONTAINER)
        environment = env_by_name(container)

        self.assertIn("REHEARSE_ROOT", environment)
        self.assertEqual(
            environment["REHEARSE_ROOT"]["value"], EXPECTED_REHEARSE_ROOT
        )
        self.assertTrue(
            environment["REHEARSE_ROOT"]["value"].startswith(SHARED_PVC_MOUNT),
            "REHEARSE_ROOT must live under the shared artifacts PVC mount",
        )

    def test_orchestrator_mounts_shared_pvc_at_rehearse_root_parent(self):
        container = container_for(DEPLOYMENT, ORCHESTRATOR_CONTAINER)
        mounts = {
            mount["mountPath"]: mount["name"]
            for mount in container.get("volumeMounts", [])
        }
        self.assertIn(SHARED_PVC_MOUNT, mounts)
        self.assertEqual(mounts[SHARED_PVC_MOUNT], "artifacts-volume")

    def test_rehearse_root_is_explicit_env_not_configmap(self):
        container = container_for(DEPLOYMENT, ORCHESTRATOR_CONTAINER)
        configmap_refs = {
            ref["configMapRef"]["name"]
            for ref in container.get("envFrom", [])
            if "configMapRef" in ref
        }
        # REHEARSE_ROOT is a deployment-owned durability path, not operator
        # configuration supplied through the configmap.
        self.assertIn("glasslab-research-orchestrator-config", configmap_refs)
        self.assertIn("REHEARSE_ROOT", env_by_name(container))


if __name__ == "__main__":
    unittest.main()
