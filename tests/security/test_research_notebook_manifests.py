"""Security invariants for the personal GPU research notebooks."""

from __future__ import annotations

import ipaddress
import unittest
from pathlib import Path

import yaml


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
NOTEBOOK_ROOT = REPOSITORY_ROOT / "kubeadm" / "glasslab-v2" / "research-notebooks"
NAMESPACE = "glasslab-notebooks"
GATEWAY_LAN = "192.168.1.4/32"
NODE02 = "192.168.1.11/32"


def documents(path: Path) -> list[dict]:
    return [item for item in yaml.safe_load_all(path.read_text(encoding="utf-8")) if item]


def all_documents() -> list[tuple[str, dict]]:
    """Kubernetes objects only; the .example.yaml schema files are skipped."""
    return [
        (path.name, item)
        for path in sorted(NOTEBOOK_ROOT.glob("*.yaml"))
        for item in documents(path)
        if "kind" in item
    ]


def by_kind(kind: str) -> list[dict]:
    return [item for _, item in all_documents() if item["kind"] == kind]


def policy(name: str) -> dict:
    return next(item for item in by_kind("NetworkPolicy") if item["metadata"]["name"] == name)


class ResearchNotebookManifestTests(unittest.TestCase):
    def test_namespace_enforces_restricted_pod_security(self) -> None:
        (namespace,) = by_kind("Namespace")
        self.assertEqual(namespace["metadata"]["name"], NAMESPACE)
        self.assertEqual(
            namespace["metadata"]["labels"]["pod-security.kubernetes.io/enforce"],
            "restricted",
        )

    def test_every_namespaced_object_stays_in_the_notebooks_namespace(self) -> None:
        for name, item in all_documents():
            if item["kind"] in {"Namespace", "PersistentVolume"}:
                continue
            with self.subTest(file=name, kind=item["kind"]):
                self.assertEqual(item["metadata"]["namespace"], NAMESPACE)

    def test_no_secret_is_tracked(self) -> None:
        self.assertEqual(by_kind("Secret"), [])
        example = documents(NOTEBOOK_ROOT / "30-secret.example.yaml")[0]
        contract = example["research_notebook_secret"]
        self.assertEqual(contract["required_suffix"], ".local.yaml")
        for value in contract["resource"]["string_data"].values():
            self.assertRegex(value, r"^<required-.*>$")

    def test_notebook_pods_have_no_cluster_credentials_and_run_unprivileged(self) -> None:
        for deployment in by_kind("Deployment"):
            spec = deployment["spec"]["template"]["spec"]
            with self.subTest(deployment=deployment["metadata"]["name"]):
                self.assertIs(spec["automountServiceAccountToken"], False)
                self.assertNotIn("hostNetwork", spec)
                self.assertNotIn("hostPID", spec)
                self.assertIs(spec["securityContext"]["runAsNonRoot"], True)
                self.assertNotEqual(spec["securityContext"]["runAsUser"], 0)
                for container in spec["containers"]:
                    context = container["securityContext"]
                    self.assertIs(context["allowPrivilegeEscalation"], False)
                    self.assertEqual(context["capabilities"]["drop"], ["ALL"])
                    self.assertNotIn("privileged", context)
        (service_account,) = by_kind("ServiceAccount")
        self.assertIs(service_account["automountServiceAccountToken"], False)

    def test_notebooks_use_one_capped_gpu_on_node02_below_glasslab_priorities(self) -> None:
        for deployment in by_kind("Deployment"):
            spec = deployment["spec"]["template"]["spec"]
            with self.subTest(deployment=deployment["metadata"]["name"]):
                self.assertEqual(spec["nodeSelector"]["kubernetes.io/hostname"], "node02")
                self.assertNotIn("priorityClassName", spec)
                self.assertEqual(deployment["spec"]["replicas"], 1)
                (container,) = spec["containers"]
                self.assertEqual(container["resources"]["limits"]["nvidia.com/gpu"], "1")
                self.assertIn("memory", container["resources"]["limits"])
                self.assertIn("cpu", container["resources"]["limits"])

    def test_login_token_comes_from_a_secret(self) -> None:
        for deployment in by_kind("Deployment"):
            (container,) = deployment["spec"]["template"]["spec"]["containers"]
            env = {item["name"]: item for item in container["env"]}
            with self.subTest(deployment=deployment["metadata"]["name"]):
                self.assertIn("secretKeyRef", env["JUPYTER_TOKEN"]["valueFrom"])
                self.assertNotIn("value", env["JUPYTER_TOKEN"])
                for arg in container.get("args", []):
                    self.assertNotIn("token", arg.lower())

    def test_namespace_denies_all_traffic_by_default(self) -> None:
        deny = policy("default-deny-all")
        self.assertEqual(deny["spec"]["podSelector"], {})
        self.assertEqual(sorted(deny["spec"]["policyTypes"]), ["Egress", "Ingress"])
        self.assertNotIn("ingress", deny["spec"])
        self.assertNotIn("egress", deny["spec"])

    def test_only_the_gateway_and_node02_reach_the_jupyter_port(self) -> None:
        ingress = policy("research-notebook-ingress")["spec"]["ingress"]
        self.assertEqual(len(ingress), 1)
        self.assertEqual(ingress[0]["ports"], [{"protocol": "TCP", "port": 8888}])
        self.assertEqual(
            ingress[0]["from"],
            [{"ipBlock": {"cidr": GATEWAY_LAN}}, {"ipBlock": {"cidr": NODE02}}],
        )

    def test_services_preserve_the_gateway_source_address(self) -> None:
        for service in by_kind("Service"):
            with self.subTest(service=service["metadata"]["name"]):
                self.assertEqual(service["spec"]["type"], "NodePort")
                self.assertEqual(service["spec"]["externalTrafficPolicy"], "Local")

    def test_egress_is_dns_and_internet_https_only(self) -> None:
        egress = policy("research-notebook-egress")["spec"]["egress"]
        self.assertEqual(len(egress), 2)
        dns, https = egress
        self.assertEqual(
            dns["to"][0]["namespaceSelector"]["matchLabels"],
            {"kubernetes.io/metadata.name": "kube-system"},
        )
        self.assertEqual({port["port"] for port in dns["ports"]}, {53})
        self.assertEqual(https["ports"], [{"protocol": "TCP", "port": 443}])
        (peer,) = https["to"]
        excluded = [ipaddress.ip_network(cidr) for cidr in peer["ipBlock"]["except"]]
        for internal in ("192.168.1.0/24", "10.244.0.0/16", "10.96.0.0/12", "134.74.146.0/24"):
            with self.subTest(internal=internal):
                network = ipaddress.ip_network(internal)
                self.assertTrue(any(network.subnet_of(block) for block in excluded))


if __name__ == "__main__":
    unittest.main()
