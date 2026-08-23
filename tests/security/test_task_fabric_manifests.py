"""Security and recovery invariants for the Glasslab task-fabric RabbitMQ manifests."""

from __future__ import annotations

import unittest
from pathlib import Path

import yaml


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
KUBE_ROOT = REPOSITORY_ROOT / "kubeadm" / "glasslab-v2"
RABBITMQ_DIR = KUBE_ROOT / "rabbitmq"

# Tested RabbitMQ 4.3.5 OCI identity (linux/amd64 manifest plus its multi-arch
# index). See kubeadm/glasslab-v2/rabbitmq/README.md for the record.
RABBITMQ_AMD64_DIGEST = "sha256:cb038b7a48d8b73507c83ff446245546a9459ac53e9ce79615217b4fbd917d50"
RABBITMQ_INDEX_DIGEST = "sha256:9d39258795e314bec0a204db15cc0b8770590ae983d88efacf159c766b1e539d"
RENDERER_IMAGE_DIGEST = "sha256:9c900dea9e8fb7e16277c179b555cc72d29a352dbc33cff48ad5a0412fd5bfc7"

SECRET_NAME = "glasslab-v2-rabbitmq"
IDENTITY_KEYS = [
    "erlang_cookie",
    "topology_admin_password",
    "publisher_password",
    "consumer_password",
    "monitoring_password",
]
CLIENT_LABELS = {
    "glasslab-research-orchestrator",
    "glasslab-workflow-api",
}


def documents(path: Path) -> list[dict]:
    return [item for item in yaml.safe_load_all(path.read_text(encoding="utf-8")) if item]


def by_kind(path: Path, kind: str) -> dict:
    return next(item for item in documents(path) if item.get("kind") == kind)


def statefulset() -> dict:
    return by_kind(RABBITMQ_DIR / "60-statefulset.yaml", "StatefulSet")


def containers() -> dict[str, dict]:
    spec = statefulset()["spec"]["template"]["spec"]
    return {item["name"]: item for item in spec["containers"]}


def init_containers() -> dict[str, dict]:
    spec = statefulset()["spec"]["template"]["spec"]
    return {item["name"]: item for item in spec.get("initContainers", [])}


def config_file(name: str) -> str:
    configmap = by_kind(RABBITMQ_DIR / "20-configmap.yaml", "ConfigMap")
    return configmap["data"][name]


def topology_template() -> dict:
    topology = by_kind(RABBITMQ_DIR / "30-topology.yaml", "ConfigMap")
    return yaml.safe_load(topology["data"]["definitions.template.json"])


class RabbitMQImageTests(unittest.TestCase):
    def test_broker_image_is_pinned_to_tested_digest(self):
        image = containers()["rabbitmq"]["image"]
        self.assertEqual(image, f"rabbitmq@{RABBITMQ_AMD64_DIGEST}")

    def test_renderer_image_is_digest_pinned(self):
        image = init_containers()["render-definitions"]["image"]
        self.assertTrue(
            image.endswith(f"@{RENDERER_IMAGE_DIGEST}"),
            f"renderer image must be digest-pinned: {image}",
        )

    def test_tested_digest_is_recorded_in_readme(self):
        readme = (RABBITMQ_DIR / "README.md").read_text(encoding="utf-8")
        self.assertIn(RABBITMQ_AMD64_DIGEST, readme)
        self.assertIn(RABBITMQ_INDEX_DIGEST, readme)
        self.assertIn("4.3.5", readme)


class RabbitMQStorageTests(unittest.TestCase):
    def test_broker_state_is_on_a_persistent_volume(self):
        spec = statefulset()["spec"]["template"]["spec"]
        claims = [
            volume["persistentVolumeClaim"]["claimName"]
            for volume in spec["volumes"]
            if "persistentVolumeClaim" in volume
        ]
        self.assertIn("glasslab-rabbitmq-data", claims)
        rabbitmq = containers()["rabbitmq"]
        mounts = {item["name"]: item["mountPath"] for item in rabbitmq["volumeMounts"]}
        self.assertEqual(mounts["rabbitmq-data"], "/var/lib/rabbitmq")

    def test_broker_is_single_replica(self):
        self.assertEqual(statefulset()["spec"]["replicas"], 1)


