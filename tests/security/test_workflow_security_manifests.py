"""Security invariants for workflow-api Kubernetes manifests."""

from __future__ import annotations

import unittest
from pathlib import Path

import yaml


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
KUBE_ROOT = REPOSITORY_ROOT / "kubeadm" / "glasslab-v2"

CALLERS = {
    "schedule-worker": {
        "deployment": KUBE_ROOT / "schedule-worker" / "10-deployment.yaml",
        "container": "schedule-worker",
        "secret": "glasslab-workflow-api-schedule-worker",
    },
    "research-orchestrator": {
        "deployment": KUBE_ROOT / "research-orchestrator" / "20-deployment.yaml",
        "container": "orchestrator",
        "secret": "glasslab-workflow-api-research-orchestrator",
    },
}


def documents(path: Path) -> list[dict]:
    return [item for item in yaml.safe_load_all(path.read_text(encoding="utf-8")) if item]


def container_for(path: Path, name: str) -> dict:
    deployment = next(item for item in documents(path) if item["kind"] == "Deployment")
    return next(item for item in deployment["spec"]["template"]["spec"]["containers"] if item["name"] == name)


def env_by_name(container: dict) -> dict[str, dict]:
    return {item["name"]: item for item in container.get("env", [])}


class WorkflowSecurityManifestTests(unittest.TestCase):
    def test_workflow_api_role_cannot_read_secrets(self):
        roles = [
            item for item in documents(KUBE_ROOT / "workflow-api" / "10-rbac.yaml")
            if item["kind"] in {"Role", "ClusterRole"}
        ]
        resources = {
            resource
            for role in roles
            for rule in role.get("rules", [])
            for resource in rule.get("resources", [])
        }
        self.assertNotIn("secrets", resources)

        job_rule = next(
            rule
            for role in roles
            for rule in role.get("rules", [])
            if rule.get("apiGroups") == ["batch"] and "jobs" in rule.get("resources", [])
        )
        self.assertIn("delete", job_rule["verbs"])

    def test_each_caller_has_fixed_name_and_dedicated_secret_token(self):
        for caller_name, expected in CALLERS.items():
            with self.subTest(caller=caller_name):
                container = container_for(expected["deployment"], expected["container"])
                environment = env_by_name(container)
                self.assertEqual(environment["GLASSLAB_WORKFLOW_API_CALLER_NAME"]["value"], caller_name)
                token_ref = environment["GLASSLAB_WORKFLOW_API_TOKEN"]["valueFrom"]["secretKeyRef"]
                self.assertEqual(token_ref, {"name": expected["secret"], "key": "token"})

    def test_workflow_api_reads_all_dedicated_token_secrets_without_interpolation(self):
        container = container_for(
            KUBE_ROOT / "workflow-api" / "20-deployment.yaml",
            "workflow-api",
        )
        environment = env_by_name(container)
        token_env_names = {
            "schedule-worker": "GLASSLAB_WORKFLOW_API_SCHEDULE_WORKER_TOKEN",
            "research-orchestrator": "GLASSLAB_WORKFLOW_API_RESEARCH_ORCHESTRATOR_TOKEN",
        }
        for caller_name, token_env_name in token_env_names.items():
            with self.subTest(caller=caller_name):
                self.assertEqual(
                    environment[token_env_name]["valueFrom"]["secretKeyRef"],
                    {"name": CALLERS[caller_name]["secret"], "key": "token"},
                )
        self.assertNotIn("GLASSLAB_WORKFLOW_API_CALLER_POLICIES", environment)

    def test_ingress_policy_only_allows_named_caller_labels_on_http_port(self):
        policy = documents(KUBE_ROOT / "workflow-api" / "50-ingress-network-policy.yaml")[0]
        self.assertEqual(policy["kind"], "NetworkPolicy")
        self.assertEqual(
            policy["spec"]["podSelector"]["matchLabels"],
            {"app.kubernetes.io/name": "glasslab-workflow-api"},
        )
        self.assertEqual(policy["spec"]["policyTypes"], ["Ingress"])
        ingress = policy["spec"]["ingress"]
        self.assertEqual(len(ingress), 1)
        self.assertEqual(ingress[0]["ports"], [{"protocol": "TCP", "port": 8080}])
        allowed = {
            peer["podSelector"]["matchLabels"]["app.kubernetes.io/name"]
            for peer in ingress[0]["from"]
        }
        self.assertEqual(allowed, {f"glasslab-{caller}" for caller in CALLERS})
        for peer in ingress[0]["from"]:
            self.assertEqual(set(peer), {"namespaceSelector", "podSelector"})
            self.assertEqual(
                peer["namespaceSelector"]["matchLabels"],
                {"kubernetes.io/metadata.name": "glasslab-v2"},
            )

    def test_rollout_applies_ingress_policy(self):
        rollout = (REPOSITORY_ROOT / "scripts" / "rollout-research-services.sh").read_text(encoding="utf-8")
        self.assertIn("workflow-api/50-ingress-network-policy.yaml", rollout)

    def test_rollout_preflights_secrets_and_stages_all_callers_before_server(self):
        rollout = (REPOSITORY_ROOT / "scripts" / "rollout-research-services.sh").read_text(encoding="utf-8")
        bundle = rollout[rollout.index("rollout_authenticated_workflow_bundle()") :]
        for caller in CALLERS:
            self.assertIn(f"require_object secret {CALLERS[caller]['secret']}", rollout)
        # The retired command-router is not part of the authenticated bundle.
        self.assertNotIn("rollout_command_router", bundle)
        schedule_position = bundle.index("rollout_schedule_worker")
        orchestrator_position = bundle.index("rollout_research_orchestrator")
        server_position = bundle.index("rollout_workflow_api")
        self.assertLess(schedule_position, server_position)
        self.assertLess(orchestrator_position, server_position)

    def test_public_smoke_does_not_call_protected_workflow_routes(self):
        smoke = (REPOSITORY_ROOT / "scripts" / "smoke-test-v2.sh").read_text(encoding="utf-8")
        self.assertIn("/healthz", smoke)
        self.assertNotIn("/workflow-families", smoke)


if __name__ == "__main__":
    unittest.main()
