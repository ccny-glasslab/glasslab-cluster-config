"""Security invariants for the research-orchestrator ingress NetworkPolicy."""

from __future__ import annotations

import unittest
from pathlib import Path

import yaml


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
KUBE_ROOT = REPOSITORY_ROOT / "kubeadm" / "glasslab-v2"
POLICY_PATH = KUBE_ROOT / "research-orchestrator" / "50-ingress-network-policy.yaml"
ROLLOUT_PATH = REPOSITORY_ROOT / "scripts" / "rollout-research-services.sh"


def documents(path: Path) -> list[dict]:
    return [item for item in yaml.safe_load_all(path.read_text(encoding="utf-8")) if item]


class OrchestratorNetworkPolicyTests(unittest.TestCase):
    def test_policy_is_ingress_only_and_selects_the_orchestrator(self) -> None:
        policy = documents(POLICY_PATH)[0]
        self.assertEqual(policy["kind"], "NetworkPolicy")
        self.assertEqual(
            policy["spec"]["podSelector"]["matchLabels"],
            {"app.kubernetes.io/name": "glasslab-research-orchestrator"},
        )
        self.assertEqual(policy["spec"]["policyTypes"], ["Ingress"])

    def test_only_the_node_may_reach_the_http_port_and_no_pods_are_allowed(self) -> None:
        policy = documents(POLICY_PATH)[0]
        ingress = policy["spec"]["ingress"]
        self.assertEqual(len(ingress), 1)
        self.assertEqual(ingress[0]["ports"], [{"protocol": "TCP", "port": 8080}])
        # The only peer is the node CIDR (kubelet probes). Any podSelector
        # peer would reopen pod-to-pod ingress, so none is permitted.
        self.assertEqual(
            ingress[0]["from"],
            [{"ipBlock": {"cidr": "192.168.1.47/32"}}],
        )

    def test_rollout_applies_the_orchestrator_ingress_policy(self) -> None:
        rollout = ROLLOUT_PATH.read_text(encoding="utf-8")
        self.assertIn(
            "research-orchestrator/50-ingress-network-policy.yaml", rollout
        )


if __name__ == "__main__":
    unittest.main()