class RabbitMQHardeningTests(unittest.TestCase):
    def test_broker_runs_non_root_with_hardened_context(self):
        spec = statefulset()["spec"]["template"]["spec"]
        pod_security = spec["securityContext"]
        self.assertTrue(pod_security["runAsNonRoot"])
        self.assertEqual(pod_security["runAsUser"], 999)
        self.assertEqual(pod_security["fsGroup"], 999)
        for container in [*spec.get("initContainers", []), *spec["containers"]]:
            with self.subTest(container=container["name"]):
                security = container["securityContext"]
                self.assertFalse(security.get("allowPrivilegeEscalation", True))
                self.assertIn("ALL", security.get("capabilities", {}).get("drop", []))

    def test_resource_alarms_are_configured(self):
        conf = config_file("rabbitmq.conf")
        self.assertRegex(conf, r"(?m)^vm_memory_high_watermark\.relative\s*=\s*\S+")
        self.assertRegex(conf, r"(?m)^disk_free_limit\.absolute\s*=\s*\S+")


class RabbitMQExposureTests(unittest.TestCase):
    def test_amqp_service_is_internal_only(self):
        service = by_kind(RABBITMQ_DIR / "40-service.yaml", "Service")
        self.assertEqual(service["spec"]["type"], "ClusterIP")
        ports = [(item["port"], item.get("name")) for item in service["spec"]["ports"]]
        self.assertEqual(ports, [(5672, "amqp")])
        self.assertNotIn("ingress", service["metadata"].get("annotations", {}))

    def test_management_ui_is_not_exposed(self):
        # The management plugin is not enabled at all, so no UI/API listener
        # exists; the Service and NetworkPolicy must not open one either.
        conf = config_file("rabbitmq.conf")
        self.assertNotRegex(conf, r"(?m)^management\.tcp\.port\b")
        policy = documents(RABBITMQ_DIR / "50-network-policy.yaml")[0]
        policy_ports = [item["port"] for item in policy["spec"]["ingress"][0]["ports"]]
        self.assertNotIn(15672, policy_ports)

    def test_no_plugins_are_enabled(self):
        configmap = by_kind(RABBITMQ_DIR / "20-configmap.yaml", "ConfigMap")
        enabled = configmap["data"]["enabled_plugins"].strip()
        self.assertEqual(enabled, "[].")
        self.assertNotIn("rabbitmq_management", enabled)

    def test_management_surface_is_absent_from_configuration(self):
        conf = config_file("rabbitmq.conf")
        self.assertNotIn("management.", conf)
        service = by_kind(RABBITMQ_DIR / "40-service.yaml", "Service")
        service_ports = [item["port"] for item in service["spec"]["ports"]]
        self.assertNotIn(15672, service_ports)

    def test_post_start_verification_is_wired_into_the_broker_container(self):
        rabbitmq = containers()["rabbitmq"]
        hook = rabbitmq["lifecycle"]["postStart"]["exec"]["command"]
        self.assertIn("/etc/rabbitmq/glasslab-verify/verify-topology.sh", " ".join(hook))
        mounts = {item["mountPath"] for item in rabbitmq["volumeMounts"]}
        self.assertIn("/etc/rabbitmq/glasslab-verify", mounts)
        topology = by_kind(RABBITMQ_DIR / "30-topology.yaml", "ConfigMap")
        self.assertIn("verify-topology.sh", topology["data"])
        self.assertIn("verify.eval", topology["data"])
        self.assertIn("topology_drift", topology["data"]["verify.eval"])

    def test_network_policy_limits_ingress_to_named_app_clients_over_amqp(self):
        policy = documents(RABBITMQ_DIR / "50-network-policy.yaml")[0]
        self.assertEqual(policy["kind"], "NetworkPolicy")
        self.assertEqual(
            policy["spec"]["podSelector"]["matchLabels"],
            {"app.kubernetes.io/name": "glasslab-rabbitmq"},
        )
        self.assertEqual(policy["spec"]["policyTypes"], ["Ingress"])
        ingress = policy["spec"]["ingress"]
        self.assertEqual(len(ingress), 1)
        self.assertEqual(ingress[0]["ports"], [{"protocol": "TCP", "port": 5672}])
        allowed = {
            peer["podSelector"]["matchLabels"]["app.kubernetes.io/name"]
            for peer in ingress[0]["from"]
        }
        self.assertEqual(allowed, CLIENT_LABELS)


