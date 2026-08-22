"""Behavioral tests for the non-revealing credential hygiene scanner."""

import base64
import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCANNER_PATH = REPOSITORY_ROOT / "scripts" / "check-credential-hygiene.py"


def load_scanner():
    spec = importlib.util.spec_from_file_location("credential_hygiene", SCANNER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load credential hygiene scanner")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def encoded(value: str) -> str:
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


class CredentialHygieneScannerTests(unittest.TestCase):
    """Each test names a credential form that must not re-enter tracked files."""

    def scan_fixture(self, files: dict[str, str]):
        scanner = load_scanner()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, contents in files.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(contents, encoding="utf-8")
            return scanner.scan_tree(root)

    def rule_ids(self, files: dict[str, str]) -> set[str]:
        return {finding.rule_id for finding in self.scan_fixture(files)}

    def test_detects_base64_dsn_in_kubernetes_secret_data(self):
        """A deployable Secret must not hide a database DSN behind base64."""
        findings = self.scan_fixture(
            {
                "secret.yaml": "\n".join(
                    [
                        "apiVersion: v1",
                        "kind: Secret",
                        "data:",
                        f"  DATABASE_URL: {encoded('postgresql://user:REDACTED@db.invalid:5432/app')}",
                    ]
                )
            }
        )

        self.assertEqual([(finding.path, finding.line, finding.rule_id) for finding in findings], [
            (Path("secret.yaml"), 4, "secret-data-dsn")
        ])

    def test_detects_sha512_crypt_verifier(self):
        """Published SHA-512 crypt password verifiers must be rejected."""
        findings = self.scan_fixture(
            {
                "cloud-init.yaml": (
                    "passwd: $" + "6$rounds=4096$publicsalt$"
                    "abcdefghijklmnopqrstuvwxzy0123456789ABCDEFGHIJKLMN\n"
                )
            }
        )

        self.assertEqual(
            [(finding.line, finding.rule_id) for finding in findings],
            [(1, "sha512-crypt-verifier")],
        )

    def test_detects_sshpass_password_flag(self):
        """Password-bearing sshpass invocations must not be tracked."""
        self.assertEqual(
            self.rule_ids({"deploy.sh": "ssh" + "pass -p 'REDACTED' ssh host\n"}),
            {"sshpass-password"},
        )

    def test_detects_known_exposed_value_by_digest(self):
        """Known exposed values are detected by SHA-256 without outputting them."""
        self.assertEqual(
            self.rule_ids({"legacy.env": "LEGACY_VALUE=" + "credential-hygiene-fixture-only\n"}),
            {"known-exposed-value"},
        )

    def test_detects_deployable_change_me_secret_manifest(self):
        """A Kubernetes Secret with a change-me value is not a safe example."""
        self.assertEqual(
            self.rule_ids(
                {
                    "example.yaml": "\n".join(
                        [
                            "apiVersion: v1",
                            "kind: Secret",
                            "metadata:",
                            "  name: example",
                            "stringData:",
                            "  TOKEN: change-me",
                        ]
                    )
                }
            ),
            {"deployable-change-me-secret"},
        )

    def test_detects_base64_change_me_secret_data(self):
        """Base64 encoding cannot make a deployable change-me value safe."""
        self.assertEqual(
            self.rule_ids(
                {
                    "example.yaml": "\n".join(
                        [
                            "apiVersion: v1",
                            "kind: Secret",
                            "data:",
                            f"  TOKEN: {encoded('change-me')}",
                        ]
                    )
                }
            ),
            {"deployable-change-me-secret"},
        )

    def test_allows_redacted_examples_and_public_ssh_keys(self):
        """Redacted documentation and public keys must remain usable controls."""
        findings = self.scan_fixture(
            {
                "safe.yaml": "\n".join(
                    [
                        "apiVersion: v1",
                        "kind: Secret",
                        "data:",
                        f"  AUTHORIZED_KEY: {encoded('ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakePublicKeyOnly operator@example')}",
                        "---",
                        "apiVersion: v1",
                        "kind: ConfigMap",
                        "data:",
                        "  example: <redacted>",
                    ]
                ),
                "deprecated-whatsapp/legacy.sh": "ssh" + "pass -p 'REDACTED' ssh host\n",
                "scan-artifacts/previous.txt": "ssh" + "pass -p 'REDACTED' ssh host\n",
                ".superpowers/sdd/review.diff": "ssh" + "pass -p 'REDACTED' ssh host\n",
            }
        )

        self.assertEqual(findings, [])

    def test_does_not_honor_content_controlled_fixture_marker(self):
        """An ordinary comment cannot suppress credential scanning."""
        self.assertEqual(
            self.rule_ids(
                {
                    "manifest.yaml": (
                        "# credential-hygiene: fixture\n"
                        + "ssh"
                        + "pass -p 'REDACTED' ssh host\n"
                    )
                }
            ),
            {"sshpass-password"},
        )

    def test_detects_secret_data_before_kind(self):
        """Secret fields are detected even when YAML keys use a different order."""
        self.assertEqual(
            self.rule_ids(
                {
                    "secret.yaml": "\n".join(
                        [
                            "apiVersion: v1",
                            "data:",
                            f"  DATABASE_URL: {encoded('postgresql://user:REDACTED@db.invalid:5432/app')}",
                            "kind: Secret",
                        ]
                    )
                }
            ),
            {"secret-data-dsn"},
        )

    def test_detects_secret_inside_kubernetes_list(self):
        """A List item that is a Secret is scanned with its source line intact."""
        findings = self.scan_fixture(
            {
                "list.yaml": "\n".join(
                    [
                        "apiVersion: v1",
                        "kind: List",
                        "items:",
                        "  - apiVersion: v1",
                        "    kind: Secret",
                        "    data:",
                        f"      DATABASE_URL: {encoded('postgresql://user:REDACTED@db.invalid:5432/app')}",
                    ]
                )
            }
        )

        self.assertEqual(
            [(finding.path, finding.line, finding.rule_id) for finding in findings],
            [(Path("list.yaml"), 7, "secret-data-dsn")],
        )

    def test_detects_secretlist_items_nested_inside_list(self):
        """Nested SecretList items inherit Secret scanning without scanning arbitrary mappings."""
        findings = self.scan_fixture(
            {
                "nested-list.yaml": "\n".join(
                    [
                        "apiVersion: v1",
                        "kind: List",
                        "items:",
                        "  - apiVersion: v1",
                        "    kind: SecretList",
                        "    items:",
                        "      - metadata:",
                        "          name: example",
                        "        data:",
                        f"          DATABASE_URL: {encoded('postgresql://user:REDACTED@db.invalid:5432/app')}",
                    ]
                )
            }
        )

        self.assertEqual(
            [(finding.path, finding.line, finding.rule_id) for finding in findings],
            [(Path("nested-list.yaml"), 10, "secret-data-dsn")],
        )

    def test_detects_flow_style_secret_data(self):
        """Flow-style Secret data mappings cannot conceal a base64 DSN."""
        self.assertEqual(
            self.rule_ids(
                {
                    "secret.yaml": (
                        "apiVersion: v1\n"
                        "kind: Secret\n"
                        f"data: {{DATABASE_URL: {encoded('postgresql://user:REDACTED@db.invalid:5432/app')}}}\n"
                    )
                }
            ),
            {"secret-data-dsn"},
        )

    def test_detects_flow_style_secret_string_data(self):
        """Flow-style Secret stringData mappings cannot contain change-me."""
        self.assertEqual(
            self.rule_ids(
                {
                    "secret.yaml": (
                        "apiVersion: v1\n"
                        "kind: Secret\n"
                        "stringData: {TOKEN: change-me}\n"
                    )
                }
            ),
            {"deployable-change-me-secret"},
        )

    def test_allows_redacted_kubernetes_secret(self):
        """A Kubernetes Secret with explicitly redacted string data is safe."""
        self.assertEqual(
            self.scan_fixture(
                {
                    "secret.yaml": "\n".join(
                        [
                            "apiVersion: v1",
                            "kind: Secret",
                            "stringData:",
                            "  TOKEN: <redacted>",
                        ]
                    )
                }
            ),
            [],
        )

    def test_cli_reports_only_location_and_rule_id(self):
        """Scanner output must never include the matched credential material."""
        dsn = "postgresql://user:REDACTED@db.invalid:5432/app"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "secret.yaml").write_text(
                "\n".join(
                    [
                        "apiVersion: v1",
                        "kind: Secret",
                        "data:",
                        f"  DATABASE_URL: {encoded(dsn)}",
                    ]
                ),
                encoding="utf-8",
            )
            result = subprocess.run(
                [sys.executable, str(SCANNER_PATH), str(root)],
                check=False,
                capture_output=True,
                text=True,
            )

        self.assertEqual(result.returncode, 1)
        self.assertIn("secret.yaml:4:secret-data-dsn", result.stdout)
        self.assertNotIn(dsn, result.stdout)
        self.assertNotIn(encoded(dsn), result.stdout)


