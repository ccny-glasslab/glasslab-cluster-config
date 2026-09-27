"""Security invariants for workflow-api Kubernetes manifests."""

from __future__ import annotations

import importlib.util
import json
import re
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
    def test_workflow_api_secret_access_is_resource_name_scoped_and_read_only(self):
        """workflow-api may read only the named runner pull secret.

        Kubernetes RBAC ignores ``resourceNames`` for list/watch, so any rule
        that names ``secrets`` grants namespace-wide secret visibility unless it
        is a ``get`` constrained by an explicit ``resourceNames`` list. The
        service only needs to confirm the runner pull secret exists.
        """
        rules = [
            rule
            for role in documents(KUBE_ROOT / "workflow-api" / "10-rbac.yaml")
            if role["kind"] in {"Role", "ClusterRole"}
            for rule in role.get("rules", [])
        ]
        secret_rules = [rule for rule in rules if "secrets" in rule.get("resources", [])]
        self.assertTrue(secret_rules, "the scoped runner pull-secret read is required")
        for rule in secret_rules:
            with self.subTest(rule=rule):
                self.assertEqual(rule["verbs"], ["get"])
                self.assertEqual(rule["resourceNames"], ["glasslab-ghcr-pull"])

        job_rule = next(
            rule
            for rule in rules
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
        # Retired (command-router) services are not part of the authenticated
        # bundle. The schedule-worker IS an authenticated caller and must roll
        # out with the bundle so its CronJob can reach a running pod (#602).
        self.assertNotIn("rollout_command_router", bundle)
        self.assertIn("rollout_schedule_worker", bundle)
        secret_preflight_position = bundle.index("require_workflow_caller_secrets")
        orchestrator_position = bundle.index("rollout_research_orchestrator")
        schedule_worker_position = bundle.index("rollout_schedule_worker")
        server_position = bundle.index("rollout_workflow_api")
        # Callers (orchestrator and schedule-worker) roll before the server so
        # workflow-api is never switched to fail-closed auth ahead of clients.
        self.assertLess(secret_preflight_position, server_position)
        self.assertLess(orchestrator_position, server_position)
        self.assertLess(schedule_worker_position, server_position)

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


SERVICE_IMAGE_WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "service-images.yml"
CANONICAL_FULL_SHA_IMAGE_RE = re.compile(r"^ghcr\.io/ccny-glasslab/glasslab-([a-z0-9-]+):[0-9a-f]{40}$")
# Canonical images CI has never published. service-images.yml only builds on
# push to main, so before the first merge no real SHA exists; the manifest must
# say so explicitly instead of guessing a SHA. scripts/validate-configs.py
# tolerates this marker only for exactly these image names.
PENDING_BUILD_TAG = "pending-ci-build"
CANONICAL_PENDING_BUILD_IMAGE_RE = re.compile(
    r"^ghcr\.io/ccny-glasslab/glasslab-([a-z0-9-]+):" + re.escape(PENDING_BUILD_TAG) + r"$"
)
ZERO_SHA = "0" * 40
ZERO_DIGEST = "sha256:" + "0" * 64
BOUNDED_SERVICE_DEPLOYMENTS = {
    "assessment-agent": KUBE_ROOT / "assessment-agent" / "10-deployment.yaml",
    "design-agent": KUBE_ROOT / "design-agent" / "10-deployment.yaml",
    "intake-agent": KUBE_ROOT / "intake-agent" / "10-deployment.yaml",
    "interpretation-agent": KUBE_ROOT / "interpretation-agent" / "10-deployment.yaml",
    "schedule-worker": KUBE_ROOT / "schedule-worker" / "10-deployment.yaml",
}
PUBLISHED_SERVICES = ("workflow-api", "research-orchestrator", *BOUNDED_SERVICE_DEPLOYMENTS)


class ServiceImageSupplyChainTests(unittest.TestCase):
    """Issue #600: deployed images must come from the canonical org, pinned."""

    def test_bounded_service_deployments_pin_canonical_org_full_sha_tags(self):
        for service, path in BOUNDED_SERVICE_DEPLOYMENTS.items():
            with self.subTest(service=service):
                container = container_for(path, service)
                image = container["image"]
                sha_match = CANONICAL_FULL_SHA_IMAGE_RE.match(image)
                pending_match = CANONICAL_PENDING_BUILD_IMAGE_RE.match(image)
                self.assertTrue(
                    sha_match or pending_match,
                    f"{service} is neither pinned to a canonical full SHA nor the documented pending marker: {image}",
                )
                match = sha_match or pending_match
                self.assertEqual(match.group(1), service)
                if sha_match:
                    self.assertNotEqual(image.rsplit(":", 1)[1], ZERO_SHA)

    def test_schedule_worker_runs_a_pod_so_the_cronjob_can_reach_it(self):
        deployment = next(
            item
            for item in documents(KUBE_ROOT / "schedule-worker" / "10-deployment.yaml")
            if item["kind"] == "Deployment"
        )
        self.assertGreaterEqual(
            deployment["spec"]["replicas"],
            1,
            "the schedule-worker CronJob POSTs to the Service and needs a running pod",
        )

    def test_service_image_pipeline_publishes_every_service_at_the_git_sha(self):
        jobs = yaml.safe_load(SERVICE_IMAGE_WORKFLOW.read_text(encoding="utf-8"))["jobs"]
        for service in PUBLISHED_SERVICES:
            with self.subTest(service=service):
                build = next(
                    step
                    for step in jobs[service]["steps"]
                    if step.get("uses", "").startswith("docker/build-push-action")
                )
                self.assertEqual(
                    build["with"]["tags"],
                    f"ghcr.io/ccny-glasslab/glasslab-{service}:${{{{ github.sha }}}}",
                )
                self.assertEqual(build["with"]["file"], f"services/{service}/Dockerfile")

    def test_no_manifest_references_the_personal_registry_org(self):
        for path in sorted(KUBE_ROOT.rglob("*.yaml")):
            with self.subTest(path=path.relative_to(KUBE_ROOT).as_posix()):
                self.assertNotIn("ghcr.io/offensivegeneric/", path.read_text(encoding="utf-8"))

    def test_minio_and_nats_images_stay_digest_pinned(self):
        for relative_path, container_name in (
            ("minio/20-deployment.yaml", "minio"),
            ("nats/10-deployment.yaml", "nats"),
        ):
            with self.subTest(manifest=relative_path):
                container = container_for(KUBE_ROOT / relative_path, container_name)
                self.assertRegex(container["image"], r"@sha256:[0-9a-f]{64}$")


class ServiceImagePinSentinelTests(unittest.TestCase):
    """The pinning gate must reject fake zero-SHA pins (#602 review).

    An all-zero tag or digest satisfies a naive 40-hex/64-hex shape while
    naming no real commit or image, so it could smuggle an unpinned service
    past the gate. The pending marker is the only tolerated non-SHA, and only
    for the exact images CI has not published yet.
    """

    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location(
            "validate_configs", REPOSITORY_ROOT / "scripts" / "validate-configs.py"
        )
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        cls.validate_configs = module

    def test_all_zero_sha_tag_is_rejected(self):
        self.assertFalse(
            self.validate_configs.is_pinned_image_ref(
                f"ghcr.io/ccny-glasslab/glasslab-schedule-worker:{ZERO_SHA}"
            )
        )

    def test_all_zero_sha256_digest_is_rejected(self):
        self.assertFalse(
            self.validate_configs.is_pinned_image_ref(
                f"ghcr.io/ccny-glasslab/glasslab-schedule-worker@{ZERO_DIGEST}"
            )
        )

    def test_all_zero_sha_component_is_rejected(self):
        self.assertFalse(
            self.validate_configs.is_pinned_image_ref(
                f"ghcr.io/ccny-glasslab/glasslab-metric-search:smoke-test-{ZERO_SHA}"
            )
        )

    def test_real_sha_and_digest_still_pass(self):
        self.assertTrue(
            self.validate_configs.is_pinned_image_ref(
                f"ghcr.io/ccny-glasslab/glasslab-workflow-api:{'a' * 40}"
            )
        )
        self.assertTrue(
            self.validate_configs.is_pinned_image_ref(
                f"ghcr.io/ccny-glasslab/glasslab-minio@sha256:{'b' * 64}"
            )
        )

    def test_pending_marker_is_tolerated_only_for_the_unpublished_images(self):
        for service in BOUNDED_SERVICE_DEPLOYMENTS:
            with self.subTest(service=service):
                self.assertTrue(
                    self.validate_configs.is_pinned_image_ref(
                        f"ghcr.io/ccny-glasslab/glasslab-{service}:{PENDING_BUILD_TAG}"
                    )
                )
        rejected = (
            f"ghcr.io/ccny-glasslab/glasslab-workflow-api:{PENDING_BUILD_TAG}",
            f"ghcr.io/ccny-glasslab/glasslab-research-orchestrator:{PENDING_BUILD_TAG}",
            f"ghcr.io/offensivegeneric/glasslab-intake-agent:{PENDING_BUILD_TAG}",
            "ghcr.io/ccny-glasslab/glasslab-intake-agent:latest",
        )
        for ref in rejected:
            with self.subTest(ref=ref):
                self.assertFalse(self.validate_configs.is_pinned_image_ref(ref))

    def test_marker_manifests_are_exactly_the_bounded_services(self):
        marker_services: set[str] = set()
        for path in sorted(KUBE_ROOT.rglob("*.yaml")):
            marker_services.update(
                re.findall(
                    r"ghcr\.io/ccny-glasslab/glasslab-([a-z0-9-]+):" + re.escape(PENDING_BUILD_TAG),
                    path.read_text(encoding="utf-8"),
                )
            )
        self.assertEqual(marker_services, set(BOUNDED_SERVICE_DEPLOYMENTS))


if __name__ == "__main__":
    unittest.main()