class RabbitMQIdentityTests(unittest.TestCase):
    def test_secret_contract_requires_split_identities(self):
        contract = documents(RABBITMQ_DIR / "10-secret.example.yaml")[0]["secret_contract"]
        required = set(contract["required_keys"])
        for key in IDENTITY_KEYS:
            self.assertIn(key, required)
        self.assertGreaterEqual(len(required), len(IDENTITY_KEYS))
        self.assertTrue(contract["example_values_are_not_deployable"])

    def test_secret_example_is_not_a_deployable_secret(self):
        items = documents(RABBITMQ_DIR / "10-secret.example.yaml")
        self.assertNotIn("Secret", {item.get("kind") for item in items})
    def test_definitions_provision_split_broker_users(self):
        template = topology_template()
        users = {user["name"]: user for user in template["users"]}
        self.assertEqual(
            set(users),
            {
                "glasslab-topology-admin",
                "glasslab-publisher",
                "glasslab-consumer",
                "glasslab-monitor",
            },
        )
        self.assertIn("administrator", users["glasslab-topology-admin"]["tags"])
        self.assertNotIn("administrator", users["glasslab-publisher"]["tags"])
        self.assertNotIn("administrator", users["glasslab-consumer"]["tags"])
        self.assertEqual(users["glasslab-monitor"]["tags"], ["monitoring"])
        for user in users.values():
            self.assertEqual(user["password"], f"<{user['name']}-password>")
        self.assertNotIn("guest", users)

    def test_renderer_sources_passwords_from_the_sops_managed_secret(self):
        renderer = init_containers()["render-definitions"]
        env = {item["name"]: item for item in renderer["env"]}
        for key in IDENTITY_KEYS:
            self.assertEqual(env[key]["valueFrom"]["secretKeyRef"], {"name": SECRET_NAME, "key": key})


class RabbitMQTopologyTests(unittest.TestCase):
    def setUp(self):
        self.template = topology_template()

    def test_topology_declares_version_marker(self):
        parameters = {
            item["name"]: item["value"]
            for item in self.template["global_parameters"]
        }
        self.assertEqual(parameters.get("glasslab/topology-version"), 1)

    def test_primary_exchanges_and_quorum_queues_are_declared(self):
        exchanges = {item["name"]: item for item in self.template["exchanges"]}
        queues = {item["name"]: item for item in self.template["queues"]}
        for base in ("glasslab.orchestrator.control", "glasslab.workflow.execution"):
            with self.subTest(base=base):
                self.assertEqual(exchanges[base]["type"], "topic")
                self.assertTrue(exchanges[base]["durable"])
                queue = queues[base]
                arguments = queue["arguments"]
                self.assertEqual(arguments["x-queue-type"], "quorum")
                self.assertTrue(queue["durable"])
                self.assertIn("x-max-length", arguments)
                self.assertIn("x-max-length-bytes", arguments)
                # Quorum queues in RabbitMQ 4.3.5 reject
                # "reject-publish-dlx"; overflow instead nacks publisher
                # confirms so the authoritative outbox republishes later.
                self.assertEqual(arguments["x-overflow"], "reject-publish")
                self.assertEqual(arguments["x-dead-letter-exchange"], f"{base}.dlx")

    def test_retry_queues_delay_and_return_to_primary_exchange(self):
        queues = {item["name"]: item for item in self.template["queues"]}
        for base in ("glasslab.orchestrator.control", "glasslab.workflow.execution"):
            with self.subTest(base=base):
                retry = queues[f"{base}.retry"]
                arguments = retry["arguments"]
                self.assertEqual(arguments["x-queue-type"], "quorum")
                self.assertIn("x-message-ttl", arguments)
                self.assertGreater(arguments["x-message-ttl"], 0)
                self.assertEqual(arguments["x-dead-letter-exchange"], base)

    def test_dead_letter_queues_capture_rejected_work(self):
        exchanges = {item["name"]: item for item in self.template["exchanges"]}
        queues = {item["name"]: item for item in self.template["queues"]}
        bindings = {(item["source"], item["destination"]) for item in self.template["bindings"]}
        for base in ("glasslab.orchestrator.control", "glasslab.workflow.execution"):
            with self.subTest(base=base):
                dlx = f"{base}.dlx"
                self.assertTrue(exchanges[dlx]["durable"])
                dlq = f"{base}.dlq"
                self.assertEqual(queues[dlq]["arguments"]["x-queue-type"], "quorum")
                self.assertIn((dlx, dlq), bindings)

    def test_bindings_route_exchanges_to_their_queues(self):
        bindings = {(item["source"], item["destination"]) for item in self.template["bindings"]}
        for base in ("glasslab.orchestrator.control", "glasslab.workflow.execution"):
            with self.subTest(base=base):
                self.assertIn((base, base), bindings)
                self.assertIn((f"{base}.retry", f"{base}.retry"), bindings)


