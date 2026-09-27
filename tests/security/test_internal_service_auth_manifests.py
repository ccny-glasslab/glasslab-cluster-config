"""Security invariants for internal service authentication (issue #602).

Covers the shared-token mounts, the default-deny agent/schedule-worker ingress
policies, the schedule-worker CronJob trigger, the workflow-api ServiceAccount
token pin, and the least-privilege job RBAC.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
KUBE_ROOT = REPOSITORY_ROOT / "kubeadm" / "glasslab-v2"
WORKFLOW_API_APP_ROOT = REPOSITORY_ROOT / "services" / "workflow-api" / "app"

INTERNAL_TOKEN_HEADER = "X-Glasslab-Internal-Token"
INTERNAL_TOKEN_SECRET = "glasslab-agent-internal-token"
INTERNAL_TOKEN_ENV = "GLASSLAB_AGENT_INTERNAL_TOKEN"
WORKFLOW_API_TOKEN_ENV = "GLASSLAB_WORKFLOW_API_AGENT_INTERNAL_TOKEN"

AGENT_SERVICES = {
    "intake-agent": ("glasslab-intake-agent", 8090),
    "interpretation-agent": ("glasslab-interpretation-agent", 8091),
    "assessment-agent": ("glasslab-assessment-agent", 8092),
    "design-agent": ("glasslab-design-agent", 8093),
}


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


def token_env_ref(environment: dict[str, dict], env_name: str) -> dict:
    ref = environment[env_name]["valueFrom"]["secretKeyRef"]
    assert ref == {"name": INTERNAL_TOKEN_SECRET, "key": "token"}, (
        f"{env_name} must come from the shared internal-token Secret"
    )
    return ref


class AgentIngressPolicyTests(unittest.TestCase):
    def test_each_agent_is_default_deny_to_workflow_api_only(self) -> None:
        for directory, (service, port) in AGENT_SERVICES.items():
            with self.subTest(service=service):
                policy = documents(KUBE_ROOT / directory / "50-ingress-network-policy.yaml")[0]
                self.assertEqual(policy["kind"], "NetworkPolicy")
                self.assertEqual(
                    policy["spec"]["podSelector"]["matchLabels"],
                    {"app.kubernetes.io/name": service},
                )
                self.assertEqual(policy["spec"]["policyTypes"], ["Ingress"])
                ingress = policy["spec"]["ingress"]
                self.assertEqual(len(ingress), 1)
                self.assertEqual(ingress[0]["ports"], [{"protocol": "TCP", "port": port}])
                self.assertEqual(
                    ingress[0]["from"],
                    [
                        {
                            "namespaceSelector": {
                                "matchLabels": {"kubernetes.io/metadata.name": "glasslab-v2"}
                            },
                            "podSelector": {
                                "matchLabels": {"app.kubernetes.io/name": "glasslab-workflow-api"}
                            },
                        }
                    ],
                )


class ScheduleWorkerIngressPolicyTests(unittest.TestCase):
    def test_only_the_cronjob_label_may_reach_run_once(self) -> None:
        policy = documents(KUBE_ROOT / "schedule-worker" / "50-ingress-network-policy.yaml")[0]
        self.assertEqual(policy["kind"], "NetworkPolicy")
        self.assertEqual(
            policy["spec"]["podSelector"]["matchLabels"],
            {"app.kubernetes.io/name": "glasslab-schedule-worker"},
        )
        self.assertEqual(policy["spec"]["policyTypes"], ["Ingress"])
        ingress = policy["spec"]["ingress"]
        self.assertEqual(len(ingress), 1)
        self.assertEqual(ingress[0]["ports"], [{"protocol": "TCP", "port": 8094}])
        allowed = {
            peer["podSelector"]["matchLabels"]["app.kubernetes.io/name"]
            for peer in ingress[0]["from"]
        }
        self.assertEqual(allowed, {"glasslab-schedule-worker-cron"})
        for peer in ingress[0]["from"]:
            self.assertEqual(
                peer["namespaceSelector"]["matchLabels"],
                {"kubernetes.io/metadata.name": "glasslab-v2"},
            )


class InternalTokenMountTests(unittest.TestCase):
    def test_agents_receive_the_shared_internal_token(self) -> None:
        for directory, (service, _port) in AGENT_SERVICES.items():
            with self.subTest(service=service):
                container = container_for(
                    KUBE_ROOT / directory / "10-deployment.yaml",
                    directory,
                )
                token_env_ref(env_by_name(container), INTERNAL_TOKEN_ENV)

    def test_schedule_worker_server_receives_the_shared_internal_token(self) -> None:
        container = container_for(
            KUBE_ROOT / "schedule-worker" / "10-deployment.yaml",
            "schedule-worker",
        )
        token_env_ref(env_by_name(container), INTERNAL_TOKEN_ENV)

    def test_workflow_api_receives_the_shared_internal_token(self) -> None:
        container = container_for(
            KUBE_ROOT / "workflow-api" / "20-deployment.yaml",
            "workflow-api",
        )
        token_env_ref(env_by_name(container), WORKFLOW_API_TOKEN_ENV)

    def test_shared_secret_contract_lists_only_the_token_key(self) -> None:
        contract = next(
            item["agent_internal_token_secret_contract"]
            for item in documents(KUBE_ROOT / "secrets" / "10-agent-internal-token.example.yaml")
            if "agent_internal_token_secret_contract" in item
        )
        self.assertEqual(contract["metadata"]["name"], INTERNAL_TOKEN_SECRET)
        self.assertEqual(contract["required_keys"], ["token"])
        self.assertEqual(contract["example_values_are_not_deployable"], True)


class ScheduleWorkerCronJobTests(unittest.TestCase):
    def cronjob(self) -> dict:
        return next(
            item
            for item in documents(KUBE_ROOT / "schedule-worker" / "30-cronjob.yaml")
            if item["kind"] == "CronJob"
        )

    def test_cronjob_calls_run_once_with_the_internal_token_header(self) -> None:
        job = self.cronjob()
        pod_spec = job["spec"]["jobTemplate"]["spec"]["template"]["spec"]
        self.assertEqual(
            job["spec"]["concurrencyPolicy"],
            "Forbid",
        )
        self.assertFalse(pod_spec.get("automountServiceAccountToken", True))
        self.assertEqual(
            job["spec"]["jobTemplate"]["spec"]["template"]["metadata"]["labels"],
            {"app.kubernetes.io/name": "glasslab-schedule-worker-cron"},
        )
        container = next(item for item in pod_spec["containers"] if item["name"] == "run-once")
        command = " ".join(container["command"])
        self.assertIn("http://glasslab-schedule-worker.glasslab-v2.svc.cluster.local:8094/run-once", command)
        self.assertIn(f"--header=\"X-Glasslab-Internal-Token: ${{{INTERNAL_TOKEN_ENV}}}\"", command)
        token_env_ref(env_by_name(container), INTERNAL_TOKEN_ENV)

    def test_cronjob_image_is_pinned(self) -> None:
        container = next(
            item for item in self.cronjob()["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"]
        )
        self.assertRegex(container["image"], r"@sha256:[0-9a-f]{64}$")


class WorkflowApiPrivilegeTests(unittest.TestCase):
    def test_automount_service_account_token_is_pinned_true(self) -> None:
        """Job submission needs in-cluster config; false breaks every job."""
        deployment = next(
            item
            for item in documents(KUBE_ROOT / "workflow-api" / "20-deployment.yaml")
            if item["kind"] == "Deployment"
        )
        pod_spec = deployment["spec"]["template"]["spec"]
        self.assertIs(pod_spec.get("automountServiceAccountToken"), True)
        for path in sorted((KUBE_ROOT / "workflow-api").glob("*.yaml")):
            for document in documents(path):
                spec = document.get("spec", {}).get("template", {}).get("spec", {})
                self.assertIsNot(
                    spec.get("automountServiceAccountToken"),
                    False,
                    f"{path.name} would break workflow-api job submission",
                )

    def test_job_rbac_drops_unused_list_and_watch_verbs(self) -> None:
        rules = [
            rule
            for role in documents(KUBE_ROOT / "workflow-api" / "10-rbac.yaml")
            if role["kind"] in {"Role", "ClusterRole"}
            for rule in role.get("rules", [])
        ]
        job_rule = next(
            rule
            for rule in rules
            if rule.get("apiGroups") == ["batch"] and "jobs" in rule.get("resources", [])
        )
        self.assertEqual(set(job_rule["verbs"]), {"create", "delete", "get"})

    def test_no_workflow_api_code_path_lists_or_watches_jobs(self) -> None:
        """The RBAC verbs above are only safe while no code lists/watches jobs."""
        offenders = [
            path.name
            for path in sorted(WORKFLOW_API_APP_ROOT.glob("*.py"))
            if re.search(r"list_namespaced_job|watch_namespaced_job|list_job_for_all_namespaces", path.read_text(encoding="utf-8"))
        ]
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
