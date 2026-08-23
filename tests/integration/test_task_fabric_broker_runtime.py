"""Runtime integration tests for the task-fabric RabbitMQ manifests.

These tests exercise the tracked manifests against the pinned RabbitMQ 4.3.5
image using local Docker. They demonstrate runtime properties that static
manifest checks cannot: boot-time definitions-import semantics, merge
behavior on existing entities, drift detection, credential rotation, erlang
cookie rotation, and restart persistence.

Live-cluster properties that still require operator validation (PVC
reattachment across node loss, total-volume-loss reconstruction drills) are
NOT claimed here; see kubeadm/glasslab-v2/rabbitmq/README.md.

Tests skip cleanly when Docker is unavailable.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
RABBITMQ_DIR = REPOSITORY_ROOT / "kubeadm" / "glasslab-v2" / "rabbitmq"

RABBITMQ_IMAGE = "rabbitmq@sha256:cb038b7a48d8b73507c83ff446245546a9459ac53e9ce79615217b4fbd917d50"
RENDERER_IMAGE = "python@sha256:9c900dea9e8fb7e16277c179b555cc72d29a352dbc33cff48ad5a0412fd5bfc7"

PASSWORDS_A = {
    "erlang_cookie": "runtime-test-cookie-A",
    "topology_admin_password": "rt-admin-pass-A",
    "publisher_password": "rt-publisher-pass-A",
    "consumer_password": "rt-consumer-pass-A",
    "monitoring_password": "rt-monitor-pass-A",
}

EXPECTED_USERS = {
    "glasslab-topology-admin": ["administrator"],
    "glasslab-publisher": [],
    "glasslab-consumer": [],
    "glasslab-monitor": ["monitoring"],
}


def docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    probe = subprocess.run(
        ["docker", "info", "--format", "ok"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return probe.returncode == 0


def run(args: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=kwargs.pop("timeout", 300), **kwargs)


class BrokerRuntimeTests(unittest.TestCase):
    """Shared environment: manifests extracted once, one broker per scenario."""

    @classmethod
    def setUpClass(cls):
        if not docker_available():
            raise unittest.SkipTest("docker is not available")
        cls.scratch = Path(tempfile.mkdtemp(prefix="task-fabric-broker-"))
        cls.workdir = cls.scratch / "work"
        cls.workdir.mkdir()
        cls._extract_manifest_assets()
        cls.containers: list[str] = []

    @classmethod
    def tearDownClass(cls):
        for name in getattr(cls, "containers", []):
            run(["docker", "rm", "-f", name])
        shutil.rmtree(cls.scratch, ignore_errors=True)

    @classmethod
    def _extract_manifest_assets(cls):
        configmap = next(
            item
            for item in yaml.safe_load_all((RABBITMQ_DIR / "20-configmap.yaml").read_text())
            if item
        )
        topology = next(
            item
            for item in yaml.safe_load_all((RABBITMQ_DIR / "30-topology.yaml").read_text())
            if item
        )
        assets = {
            "rabbitmq.conf": configmap["data"]["rabbitmq.conf"],
            "enabled_plugins": configmap["data"]["enabled_plugins"],
            "render.py": topology["data"]["render.py"],
            "definitions.template.json": topology["data"]["definitions.template.json"],
            "verify.eval": topology["data"]["verify.eval"],
            "verify-topology.sh": topology["data"]["verify-topology.sh"],
        }
        for name, content in assets.items():
            (cls.workdir / name).write_text(content)

    @classmethod
    def _render(cls, dirname: str, passwords: dict[str, str]):
        rendered = cls.scratch / dirname
        data = cls.scratch / f"{dirname}-data"
        rendered.mkdir(exist_ok=True)
        data.mkdir(exist_ok=True)
        # The renderer and broker run as uid 999 inside Docker; the host-side
        # scratch directories must be traversable/writable by that uid.
        rendered.chmod(0o777)
        data.chmod(0o777)
        result = run([
            "docker", "run", "--rm", "--user", "999:999",
            "-v", str(cls.workdir) + ":/work",
            "-v", str(rendered) + ":/work/rendered",
            "-v", str(data) + ":/var/lib/rabbitmq",
            *[item for key, value in passwords.items() for item in ("-e", f"{key}={value}")],
            RENDERER_IMAGE, "python", "/work/render.py",
        ])
        assert result.returncode == 0, result.stderr
        return rendered, data

    @classmethod
    def _start_broker(cls, name: str, rendered: Path, data: Path):
        run(["docker", "rm", "-f", name])
        cls.containers.append(name)
        result = run([
            "docker", "run", "-d", "--name", name, "--user", "999:999",
            "-v", str(cls.workdir / "rabbitmq.conf") + ":/etc/rabbitmq/rabbitmq.conf:ro",
            "-v", str(cls.workdir / "enabled_plugins") + ":/etc/rabbitmq/enabled_plugins:ro",
            "-v", str(rendered) + ":/etc/rabbitmq/rendered",
            "-v", str(data) + ":/var/lib/rabbitmq",
            RABBITMQ_IMAGE,
        ])
        assert result.returncode == 0, result.stderr

    @classmethod
    def _wait_ping(cls, name: str, timeout_seconds: int = 150) -> bool:
        # check_running gates on the rabbit application being fully started;
        # plain ping succeeds as soon as Erlang distribution is up, which
        # happens before definitions import completes.
        attempts = timeout_seconds // 5
        for _ in range(attempts):
            probe = run(
                ["docker", "exec", name, "rabbitmq-diagnostics", "-q", "check_running"],
                timeout=60,
            )
            if probe.returncode == 0:
                return True
            state = run(["docker", "inspect", "-f", "{{.State.Running}}", name])
            if state.stdout.strip() != "true":
                return False
        return False

    @classmethod
    def _boot_new_broker(cls, name: str, dirname: str, passwords: dict[str, str]) -> None:
        rendered, data = cls._render(dirname, passwords)
        cls._start_broker(name, rendered, data)
        assert cls._wait_ping(name), f"broker {name} did not become ready"

    def _exec(self, name: str, command: str, timeout: int = 120) -> subprocess.CompletedProcess:
        return run(["docker", "exec", name, "sh", "-c", command], timeout=timeout)

    def _live_definitions(self, name: str) -> dict:
        exported = self._exec(name, "rabbitmqctl export_definitions /tmp/live.json >/dev/null && cat /tmp/live.json")
        self.assertEqual(exported.returncode, 0, exported.stderr)
        return json.loads(exported.stdout)

    # -- helpers above; scenario scaffolding below --------------------------

    @classmethod
    def _mount_verify_assets(cls, name: str):
        """Copy verifier assets into a running container (Docker cannot model
        the Kubernetes postStart volume mounts; Kubernetes provides them)."""
        prep = run(["docker", "exec", name, "mkdir", "-p", "/etc/rabbitmq/glasslab-verify"])
        assert prep.returncode == 0, prep.stderr
        for asset in ("verify.eval", "verify-topology.sh"):
            src = cls.workdir / asset
            copied = run(["docker", "cp", str(src), f"{name}:/etc/rabbitmq/glasslab-verify/{asset}"])
            assert copied.returncode == 0, copied.stderr

    def _assert_expected_topology(self, name: str):
        live = self._live_definitions(name)
        vhost_queues = {
            item["name"]: item
            for item in live["queues"]
            if item.get("vhost") == "glasslab"
        }
        template = json.loads((self.workdir / "definitions.template.json").read_text())
        for wanted in template["queues"]:
            actual = vhost_queues[wanted["name"]]
            self.assertEqual(actual["type"], "quorum", wanted["name"])
            self.assertTrue(actual["durable"], wanted["name"])
            for key, value in wanted["arguments"].items():
                self.assertEqual(actual["arguments"][key], value, f"{wanted['name']}:{key}")
        users = {user["name"]: user for user in live["users"]}
        self.assertEqual(set(users), set(EXPECTED_USERS))
        self.assertNotIn("guest", users)

    # -- scenarios -----------------------------------------------------------

    def test_a_boot_imports_full_topology_with_no_plugins_no_ui_no_guest(self):
        self._boot_new_broker("tf-rt-a", "a-rendered", PASSWORDS_A)
        self._mount_verify_assets("tf-rt-a")

        enabled = self._exec("tf-rt-a", "cat /etc/rabbitmq/enabled_plugins").stdout.strip()
        self.assertEqual(enabled, "[].")

        listeners = self._exec("tf-rt-a", "rabbitmq-diagnostics -q listeners").stdout
        self.assertNotIn("http", listeners)
        self.assertIn("5672", listeners)

        self._assert_expected_topology("tf-rt-a")

        verified = self._exec("tf-rt-a", "sh /etc/rabbitmq/glasslab-verify/verify-topology.sh")
        self.assertEqual(verified.returncode, 0, verified.stdout + verified.stderr)
        self.assertIn("topology verified", verified.stdout)

        logs = run(["docker", "logs", "tf-rt-a"]).stdout + run(["docker", "logs", "tf-rt-a"]).stderr
        self.assertNotIn("Invalid overflow", logs)

    def test_b_merge_semantics_ignore_arg_changes_but_drift_check_fails(self):
        self._boot_new_broker("tf-rt-b", "b-rendered", PASSWORDS_A)
        self._mount_verify_assets("tf-rt-b")

        # Simulate an incompatible Git change reaching the rendered file while
        # the entity already exists on the broker.
        edit = self._exec(
            "tf-rt-b",
            'sed -i \'s/"x-max-length": 10000/"x-max-length": 9999/\' /etc/rabbitmq/rendered/definitions.json',
        )
        self.assertEqual(edit.returncode, 0, edit.stderr)

        restarted = run(["docker", "restart", "tf-rt-b"])
        self.assertEqual(restarted.returncode, 0)
        self.assertTrue(self._wait_ping("tf-rt-b"), "broker did not come back after re-import")

        # Merge semantics: the existing queue keeps its original arguments.
        live = self._live_definitions("tf-rt-b")
        primary = next(q for q in live["queues"] if q["name"] == "glasslab.orchestrator.control")
        self.assertEqual(primary["arguments"]["x-max-length"], 10000)

        # Drift enforcement: expected (9999) vs live (10000) must fail loudly.
        verified = self._exec("tf-rt-b", "sh /etc/rabbitmq/glasslab-verify/verify-topology.sh")
        self.assertNotEqual(verified.returncode, 0)
        self.assertIn("topology_drift", verified.stderr + verified.stdout)

    def test_c_password_rotation_via_reimport_upserts_existing_users(self):
        self._boot_new_broker("tf-rt-c", "c-rendered", PASSWORDS_A)
        self._mount_verify_assets("tf-rt-c")

        pre = self._exec("tf-rt-c", "rabbitmqctl authenticate_user glasslab-publisher rt-publisher-pass-A")
        self.assertEqual(pre.returncode, 0)

        rotated = self._exec(
            "tf-rt-c",
            'sed -i \'s/"password": "rt-publisher-pass-A"/"password": "rt-publisher-pass-B"/\' /etc/rabbitmq/rendered/definitions.json',
        )
        self.assertEqual(rotated.returncode, 0, rotated.stderr)

        restarted = run(["docker", "restart", "tf-rt-c"])
        self.assertEqual(restarted.returncode, 0)
        self.assertTrue(self._wait_ping("tf-rt-c"))

        new_pass = self._exec("tf-rt-c", "rabbitmqctl authenticate_user glasslab-publisher rt-publisher-pass-B")
        self.assertEqual(new_pass.returncode, 0, "rotated password must authenticate after re-import")
        old_pass = self._exec("tf-rt-c", "rabbitmqctl authenticate_user glasslab-publisher rt-publisher-pass-A")
        self.assertNotEqual(old_pass.returncode, 0, "old password must stop working after rotation")

    def test_d_cookie_rotation_preserves_persistent_metadata(self):
        self._boot_new_broker("tf-rt-d", "d-rendered", PASSWORDS_A)

        stopped = run(["docker", "stop", "-t", "30", "tf-rt-d"])
        self.assertEqual(stopped.returncode, 0)

        rewrite = run([
            "docker", "run", "--rm", "--user", "0",
            "-v", str(self.scratch / "d-rendered-data") + ":/var/lib/rabbitmq",
            RENDERER_IMAGE,
            "sh", "-c",
            'printf "runtime-test-cookie-TOTALLY-DIFFERENT\\n" > /var/lib/rabbitmq/.erlang.cookie && chown 999:999 /var/lib/rabbitmq/.erlang.cookie && chmod 600 /var/lib/rabbitmq/.erlang.cookie',
        ])
        self.assertEqual(rewrite.returncode, 0, rewrite.stderr)

        started = run(["docker", "start", "tf-rt-d"])
        self.assertEqual(started.returncode, 0)
        self.assertTrue(self._wait_ping("tf-rt-d"), "broker must boot after cookie rotation")

        live = self._live_definitions("tf-rt-d")
        queue_names = {q["name"] for q in live["queues"] if q.get("vhost") == "glasslab"}
        self.assertGreaterEqual(len(queue_names), 6, "queues must survive cookie rotation")
        users = {u["name"] for u in live["users"]}
        self.assertEqual(users, set(EXPECTED_USERS))

    def test_e_plain_restart_persists_topology(self):
        self._boot_new_broker("tf-rt-e", "e-rendered", PASSWORDS_A)

        restarted = run(["docker", "restart", "tf-rt-e"])
        self.assertEqual(restarted.returncode, 0)
        self.assertTrue(self._wait_ping("tf-rt-e"))

        self._assert_expected_topology("tf-rt-e")

    def test_f_version_is_derived_from_rendered_file_and_old_entities_drain(self):
        name = "tf-rt-f"
        self._boot_new_broker(name, "f-rendered", PASSWORDS_A)
        self._mount_verify_assets(name)

        # Baseline: v1 expected vs v1 live verifies cleanly.
        baseline = self._exec(name, "sh /etc/rabbitmq/glasslab-verify/verify-topology.sh")
        self.assertEqual(baseline.returncode, 0, baseline.stdout + baseline.stderr)

        # Build a v2 topology file: version bumped, one new queue added, one
        # old queue (plus its binding) retired so it can drain.
        template = json.loads((self.workdir / "definitions.template.json").read_text())
        for entry in template["global_parameters"]:
            if entry["name"] == "glasslab/topology-version":
                entry["value"] = 2
        retired_queue = "glasslab.orchestrator.control.retry"
        template["queues"] = [q for q in template["queues"] if q["name"] != retired_queue]
        template["bindings"] = [
            b for b in template["bindings"] if b.get("destination") != retired_queue
        ]
        new_queue = {
            "name": "glasslab.orchestrator.control.v2",
            "vhost": "glasslab",
            "durable": True,
            "auto_delete": False,
            "arguments": {
                "x-queue-type": "quorum",
                "x-max-length": 10000,
                "x-max-length-bytes": 536870912,
                "x-overflow": "reject-publish",
                "x-dead-letter-exchange": "glasslab.orchestrator.control.dlx",
            },
        }
        template["queues"].append(new_queue)
        template["bindings"].append({
            "source": "glasslab.orchestrator.control",
            "vhost": "glasslab",
            "destination": new_queue["name"],
            "destination_type": "queue",
            "routing_key": "v2",
            "arguments": {},
        })
        (self.workdir / "definitions.template.json").write_text(json.dumps(template, indent=2))

        # Re-render with the v2 template; the running broker is still v1.
        rendered, _ = self._render("f-rendered", PASSWORDS_A)
        pre_restart = self._exec(name, "sh /etc/rabbitmq/glasslab-verify/verify-topology.sh")
        self.assertNotEqual(
            pre_restart.returncode,
            0,
            "verifier must fail while live broker is still on version 1 — "
            "this proves the expected version is derived from the rendered "
            "file rather than hard-coded",
        )

        # Roll the broker: import applies the v2 parameter and creates the
        # new queue; the retired queue keeps draining as a tolerated extra.
        restarted = run(["docker", "restart", name])
        self.assertEqual(restarted.returncode, 0)
        self.assertTrue(self._wait_ping(name))

        post = self._exec(name, "sh /etc/rabbitmq/glasslab-verify/verify-topology.sh")
        self.assertEqual(post.returncode, 0, post.stdout + post.stderr)

        live = self._live_definitions(name)
        versions = [
            entry["value"]
            for entry in live["global_parameters"]
            if entry["name"] == "glasslab/topology-version"
        ]
        self.assertEqual(versions, [2], "boot re-import must update the topology-version parameter")
        queue_names = {q["name"] for q in live["queues"] if q.get("vhost") == "glasslab"}
        self.assertIn(new_queue["name"], queue_names)
        self.assertIn(retired_queue, queue_names, "retired entity must be tolerated while draining")

    def test_g_identity_state_is_enforced_exactly(self):
        name = "tf-rt-g"
        self._boot_new_broker(name, "g-rendered", PASSWORDS_A)
        self._mount_verify_assets(name)

        baseline = self._exec(name, "sh /etc/rabbitmq/glasslab-verify/verify-topology.sh")
        self.assertEqual(baseline.returncode, 0, baseline.stdout + baseline.stderr)

        # Injected extra admin user must fail verification.
        injected_user = self._exec(
            name,
            'rabbitmqctl add_user glasslab-intruder rt-intruder-pass >/dev/null && '
            'rabbitmqctl set_permissions -p glasslab glasslab-intruder ".*" ".*" ".*" >/dev/null',
        )
        self.assertEqual(injected_user.returncode, 0, injected_user.stderr)
        failed_user = self._exec(name, "sh /etc/rabbitmq/glasslab-verify/verify-topology.sh")
        self.assertNotEqual(failed_user.returncode, 0, "unexpected user must fail verification")
        combined = failed_user.stdout + failed_user.stderr
        self.assertIn("unexpected", combined)

        # Removing the intruder restores conformance.
        removed = self._exec(name, "rabbitmqctl delete_user glasslab-intruder >/dev/null")
        self.assertEqual(removed.returncode, 0, removed.stderr)
        restored = self._exec(name, "sh /etc/rabbitmq/glasslab-verify/verify-topology.sh")
        self.assertEqual(restored.returncode, 0, restored.stdout + restored.stderr)

        # An unexpected permission grant on the Glasslab vhost must also fail.
        grant = self._exec(
            name,
            'rabbitmqctl set_permissions -p glasslab glasslab-monitor ".*" ".*" ".*" >/dev/null',
        )
        self.assertEqual(grant.returncode, 0, grant.stderr)
        failed_grant = self._exec(name, "sh /etc/rabbitmq/glasslab-verify/verify-topology.sh")
        self.assertNotEqual(failed_grant.returncode, 0, "unexpected permission grant must fail verification")
        self.assertIn("unexpected_grant", failed_grant.stdout + failed_grant.stderr)

        revoke = self._exec(
            name,
            'rabbitmqctl set_permissions -p glasslab glasslab-monitor "" "" ".*" >/dev/null',
        )
        self.assertEqual(revoke.returncode, 0, revoke.stderr)

        # An unexpected vhost must fail; only the Glasslab vhost and the
        # RabbitMQ system default are allowlisted.
        new_vhost = self._exec(name, "rabbitmqctl add_vhost intruder-vhost >/dev/null")
        self.assertEqual(new_vhost.returncode, 0, new_vhost.stderr)
        failed_vhost = self._exec(name, "sh /etc/rabbitmq/glasslab-verify/verify-topology.sh")
        self.assertNotEqual(failed_vhost.returncode, 0, "unexpected vhost must fail verification")
        self.assertIn("unexpected_vhost", failed_vhost.stdout + failed_vhost.stderr)

        deleted_vhost = self._exec(name, "rabbitmqctl delete_vhost intruder-vhost >/dev/null")
        self.assertEqual(deleted_vhost.returncode, 0, deleted_vhost.stderr)
        final = self._exec(name, "sh /etc/rabbitmq/glasslab-verify/verify-topology.sh")
        self.assertEqual(final.returncode, 0, final.stdout + final.stderr)


if __name__ == "__main__":
    unittest.main()