class RabbitMQDocumentationTests(unittest.TestCase):
    def setUp(self):
        self.readme = (RABBITMQ_DIR / "README.md").read_text(encoding="utf-8")

    def test_single_node_quorum_is_documented_as_persistent_not_ha(self):
        lowered = self.readme.lower()
        self.assertIn("not highly available", lowered)
        self.assertIn("persistent", lowered)

    def test_total_volume_loss_reconstruction_is_documented(self):
        lowered = self.readme.lower()
        self.assertIn("volume loss", lowered)
        self.assertIn("outbox", lowered)

    def test_incompatible_declaration_behavior_and_ownership_are_documented(self):
        lowered = self.readme.lower()
        self.assertIn("incompatible", lowered)
        self.assertIn("ownership", lowered)


class RabbitMQRolloutTests(unittest.TestCase):
    def setUp(self):
        self.rollout = (REPOSITORY_ROOT / "scripts" / "rollout-research-services.sh").read_text(encoding="utf-8")

    def test_rollout_supports_rabbitmq_service_target(self):
        self.assertIn("rabbitmq)", self.rollout)
        self.assertIn("rollout_rabbitmq", self.rollout)

    def test_rollout_requires_secret_and_pvc_before_applying_statefulset(self):
        function_body = self.rollout[self.rollout.index("rollout_rabbitmq()") :]
        function_body = function_body[: function_body.index("\n}")]
        self.assertIn(f"require_object secret {SECRET_NAME}", function_body)
        self.assertIn("require_object persistentvolumeclaim glasslab-rabbitmq-data", function_body)
        secret_position = function_body.index(f"require_object secret {SECRET_NAME}")
        apply_position = function_body.index("60-statefulset.yaml")
        self.assertLess(secret_position, apply_position)

    def test_rollout_applies_tracked_rabbitmq_manifests_and_waits(self):
        for manifest in (
            "20-configmap.yaml",
            "30-topology.yaml",
            "40-service.yaml",
            "50-network-policy.yaml",
            "60-statefulset.yaml",
        ):
            self.assertIn(f"rabbitmq/{manifest}", self.rollout)
        self.assertIn("statefulset/glasslab-rabbitmq", self.rollout)

    def test_default_rollout_bundle_does_not_include_the_broker(self):
        bundle = self.rollout[
            self.rollout.index("rollout_authenticated_workflow_bundle()") :
            self.rollout.index("rollout_research_orchestrator()")
        ]
        self.assertNotIn("rollout_rabbitmq", bundle)


if __name__ == "__main__":
    unittest.main()
