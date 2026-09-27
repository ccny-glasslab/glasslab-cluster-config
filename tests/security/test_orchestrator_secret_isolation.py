"""Manifest invariants for the research-orchestrator secret/egress boundary.

Issue #597 (CRITICAL): the orchestrator runs uvicorn (PID 1) and spawns the
same-UID ``opencode serve`` child in one container, so any control-plane
secret left in the orchestrator's environment is readable by the agent through
``/proc/1/environ``. These tests pin the immediate mitigation:

* the ``glasslab-research-orchestrator`` Secret is projected as a read-only
  file volume (mode 0400) under ``/etc/glasslab-secrets`` and is no longer
  referenced from ``envFrom``/``env``;
* the deployment points ``GLASSLAB_ORCHESTRATOR_SECRETS_DIR`` at that mount;
* an egress NetworkPolicy default-denies everything except DNS, the
  in-namespace data services, the local exo endpoints, and TCP 443 (the
  documented residual);
* the rollout script applies the egress policy.

The second-container UID split is a follow-up; these tests only cover the
immediate, mergeable mitigation.
"""

from __future__ import annotations

import unittest
from pathlib import Path

import yaml


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
KUBE_ROOT = REPOSITORY_ROOT / "kubeadm" / "glasslab-v2"
ORCHESTRATOR_ROOT = KUBE_ROOT / "research-orchestrator"
DEPLOYMENT_PATH = ORCHESTRATOR_ROOT / "20-deployment.yaml"
SECRET_EXAMPLE_PATH = ORCHESTRATOR_ROOT / "11-secret.example.yaml"
EGRESS_POLICY_PATH = ORCHESTRATOR_ROOT / "45-egress-network-policy.yaml"
ROLLOUT_PATH = REPOSITORY_ROOT / "scripts" / "rollout-research-services.sh"

SECRET_NAME = "glasslab-research-orchestrator"
SECRETS_MOUNT_PATH = "/etc/glasslab-secrets"
CONTROL_PLANE_KEYS = (
    "GLASSLAB_ORCHESTRATOR_STORE_POSTGRES_DSN",
    "GLASSLAB_ORCHESTRATOR_OPERATOR_API_TOKEN",
    "GLASSLAB_ORCHESTRATOR_DISCORD_BOT_TOKEN",
    "GLASSLAB_ORCHESTRATOR_DISCORD_WEBHOOK_URL",
    "GLASSLAB_ORCHESTRATOR_LINK_SIGNING_SECRET",
)
ORCHESTRATOR_LABELS = {
    "app.kubernetes.io/name": "glasslab-research-orchestrator"
}


def documents(path: Path) -> list[dict]:
    return [item for item in yaml.safe_load_all(path.read_text(encoding="utf-8")) if item]


def deployment() -> dict:
    return next(
        item for item in documents(DEPLOYMENT_PATH) if item["kind"] == "Deployment"
    )


def orchestrator_container() -> dict:
    containers = deployment()["spec"]["template"]["spec"]["containers"]
    return next(item for item in containers if item["name"] == "orchestrator")


def env_by_name(container: dict) -> dict[str, dict]:
    return {item["name"]: item for item in container.get("env", [])}


class DeploymentSecretVolumeTests(unittest.TestCase):
    def test_orchestrator_secret_is_not_referenced_from_env(self) -> None:
        container = orchestrator_container()
        # The entire orchestrator Secret must leave the environment; a same-UID
        # child can read /proc/1/environ.
        for ref in container.get("envFrom", []):
            secret_ref = ref.get("secretRef")
            if secret_ref is not None:
                self.assertNotEqual(secret_ref.get("name"), SECRET_NAME)
        environment = env_by_name(container)
        for key in CONTROL_PLANE_KEYS:
            self.assertNotIn(key, environment)
        for name, entry in environment.items():
            secret_ref = entry.get("valueFrom", {}).get("secretKeyRef", {})
            self.assertNotEqual(
                secret_ref.get("name"),
                SECRET_NAME,
                f"{name} still projects the orchestrator secret into the env",
            )

    def test_configmap_reference_is_retained(self) -> None:
        configmap_refs = {
            ref["configMapRef"]["name"]
            for ref in orchestrator_container().get("envFrom", [])
            if "configMapRef" in ref
        }
        self.assertIn("glasslab-research-orchestrator-config", configmap_refs)

    def test_secret_is_projected_as_a_read_only_file_volume(self) -> None:
        container = orchestrator_container()
        mounts = {
            mount["mountPath"]: mount
            for mount in container.get("volumeMounts", [])
        }
        self.assertIn(SECRETS_MOUNT_PATH, mounts)
        mount = mounts[SECRETS_MOUNT_PATH]
        self.assertEqual(mount["name"], "control-plane-secrets")
        self.assertTrue(mount.get("readOnly"), "secret mount must be read-only")

        volumes = {
            volume["name"]: volume
            for volume in deployment()["spec"]["template"]["spec"]["volumes"]
        }
        secret_volume = volumes["control-plane-secrets"]["secret"]
        self.assertEqual(secret_volume["secretName"], SECRET_NAME)
        # 0400 (PyYAML resolves the YAML octal literal to 256).
        self.assertEqual(int(secret_volume["defaultMode"]), 0o400)
        projected = {item["key"] for item in secret_volume["items"]}
        self.assertEqual(projected, set(CONTROL_PLANE_KEYS))

    def test_secrets_dir_env_points_at_the_mount(self) -> None:
        environment = env_by_name(orchestrator_container())
        self.assertIn("GLASSLAB_ORCHESTRATOR_SECRETS_DIR", environment)
        self.assertEqual(
            environment["GLASSLAB_ORCHESTRATOR_SECRETS_DIR"]["value"],
            SECRETS_MOUNT_PATH,
        )
        # A literal deployment value, not a reference back into the Secret.
        self.assertNotIn(
            "valueFrom", environment["GLASSLAB_ORCHESTRATOR_SECRETS_DIR"]
        )

    def test_secret_contract_documents_the_file_keys_and_signing_secret(
        self,
    ) -> None:
        contract = documents(SECRET_EXAMPLE_PATH)[0]["secret_contract"]
        self.assertEqual(contract["metadata"]["name"], SECRET_NAME)
        required = set(contract["required_keys"])
        for key in CONTROL_PLANE_KEYS:
            self.assertIn(key, required)

    def test_service_account_token_is_not_automounted(self) -> None:
        pod_spec = deployment()["spec"]["template"]["spec"]
        self.assertIs(pod_spec["automountServiceAccountToken"], False)


