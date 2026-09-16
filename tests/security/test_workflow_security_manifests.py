"""Security invariants for workflow-api Kubernetes manifests."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

import yaml


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
KUBE_ROOT = REPOSITORY_ROOT / "kubeadm" / "glasslab-v2"

MINIO_ROOT_SECRET = "glasslab-v2-minio"
MINIO_WORKFLOW_API_SECRET = "glasslab-v2-workflow-api-minio"
MINIO_SOURCE_DOCUMENT_BUCKET = "glasslab-source-documents"

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


def all_manifest_documents() -> list[tuple[str, dict]]:
    documents_out: list[tuple[str, dict]] = []
    for path in sorted(KUBE_ROOT.rglob("*.yaml")):
        for item in yaml.safe_load_all(path.read_text(encoding="utf-8")):
            if isinstance(item, dict):
                documents_out.append((path.relative_to(KUBE_ROOT).as_posix(), item))
    return documents_out


def pod_containers(document: dict) -> list[dict]:
    if document.get("kind") not in {"Deployment", "StatefulSet", "Job", "DaemonSet"}:
        return []
    spec = document.get("spec", {}).get("template", {}).get("spec")
    if not isinstance(spec, dict):
        return []
    return [*spec.get("containers", []), *spec.get("initContainers", [])]


def referenced_secret_names(document: dict) -> set[str]:
    names: set[str] = set()
    for container in pod_containers(document):
        for env in container.get("env", []):
            ref = env.get("valueFrom", {}).get("secretKeyRef")
            if isinstance(ref, dict) and "name" in ref:
                names.add(ref["name"])
    return names


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

    def test_caller_secret_contract_lists_exactly_the_deployed_callers(self):
        contract = next(
            item["caller_secret_contract"]
            for item in documents(KUBE_ROOT / "workflow-api" / "10-secret.example")
            if "caller_secret_contract" in item
        )
        required = {entry["name"]: entry["required_keys"] for entry in contract["secrets"]}
        self.assertEqual(
            required,
            {expected["secret"]: ["token"] for expected in CALLERS.values()},
        )

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
            self.assertIn(f"'{CALLERS[caller]['secret']}:token'", rollout)
        # Retired (command-router) and legacy (schedule-worker) services are
        # not part of the authenticated bundle; their images are not published
        # by the ccny service-image pipeline.
        self.assertNotIn("rollout_command_router", bundle)
        self.assertNotIn("rollout_schedule_worker", bundle)
        secret_preflight_position = bundle.index("require_workflow_caller_secrets")
        orchestrator_position = bundle.index("rollout_research_orchestrator")
        server_position = bundle.index("rollout_workflow_api")
        self.assertLess(secret_preflight_position, server_position)
        self.assertLess(orchestrator_position, server_position)

    def test_public_smoke_does_not_call_protected_workflow_routes(self):
        smoke = (REPOSITORY_ROOT / "scripts" / "smoke-test-v2.sh").read_text(encoding="utf-8")
        self.assertIn("/healthz", smoke)
        self.assertNotIn("/workflow-families", smoke)


class MinioCredentialScopingTests(unittest.TestCase):
    """Only the MinIO admin surface may consume the root object-store credential."""

    def test_only_minio_admin_workloads_reference_the_root_secret(self):
        """Any other pod reading MINIO_ROOT_* would regain whole-store authority."""
        consumers = {
            path
            for path, document in all_manifest_documents()
            if MINIO_ROOT_SECRET in referenced_secret_names(document)
        }
        self.assertEqual(
            consumers,
            {
                "minio/20-deployment.yaml",
                "minio/45-provision-scoped-users-job.yaml",
            },
        )

    def test_gpu_runner_no_longer_mounts_minio_credentials(self):
        """The GPU runner needs no object-store identity; root must not return."""
        for name in ("00-all.yaml", "10-deployment.yaml"):
            path = KUBE_ROOT / "gpu-runner" / name
            for document in documents(path):
                for container in pod_containers(document):
                    environment = env_by_name(container)
                    self.assertFalse(
                        any(key.startswith("GLASSLAB_RUNNER_MINIO") for key in environment),
                        f"{name} still injects MinIO credentials",
                    )
                self.assertNotIn(MINIO_ROOT_SECRET, referenced_secret_names(document))

    def test_workflow_api_uses_its_own_scoped_minio_secret(self):
        """workflow-api must read a bucket-scoped user, never the root identity."""
        deployment_path = KUBE_ROOT / "workflow-api" / "20-deployment.yaml"
        container = container_for(deployment_path, "workflow-api")
        environment = env_by_name(container)
        for env_name, key_name in (
            ("GLASSLAB_WORKFLOW_API_MINIO_ACCESS_KEY", "MINIO_ACCESS_KEY"),
            ("GLASSLAB_WORKFLOW_API_MINIO_SECRET_KEY", "MINIO_SECRET_KEY"),
        ):
            with self.subTest(env=env_name):
                ref = environment[env_name]["valueFrom"]["secretKeyRef"]
                self.assertEqual(ref["name"], MINIO_WORKFLOW_API_SECRET)
                self.assertEqual(ref["key"], key_name)
                self.assertNotEqual(ref["name"], MINIO_ROOT_SECRET)
        deployment = next(item for item in documents(deployment_path) if item["kind"] == "Deployment")
        self.assertNotIn(MINIO_ROOT_SECRET, referenced_secret_names(deployment))

    def test_root_secret_contract_stays_root_only(self):
        """The root Secret contract must not absorb scoped service-user keys."""
        contract = next(
            item["secret_contract"]
            for item in documents(KUBE_ROOT / "minio" / "10-secret.example.yaml")
            if "secret_contract" in item
        )
        self.assertEqual(
            contract["required_keys"],
            ["MINIO_ROOT_USER", "MINIO_ROOT_PASSWORD"],
        )

    def test_scoped_minio_secret_contract_lists_only_scoped_keys(self):
        """The workflow-api object-store identity lives in its own Secret."""
        contract = next(
            item["minio_secret_contract"]
            for item in documents(KUBE_ROOT / "workflow-api" / "10-secret.example")
            if "minio_secret_contract" in item
        )
        self.assertEqual(contract["metadata"]["name"], MINIO_WORKFLOW_API_SECRET)
        self.assertEqual(
            contract["required_keys"],
            ["MINIO_ACCESS_KEY", "MINIO_SECRET_KEY"],
        )
        self.assertEqual(contract["example_values_are_not_deployable"], True)

    def test_provisioning_job_is_admin_only_and_policy_is_bucket_scoped(self):
        """The one-shot provisioner creates the scoped user; its policy is bucket-limited."""
        job = next(
            item
            for item in documents(KUBE_ROOT / "minio" / "45-provision-scoped-users-job.yaml")
            if item["kind"] == "Job"
        )
        pod_spec = job["spec"]["template"]["spec"]
        self.assertFalse(pod_spec.get("automountServiceAccountToken", False))

        container = next(item for item in pod_spec["containers"] if item["name"] == "mc")
        self.assertRegex(container["image"], r"@sha256:[0-9a-f]{64}$")
        secret_refs = {
            env["name"]: env["valueFrom"]["secretKeyRef"]
            for env in container["env"]
            if "valueFrom" in env
        }
        self.assertEqual(
            secret_refs["MINIO_ROOT_USER"],
            {"name": MINIO_ROOT_SECRET, "key": "MINIO_ROOT_USER"},
        )
        self.assertEqual(
            secret_refs["MINIO_ROOT_PASSWORD"],
            {"name": MINIO_ROOT_SECRET, "key": "MINIO_ROOT_PASSWORD"},
        )
        self.assertEqual(
            secret_refs["MINIO_ACCESS_KEY"],
            {"name": MINIO_WORKFLOW_API_SECRET, "key": "MINIO_ACCESS_KEY"},
        )
        self.assertEqual(
            secret_refs["MINIO_SECRET_KEY"],
            {"name": MINIO_WORKFLOW_API_SECRET, "key": "MINIO_SECRET_KEY"},
        )

        mount = next(item for item in container["volumeMounts"] if item["mountPath"] == "/policies")
        self.assertTrue(mount["readOnly"])
        volume = next(item for item in pod_spec["volumes"] if item["name"] == mount["name"])
        self.assertEqual(volume["configMap"]["name"], "glasslab-minio-scoped-policies")

        policy_config = next(
            item
            for item in documents(KUBE_ROOT / "minio" / "40-scoped-user-policies.yaml")
            if item["kind"] == "ConfigMap"
        )
        self.assertEqual(policy_config["metadata"]["name"], "glasslab-minio-scoped-policies")
        policy = json.loads(policy_config["data"]["glasslab-workflow-api-sources.json"])
        resources = {
            resource
            for statement in policy["Statement"]
            for resource in statement["Resource"]
        }
        self.assertTrue(resources)
        self.assertNotIn("*", resources)
        for resource in resources:
            self.assertTrue(
                resource.startswith(f"arn:aws:s3:::{MINIO_SOURCE_DOCUMENT_BUCKET}"),
                f"policy grants access outside the scoped bucket: {resource}",
            )


if __name__ == "__main__":
    unittest.main()