class RepositoryCredentialPolicyTests(unittest.TestCase):
    """Repository deployment artifacts must fail closed around live credentials."""

    SECRET_EXAMPLES = (
        REPOSITORY_ROOT / "kubeadm" / "agent-stack" / "12-agent-secrets.example.yaml",
        REPOSITORY_ROOT / "kubeadm" / "glasslab-v2" / "minio" / "10-secret.example.yaml",
        REPOSITORY_ROOT / "kubeadm" / "glasslab-v2" / "postgres" / "10-secret.example.yaml",
        REPOSITORY_ROOT / "kubeadm" / "glasslab-v2" / "workflow-api" / "10-secret.example",
        REPOSITORY_ROOT
        / "kubeadm"
        / "glasslab-v2"
        / "research-orchestrator"
        / "11-secret.example.yaml",
    )
    VLLM_DEPLOY_SCRIPT = REPOSITORY_ROOT / "scripts" / "deploy-vllm.sh"
    VLLM_TEST_SCRIPT = REPOSITORY_ROOT / "scripts" / "test-vllm.sh"
    GPU_RUNNER_DIR = REPOSITORY_ROOT / "kubeadm" / "glasslab-v2" / "gpu-runner"
    GPU_DEPLOY_SCRIPT = REPOSITORY_ROOT / "scripts" / "deploy-gpu-runner.sh"
    PXE_ROOT = (
        REPOSITORY_ROOT
        / "live-config"
        / "provisioner"
        / "var"
        / "www"
        / "html"
        / "pxe"
        / "cloud-init"
    )
    PXE_PROFILES = ("default", "node02", "node03", "node04", "node05", "node48", "node49")

    def run_vllm_deploy(
        self,
        *,
        secret_file: Path | None = None,
        cluster_secret_exists: bool = False,
        applied_secret_has_key: bool = True,
    ) -> tuple[subprocess.CompletedProcess[str], list[str]]:
        with tempfile.TemporaryDirectory() as directory:
            fixture_root = Path(directory)
            bin_dir = fixture_root / "bin"
            bin_dir.mkdir()
            calls_path = fixture_root / "kubectl-calls"
            live_key_path = fixture_root / "live-secret-key"
            if cluster_secret_exists:
                live_key_path.touch()
            kubectl = bin_dir / "kubectl"
            kubectl.write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' \"$*\" >> \"$KUBECTL_CALLS\"\n"
                "if [[ \"${1-}\" == get && \"${2-}\" == secret ]]; then\n"
                "  [[ \"${3-}\" == glasslab-agent-secrets && \"${4-}\" == -n && \"${5-}\" == glasslab-agents ]] || exit 1\n"
                "  [[ -f \"$FAKE_LIVE_SECRET_KEY\" ]] || exit 1\n"
                "  printf 'Zml4dHVyZQ=='\n"
                "  exit 0\n"
                "fi\n"
                "if [[ \"${1-}\" == apply && \"${FAKE_APPLIED_SECRET_HAS_KEY:-0}\" == 1 ]]; then\n"
                "  case \"${3-}\" in *agent-secrets*) touch \"$FAKE_LIVE_SECRET_KEY\" ;; esac\n"
                "fi\n",
                encoding="utf-8",
            )
            kubectl.chmod(0o755)
            environment = os.environ.copy()
            environment["PATH"] = f"{bin_dir}:{environment['PATH']}"
            environment["KUBECTL_CALLS"] = str(calls_path)
            environment["FAKE_LIVE_SECRET_KEY"] = str(live_key_path)
            environment["FAKE_APPLIED_SECRET_HAS_KEY"] = "1" if applied_secret_has_key else "0"
            if secret_file is None:
                environment["GLASSLAB_VLLM_SECRET_FILE"] = str(fixture_root / "missing.local.yaml")
            else:
                environment["GLASSLAB_VLLM_SECRET_FILE"] = str(secret_file)

            result = subprocess.run(
                ["bash", str(self.VLLM_DEPLOY_SCRIPT)],
                cwd=REPOSITORY_ROOT,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )
            calls = calls_path.read_text(encoding="utf-8").splitlines() if calls_path.exists() else []
            return result, calls

    def run_vllm_test(
        self, api_key: str | None
    ) -> tuple[subprocess.CompletedProcess[str], str, str, list[str], list[str]]:
        with tempfile.TemporaryDirectory() as directory:
            fixture_root = Path(directory)
            bin_dir = fixture_root / "bin"
            bin_dir.mkdir()
            argv_path = fixture_root / "curl-argv"
            config_copy_path = fixture_root / "curl-config-copy"
            config_paths_path = fixture_root / "curl-config-paths"
            config_modes_path = fixture_root / "curl-config-modes"
            curl = bin_dir / "curl"
            curl.write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' \"$*\" >> \"$CURL_ARGV\"\n"
                "previous=''\n"
                "for argument in \"$@\"; do\n"
                "  if [[ \"$previous\" == --config ]]; then\n"
                "    printf '%s\\n' \"$argument\" >> \"$CURL_CONFIG_PATHS\"\n"
                "    stat -c '%a' \"$argument\" >> \"$CURL_CONFIG_MODES\"\n"
                "    cp \"$argument\" \"$CURL_CONFIG_COPY\"\n"
                "  fi\n"
                "  previous=\"$argument\"\n"
                "done\n",
                encoding="utf-8",
            )
            curl.chmod(0o755)
            environment = os.environ.copy()
            environment["PATH"] = f"{bin_dir}:{environment['PATH']}"
            environment["CURL_ARGV"] = str(argv_path)
            environment["CURL_CONFIG_COPY"] = str(config_copy_path)
            environment["CURL_CONFIG_PATHS"] = str(config_paths_path)
            environment["CURL_CONFIG_MODES"] = str(config_modes_path)
            if api_key is None:
                environment.pop("VLLM_API_KEY", None)
            else:
                environment["VLLM_API_KEY"] = api_key

            result = subprocess.run(
                ["bash", str(self.VLLM_TEST_SCRIPT)],
                cwd=REPOSITORY_ROOT,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )
            argv = argv_path.read_text(encoding="utf-8") if argv_path.exists() else ""
            config_copy = config_copy_path.read_text(encoding="utf-8") if config_copy_path.exists() else ""
            config_paths = config_paths_path.read_text(encoding="utf-8").splitlines() if config_paths_path.exists() else []
            config_modes = config_modes_path.read_text(encoding="utf-8").splitlines() if config_modes_path.exists() else []
            removed_config_paths = [path for path in config_paths if not Path(path).exists()]
            return result, argv, config_copy, config_modes, removed_config_paths

    @staticmethod
    def vllm_secret_manifest(api_key: str) -> str:
        return (
            "apiVersion: v1\n"
            "kind: Secret\n"
            "metadata:\n"
            "  name: glasslab-agent-secrets\n"
            "  namespace: glasslab-agents\n"
            "stringData:\n"
            f"  VLLM_API_KEY: {api_key}\n"
        )

    def test_tracked_secret_examples_are_not_deployable_kubernetes_secrets(self):
        """Copy-pasting a tracked secret example must never create a live Secret."""
        for example_path in self.SECRET_EXAMPLES:
            with self.subTest(example=example_path.relative_to(REPOSITORY_ROOT)):
                documents = yaml.safe_load_all(example_path.read_text(encoding="utf-8"))
                self.assertNotIn(
                    "Secret",
                    {document.get("kind") for document in documents if isinstance(document, dict)},
                )

    def test_vllm_deploy_exits_before_apply_when_explicit_secret_file_is_absent(self):
        """A missing explicit vLLM Secret source must stop deployment before apply."""
        result, calls = self.run_vllm_deploy()

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any(call.startswith("apply ") for call in calls), calls)

    def test_vllm_deploy_accepts_preexisting_named_cluster_secret(self):
        """A pre-existing named Secret remains a valid fail-closed deployment source."""
        result, calls = self.run_vllm_deploy(cluster_secret_exists=True)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            calls[0],
            "get secret glasslab-agent-secrets -n glasslab-agents -o jsonpath={.data.VLLM_API_KEY}",
        )
        self.assertTrue(any(call.endswith("/11-vllm-deployment.yaml") for call in calls), calls)

    def test_vllm_deploy_applies_explicit_live_secret_before_workload(self):
        """A valid explicit local Secret remains usable and precedes the workload."""
        with tempfile.TemporaryDirectory() as directory:
            secret_path = Path(directory) / "agent-secrets.local.yaml"
            secret_path.write_text(self.vllm_secret_manifest("fixture-live-key"), encoding="utf-8")
            result, calls = self.run_vllm_deploy(secret_file=secret_path)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"apply -f {secret_path}", calls)
        secret_apply_index = calls.index(f"apply -f {secret_path}")
        workload_apply_index = next(
            index for index, call in enumerate(calls) if call.endswith("/11-vllm-deployment.yaml")
        )
        self.assertLess(secret_apply_index, workload_apply_index)

    def test_vllm_deploy_rejects_placeholder_local_secret(self):
        """An explicit local Secret containing a placeholder must never be applied."""
        with tempfile.TemporaryDirectory() as directory:
            secret_path = Path(directory) / "agent-secrets.local.yaml"
            secret_path.write_text(self.vllm_secret_manifest("change-me"), encoding="utf-8")
            result, calls = self.run_vllm_deploy(secret_file=secret_path)

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(calls, [])

    def test_vllm_test_requires_explicit_api_key(self):
        """The smoke test must stop rather than silently substituting a credential."""
        result, argv, _, _, _ = self.run_vllm_test(None)

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(argv, "")

    def test_vllm_test_keeps_api_key_out_of_argv_and_removes_private_config(self):
        """A real key must reach curl only through a temporary private config file."""
        api_key = "vllm-fixture-secret"
        result, argv, config_copy, config_modes, removed_paths = self.run_vllm_test(api_key)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(api_key, argv)
        self.assertIn(f"Authorization: Bearer {api_key}", config_copy)
        self.assertEqual(config_modes, ["600", "600"])
        self.assertEqual(len(removed_paths), 2)

    def run_gpu_deploy(
        self,
        *,
        secret_file: Path | None = None,
        cluster_secret_exists: bool = False,
        cluster_secret_has_key: bool = True,
        apply_creates_live_secret: bool = True,
        applied_secret_has_key: bool = True,
    ) -> tuple[subprocess.CompletedProcess[str], list[str]]:
        with tempfile.TemporaryDirectory() as directory:
            fixture_root = Path(directory)
            bin_dir = fixture_root / "bin"
            bin_dir.mkdir()
            calls_path = fixture_root / "kubectl-calls"
            live_secret_path = fixture_root / "live-secret"
            live_key_path = fixture_root / "live-secret-key"
            if cluster_secret_exists:
                live_secret_path.touch()
                if cluster_secret_has_key:
                    live_key_path.touch()
            kubectl = bin_dir / "kubectl"
            kubectl.write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' \"$*\" >> \"$KUBECTL_CALLS\"\n"
                "if [[ \"${1-}\" == get && \"${2-}\" == secret ]]; then\n"
                "  [[ \"${3-}\" == glasslab-v2-runner && \"${4-}\" == -n && \"${5-}\" == glasslab-v2 ]] || exit 1\n"
                "  [[ -f \"$FAKE_LIVE_SECRET\" ]] || exit 1\n"
                "  if [[ -f \"$FAKE_LIVE_SECRET_KEY\" ]]; then printf 'Zml4dHVyZQ=='; fi\n"
                "  exit 0\n"
                "fi\n"
                "if [[ \"${1-}\" == apply && \"${3-}\" != *'/00-all.yaml' ]]; then\n"
                "  if [[ \"${FAKE_APPLY_CREATES_LIVE_SECRET:-0}\" == 1 ]]; then touch \"$FAKE_LIVE_SECRET\"; fi\n"
                "  if [[ \"${FAKE_APPLIED_SECRET_HAS_KEY:-0}\" == 1 ]]; then touch \"$FAKE_LIVE_SECRET_KEY\"; fi\n"
                "fi\n",
                encoding="utf-8",
            )
            kubectl.chmod(0o755)
            environment = os.environ.copy()
            environment["PATH"] = f"{bin_dir}:{environment['PATH']}"
            environment["KUBECTL_CALLS"] = str(calls_path)
            environment["FAKE_LIVE_SECRET"] = str(live_secret_path)
            environment["FAKE_LIVE_SECRET_KEY"] = str(live_key_path)
            environment["FAKE_APPLY_CREATES_LIVE_SECRET"] = "1" if apply_creates_live_secret else "0"
            environment["FAKE_APPLIED_SECRET_HAS_KEY"] = "1" if applied_secret_has_key else "0"
            if secret_file is None:
                environment.pop("GLASSLAB_GPU_RUNNER_SECRET_FILE", None)
            else:
                environment["GLASSLAB_GPU_RUNNER_SECRET_FILE"] = str(secret_file)

            result = subprocess.run(
                ["bash", str(self.GPU_DEPLOY_SCRIPT), "--apply"],
                cwd=REPOSITORY_ROOT,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )
            calls = calls_path.read_text(encoding="utf-8").splitlines() if calls_path.exists() else []
            return result, calls

    @staticmethod
    def gpu_secret_manifest(
        *,
        kind: str = "Secret",
        name: str = "glasslab-v2-runner",
        namespace: str = "glasslab-v2",
        include_key: bool = True,
    ) -> str:
        string_data = "  GLASSLAB_RUNNER_STORE_POSTGRES_DSN: fixture-value\n" if include_key else "  OTHER_KEY: fixture-value\n"
        return (
            "apiVersion: v1\n"
            f"kind: {kind}\n"
            "metadata:\n"
            f"  name: {name}\n"
            f"  namespace: {namespace}\n"
            "stringData:\n"
            f"{string_data}"
        )

    def test_gpu_aggregate_contains_no_secret_resource(self):
        """Reintroducing a deployable Secret into the aggregate must fail."""
        documents = yaml.safe_load_all((self.GPU_RUNNER_DIR / "00-all.yaml").read_text(encoding="utf-8"))

        self.assertNotIn("Secret", {document.get("kind") for document in documents if isinstance(document, dict)})

    def test_gpu_secret_example_is_not_a_deployable_secret(self):
        """Copy-pasting the tracked example must not create a Kubernetes Secret."""
        example_path = self.GPU_RUNNER_DIR / "40-secret.example.yaml"

        self.assertTrue(example_path.is_file(), "tracked GPU Secret schema example is missing")
        documents = yaml.safe_load_all(example_path.read_text(encoding="utf-8"))
        self.assertNotIn("Secret", {document.get("kind") for document in documents if isinstance(document, dict)})

    def test_gpu_deploy_exits_before_apply_when_live_secret_is_absent(self):
        """An absent local or cluster Secret must prevent workload deployment."""
        result, calls = self.run_gpu_deploy()

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any(call.startswith("apply ") for call in calls), calls)

    def test_gpu_deploy_rejects_secret_file_without_local_yaml_suffix(self):
        """A tracked-looking Secret filename must be rejected before kubectl runs."""
        with tempfile.TemporaryDirectory() as directory:
            secret_path = Path(directory) / "40-secret.yaml"
            secret_path.write_text("apiVersion: v1\nkind: Secret\n", encoding="utf-8")
            result, calls = self.run_gpu_deploy(secret_file=secret_path)

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(calls, [])

    def test_gpu_deploy_applies_explicit_local_secret_before_workload(self):
        """An explicit local Secret must be installed before dependent resources."""
        with tempfile.TemporaryDirectory() as directory:
            secret_path = Path(directory) / "gpu-runner-secret.local.yaml"
            secret_path.write_text(self.gpu_secret_manifest(), encoding="utf-8")
            result, calls = self.run_gpu_deploy(secret_file=secret_path)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls[0], f"apply -f {secret_path}")
        self.assertEqual(
            calls[1],
            "get secret glasslab-v2-runner -n glasslab-v2 "
            "-o jsonpath={.data.GLASSLAB_RUNNER_STORE_POSTGRES_DSN}",
        )
        self.assertEqual(calls[2], f"apply -f {self.GPU_RUNNER_DIR / '00-all.yaml'}")

    def test_gpu_deploy_accepts_preexisting_named_cluster_secret(self):
        """A pre-existing named cluster Secret satisfies the deployment precondition."""
        result, calls = self.run_gpu_deploy(cluster_secret_exists=True)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            calls[0],
            "get secret glasslab-v2-runner -n glasslab-v2 "
            "-o jsonpath={.data.GLASSLAB_RUNNER_STORE_POSTGRES_DSN}",
        )
        self.assertEqual(calls[1], f"apply -f {self.GPU_RUNNER_DIR / '00-all.yaml'}")

    def test_gpu_deploy_rejects_local_file_with_wrong_kind(self):
        """A non-Secret local resource must never be applied as deployment credentials."""
        with tempfile.TemporaryDirectory() as directory:
            secret_path = Path(directory) / "gpu-runner-secret.local.yaml"
            secret_path.write_text(self.gpu_secret_manifest(kind="ConfigMap"), encoding="utf-8")
            result, calls = self.run_gpu_deploy(secret_file=secret_path)

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(calls, [])

    def test_gpu_deploy_rejects_local_secret_with_wrong_name(self):
        """A local Secret for another workload must never satisfy the GPU runner gate."""
        with tempfile.TemporaryDirectory() as directory:
            secret_path = Path(directory) / "gpu-runner-secret.local.yaml"
            secret_path.write_text(self.gpu_secret_manifest(name="other-runner"), encoding="utf-8")
            result, calls = self.run_gpu_deploy(secret_file=secret_path)

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(calls, [])

    def test_gpu_deploy_rejects_local_secret_with_wrong_namespace(self):
        """A Secret in another namespace must never satisfy the GPU runner gate."""
        with tempfile.TemporaryDirectory() as directory:
            secret_path = Path(directory) / "gpu-runner-secret.local.yaml"
            secret_path.write_text(self.gpu_secret_manifest(namespace="other-namespace"), encoding="utf-8")
            result, calls = self.run_gpu_deploy(secret_file=secret_path)

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(calls, [])

    def test_gpu_deploy_rejects_local_secret_without_required_key(self):
        """A Secret missing the runner DSN key must never be applied."""
        with tempfile.TemporaryDirectory() as directory:
            secret_path = Path(directory) / "gpu-runner-secret.local.yaml"
            secret_path.write_text(self.gpu_secret_manifest(include_key=False), encoding="utf-8")
            result, calls = self.run_gpu_deploy(secret_file=secret_path)

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(calls, [])

    def test_gpu_deploy_stops_when_apply_does_not_create_exact_live_secret_key(self):
        """A successful kubectl apply without the exact live Secret/key must block the workload."""
        with tempfile.TemporaryDirectory() as directory:
            secret_path = Path(directory) / "gpu-runner-secret.local.yaml"
            secret_path.write_text(self.gpu_secret_manifest(), encoding="utf-8")
            result, calls = self.run_gpu_deploy(
                secret_file=secret_path,
                apply_creates_live_secret=True,
                applied_secret_has_key=False,
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(calls[0], f"apply -f {secret_path}")
        self.assertEqual(
            calls[1],
            "get secret glasslab-v2-runner -n glasslab-v2 "
            "-o jsonpath={.data.GLASSLAB_RUNNER_STORE_POSTGRES_DSN}",
        )
        self.assertFalse(any(call.endswith("/00-all.yaml") for call in calls), calls)

    def test_gpu_deploy_rejects_preexisting_secret_without_required_key(self):
        """An existing named Secret without the DSN key must block deployment."""
        result, calls = self.run_gpu_deploy(cluster_secret_exists=True, cluster_secret_has_key=False)

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any(call.startswith("apply ") for call in calls), calls)

    def test_gpu_workloads_require_the_runner_secret_key(self):
        """The pod must not start when the mandatory runner DSN key is absent."""
        for manifest_name in ("00-all.yaml", "10-deployment.yaml"):
            with self.subTest(manifest=manifest_name):
                documents = yaml.safe_load_all((self.GPU_RUNNER_DIR / manifest_name).read_text(encoding="utf-8"))
                deployment = next(document for document in documents if document.get("kind") == "Deployment")
                environment = deployment["spec"]["template"]["spec"]["containers"][0]["env"]
                dsn_variable = next(
                    variable for variable in environment if variable["name"] == "GLASSLAB_RUNNER_STORE_POSTGRES_DSN"
                )

                self.assertIs(dsn_variable["valueFrom"]["secretKeyRef"].get("optional", False), False)

    def test_pxe_profiles_lock_passwords_and_retain_key_only_ssh(self):
        """Removing password verifiers must never remove the provisioner key or SSH hardening."""
        for profile in self.PXE_PROFILES:
            with self.subTest(profile=profile):
                document = yaml.safe_load((self.PXE_ROOT / profile / "user-data").read_text(encoding="utf-8"))
                autoinstall = document["autoinstall"]
                identity = autoinstall["identity"]
                ssh = autoinstall["ssh"]
                user_data = autoinstall["user-data"]
                authorized_keys = ssh["authorized-keys"]
                hardening = "\n".join(
                    item.get("content", "") for item in user_data["write_files"] if isinstance(item, dict)
                )

                self.assertEqual(identity["password"], "!")
                self.assertIs(ssh["allow-pw"], False)
                self.assertTrue(authorized_keys)
                self.assertTrue(all(isinstance(key, str) and key.strip() for key in authorized_keys))
                self.assertIs(user_data["ssh_pwauth"], False)
                self.assertIn("PasswordAuthentication no", hardening)
                self.assertIn("KbdInteractiveAuthentication no", hardening)
                self.assertIn("PubkeyAuthentication yes", hardening)


if __name__ == "__main__":
    unittest.main()