class EgressNetworkPolicyTests(unittest.TestCase):
    def policy(self) -> dict:
        return documents(EGRESS_POLICY_PATH)[0]

    def test_policy_selects_the_orchestrator_and_is_egress_only(self) -> None:
        policy = self.policy()
        self.assertEqual(policy["kind"], "NetworkPolicy")
        self.assertEqual(policy["spec"]["podSelector"]["matchLabels"], ORCHESTRATOR_LABELS)
        self.assertEqual(policy["spec"]["policyTypes"], ["Egress"])

    def test_default_deny_except_for_an_explicit_allow_set(self) -> None:
        egress = self.policy()["spec"]["egress"]
        self.assertTrue(egress, "an egress allow-list is required")
        # Every rule must be explicit; an empty rule with no peers would
        # re-open egress to everything.
        for rule in egress:
            self.assertTrue(
                rule.get("to") or rule.get("ports"),
                "an egress rule must name a peer or a port",
            )

    def test_dns_is_allowed_from_kube_system(self) -> None:
        egress = self.policy()["spec"]["egress"]
        dns_rule = next(
            rule
            for rule in egress
            if any(
                peer.get("namespaceSelector", {})
                .get("matchLabels", {})
                .get("kubernetes.io/metadata.name")
                == "kube-system"
                for peer in rule.get("to", [])
            )
        )
        ports = {
            (port["protocol"], port["port"]) for port in dns_rule["ports"]
        }
        self.assertIn(("UDP", 53), ports)
        self.assertIn(("TCP", 53), ports)

    def test_in_namespace_data_services_and_workflow_api_are_allowed(
        self,
    ) -> None:
        egress = self.policy()["spec"]["egress"]
        allowed = set()
        for rule in egress:
            for peer in rule.get("to", []):
                labels = peer.get("podSelector", {}).get("matchLabels", {})
                name = labels.get("app.kubernetes.io/name")
                if name:
                    for port in rule.get("ports", []):
                        allowed.add((name, port["port"]))
        for name, port in (
            ("glasslab-postgres", 5432),
            ("glasslab-minio", 9000),
            ("glasslab-nats", 4222),
            ("glasslab-rabbitmq", 5672),
            ("glasslab-workflow-api", 8080),
        ):
            self.assertIn((name, port), allowed)

    def test_local_exo_endpoints_are_allowed(self) -> None:
        egress = self.policy()["spec"]["egress"]
        exo = None
        for rule in egress:
            cidrs = {
                peer["ipBlock"]["cidr"]
                for peer in rule.get("to", [])
                if "ipBlock" in peer
            }
            if {"192.168.1.17/32", "192.168.1.18/32"} <= cidrs:
                exo = rule
                break
        self.assertIsNotNone(exo, "the local exo endpoints must be reachable")
        self.assertEqual(
            {(port["protocol"], port["port"]) for port in exo["ports"]},
            {("TCP", 52417)},
        )

    def test_tcp_443_is_the_documented_residual(self) -> None:
        egress = self.policy()["spec"]["egress"]
        https_rules = [
            rule
            for rule in egress
            if any(
                port.get("protocol") == "TCP" and port.get("port") == 443
                for port in rule.get("ports", [])
            )
        ]
        self.assertTrue(https_rules, "TCP 443 egress must be documented")
        # The documented residual must not be an unqualified allow-all: it is
        # pinned to port 443 only.
        self.assertIn("residual", EGRESS_POLICY_PATH.read_text(encoding="utf-8").lower())

    def test_rollout_applies_the_egress_policy(self) -> None:
        rollout = ROLLOUT_PATH.read_text(encoding="utf-8")
        self.assertIn(
            "research-orchestrator/45-egress-network-policy.yaml", rollout
        )


if __name__ == "__main__":
    unittest.main()
