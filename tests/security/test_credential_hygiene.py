"""Behavioral tests for the non-revealing credential hygiene scanner."""

import base64
import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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

    def test_retains_nonrevealing_fingerprint_for_shared_legacy_credential(self):
        """Removing the exposed-value fingerprint would allow the known credential back."""
        scanner = load_scanner()

        self.assertIn(
            "cf70a192a840ad93e149a8897417a27cd2698dcc1f12d6108d0f4c2b53798d97",
            scanner.KNOWN_EXPOSED_VALUE_SHA256,
        )

    def test_detects_known_value_embedded_inside_documentation_text(self):
        """Punctuation or a user/value example must not bypass a fixed-length fingerprint."""
        scanner = load_scanner()
        sentinel = "window-fixture"
        sentinel_digest = __import__("hashlib").sha256(sentinel.encode()).hexdigest()
        with mock.patch.object(
            scanner,
            "KNOWN_EXPOSED_FIXED_LENGTH_SHA256",
            {len(sentinel): frozenset({sentinel_digest})},
            create=True,
        ):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "handoff.md").write_text(
                    f"legacy user/value: operator/{sentinel}; remove it\n",
                    encoding="utf-8",
                )
                findings = scanner.scan_tree(root)

        self.assertEqual({finding.rule_id for finding in findings}, {"known-exposed-value"})

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

    def test_scans_plaintext_dsn_in_string_data_even_under_ignored_local_path(self):
        """A gitignored local Secret forced into a commit must still expose its DSN finding."""
        self.assertEqual(
            self.rule_ids(
                {
                    ".gitignore": "kubeadm/gpu/40-secret.local.yaml\n",
                    "kubeadm/gpu/40-secret.local.yaml": "\n".join(
                        [
                            "apiVersion: v1",
                            "kind: Secret",
                            "stringData:",
                            "  DATABASE_DSN: postgresql://fixture:sentinel@db.invalid/app",
                        ]
                    ),
                }
            ),
            {"secret-stringdata-dsn"},
        )

    def test_scans_plaintext_credential_like_string_data(self):
        """A non-placeholder API key in deployable stringData is still plaintext."""
        self.assertEqual(
            self.rule_ids(
                {
                    "secret.yaml": "\n".join(
                        [
                            "apiVersion: v1",
                            "kind: Secret",
                            "stringData:",
                            "  VLLM_API_KEY: fixture-live-key",
                        ]
                    )
                }
            ),
            {"secret-stringdata-credential"},
        )

    def test_duplicate_kind_is_a_scan_error_instead_of_a_secret_bypass(self):
        """A duplicate kind key must not let an earlier non-Secret value win."""
        self.assertEqual(
            self.rule_ids(
                {
                    "secret.yaml": "\n".join(
                        [
                            "apiVersion: v1",
                            "kind: ConfigMap",
                            "kind: Secret",
                            "stringData:",
                            "  TOKEN: fixture-live-key",
                        ]
                    )
                }
            ),
            {"scan-error-duplicate-yaml-key"},
        )

    def test_yaml_parse_error_is_reported_and_cli_exits_nonzero(self):
        """Malformed YAML must never be treated as a clean credential scan."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "broken.yaml").write_text("kind: [Secret\n", encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(SCANNER_PATH), str(root)],
                check=False,
                capture_output=True,
                text=True,
            )

        self.assertEqual(result.returncode, 1)
        self.assertIn("broken.yaml", result.stdout)
        self.assertIn("scan-error-yaml", result.stdout)
        self.assertNotIn("kind: [Secret", result.stdout + result.stderr)

    def test_file_read_error_is_reported_without_relying_on_unprivileged_modes(self):
        """A deterministic read failure must not disappear when tests run as root."""
        scanner = load_scanner()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            blocked = root / "blocked.yaml"
            blocked.write_text("kind: Secret\n", encoding="utf-8")
            real_read_text = scanner.Path.read_text

            def controlled_read(path, *args, **kwargs):
                if path == blocked:
                    raise PermissionError("controlled read failure")
                return real_read_text(path, *args, **kwargs)

            with mock.patch.object(scanner.Path, "read_text", new=controlled_read):
                findings = scanner.scan_tree(root)

        self.assertEqual({finding.rule_id for finding in findings}, {"scan-error-file-read"})

    def test_file_stat_error_is_reported_without_relying_on_unprivileged_modes(self):
        """A deterministic stat failure must not silently omit a candidate file."""
        scanner = load_scanner()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            blocked = root / "blocked.yaml"
            blocked.write_text("kind: Secret\n", encoding="utf-8")
            real_is_symlink = scanner.Path.is_symlink

            def controlled_is_symlink(path):
                if path == blocked:
                    raise PermissionError("controlled stat failure")
                return real_is_symlink(path)

            with mock.patch.object(scanner.Path, "is_symlink", new=controlled_is_symlink):
                try:
                    findings = scanner.scan_tree(root)
                except PermissionError:
                    findings = []

        self.assertEqual({finding.rule_id for finding in findings}, {"scan-error-file-stat"})

    def test_traversal_error_is_reported_without_relying_on_unprivileged_modes(self):
        """An os.walk failure must create a scan issue instead of an empty result."""
        scanner = load_scanner()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def controlled_walk(path, *, topdown, followlinks, onerror=None):
                if onerror is not None:
                    onerror(PermissionError(13, "controlled traversal failure", str(path)))
                return []

            with mock.patch.object(scanner.os, "walk", side_effect=controlled_walk):
                findings = scanner.scan_tree(root)

        self.assertEqual({finding.rule_id for finding in findings}, {"scan-error-traversal"})

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
    AGENT_STACK_DEPLOY_SCRIPT = REPOSITORY_ROOT / "scripts" / "deploy-agent-stack.sh"
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
        deploy_script: Path | None = None,
        secret_file: Path | None = None,
        cluster_secret_exists: bool = False,
        cluster_secret_value: str | None = None,
        cluster_secret_values: dict[str, str] | None = None,
        applied_secret_has_key: bool = True,
        xtrace: bool = False,
        source_wrapper: bool = False,
    ) -> tuple[subprocess.CompletedProcess[str], list[str]]:
        with tempfile.TemporaryDirectory() as directory:
            fixture_root = Path(directory)
            bin_dir = fixture_root / "bin"
            bin_dir.mkdir()
            calls_path = fixture_root / "kubectl-calls"
            live_secret_dir = fixture_root / "live-secret"
            live_secret_dir.mkdir()
            if cluster_secret_exists:
                values = cluster_secret_values or {
                    "VLLM_API_KEY": cluster_secret_value if cluster_secret_value is not None else "fixture-live-key"
                }
                for key, value in values.items():
                    (live_secret_dir / key).write_text(encoded(value), encoding="utf-8")
            kubectl = bin_dir / "kubectl"
            kubectl.write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' \"$*\" >> \"$KUBECTL_CALLS\"\n"
                "if [[ \"${1-}\" == get && \"${2-}\" == secret ]]; then\n"
                "  [[ \"${3-}\" == glasslab-agent-secrets && \"${4-}\" == -n && \"${5-}\" == glasslab-agents ]] || exit 1\n"
                "  case \"${7-}\" in\n"
                "    'jsonpath={.data.VLLM_API_KEY}') key=VLLM_API_KEY ;;\n"
                "    'jsonpath={.data.HUGGING_FACE_HUB_TOKEN}') key=HUGGING_FACE_HUB_TOKEN ;;\n"
                "    'jsonpath={.data.GLASSLAB_AGENT_QWEN_API_KEY}') key=GLASSLAB_AGENT_QWEN_API_KEY ;;\n"
                "    *) exit 1 ;;\n"
                "  esac\n"
                "  [[ -f \"$FAKE_LIVE_SECRET_DIR/$key\" ]] || exit 1\n"
                "  cat \"$FAKE_LIVE_SECRET_DIR/$key\"\n"
                "  exit 0\n"
                "fi\n"
                "if [[ \"${1-}\" == apply && \"${FAKE_APPLIED_SECRET_HAS_KEY:-0}\" == 1 ]]; then\n"
                "  case \"${3-}\" in\n"
                "    *agent-secrets*)\n"
                "      printf 'Zml4dHVyZS1saXZlLWtleQ==' > \"$FAKE_LIVE_SECRET_DIR/VLLM_API_KEY\"\n"
                "      printf 'Zml4dHVyZS1odWYtdG9rZW4=' > \"$FAKE_LIVE_SECRET_DIR/HUGGING_FACE_HUB_TOKEN\"\n"
                "      printf 'Zml4dHVyZS1xd2VuLWtleQ==' > \"$FAKE_LIVE_SECRET_DIR/GLASSLAB_AGENT_QWEN_API_KEY\"\n"
                "      ;;\n"
                "  esac\n"
                "fi\n",
                encoding="utf-8",
            )
            kubectl.chmod(0o755)
            environment = os.environ.copy()
            environment["PATH"] = f"{bin_dir}:{environment['PATH']}"
            environment["KUBECTL_CALLS"] = str(calls_path)
            environment["FAKE_LIVE_SECRET_DIR"] = str(live_secret_dir)
            environment["FAKE_APPLIED_SECRET_HAS_KEY"] = "1" if applied_secret_has_key else "0"
            if secret_file is None:
                environment["GLASSLAB_VLLM_SECRET_FILE"] = str(fixture_root / "missing.local.yaml")
            else:
                environment["GLASSLAB_VLLM_SECRET_FILE"] = str(secret_file)

            script = str(deploy_script or self.VLLM_DEPLOY_SCRIPT)
            if source_wrapper:
                command = [
                    "bash",
                    "-x",
                    "-c",
                    'source "$1"; printf "trace-restored-marker\\n"',
                    "vllm-deploy-wrapper",
                    script,
                ]
            else:
                command = ["bash", *(["-x"] if xtrace else []), script]
            result = subprocess.run(
                command,
                cwd=REPOSITORY_ROOT,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )
            calls = calls_path.read_text(encoding="utf-8").splitlines() if calls_path.exists() else []
            return result, calls

    def run_vllm_test(
        self,
        api_key: str | None,
        *,
        xtrace: bool = False,
        source_wrapper: bool = False,
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

            if source_wrapper:
                command = [
                    "bash",
                    "-x",
                    "-c",
                    'source "$1"; printf "trace-restored-marker\\n"',
                    "vllm-test-wrapper",
                    str(self.VLLM_TEST_SCRIPT),
                ]
            else:
                command = [
                    "bash",
                    *(["-x"] if xtrace else []),
                    str(self.VLLM_TEST_SCRIPT),
                ]
            result = subprocess.run(
                command,
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

    @staticmethod
    def agent_secret_values() -> dict[str, str]:
        return {
            "VLLM_API_KEY": "fixture-vllm-key",
            "HUGGING_FACE_HUB_TOKEN": "fixture-hugging-face-token",
            "GLASSLAB_AGENT_QWEN_API_KEY": "fixture-qwen-key",
        }

    @classmethod
    def agent_secret_manifest(cls, values: dict[str, str] | None = None) -> str:
        values = values or cls.agent_secret_values()
        lines = [
            "apiVersion: v1",
            "kind: Secret",
            "metadata:",
            "  name: glasslab-agent-secrets",
            "  namespace: glasslab-agents",
            "stringData:",
        ]
        lines.extend(f"  {key}: {value}" for key, value in values.items())
        return "\n".join(lines) + "\n"

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

    def test_vllm_deploy_rejects_placeholder_in_preexisting_cluster_secret(self):
        """A non-empty but placeholder live Secret key must block deployment."""
        result, calls = self.run_vllm_deploy(
            cluster_secret_exists=True,
            cluster_secret_value="change-me",
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any(call.startswith("apply ") for call in calls), calls)
        self.assertNotIn("change-me", result.stdout + result.stderr)

    def test_vllm_deploy_disables_inherited_xtrace_before_secret_capture(self):
        """Cluster Secret bytes must stay out of a caller-enabled bash trace."""
        api_key = "vllm-deploy-xtrace-sentinel"
        result, calls = self.run_vllm_deploy(
            cluster_secret_exists=True,
            cluster_secret_value=api_key,
            xtrace=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(any(call.endswith("/11-vllm-deployment.yaml") for call in calls), calls)
        self.assertNotIn(api_key, result.stdout + result.stderr)
        self.assertNotIn(encoded(api_key), result.stdout + result.stderr)

    def test_vllm_deploy_restores_inherited_xtrace_after_secret_state_is_gone(self):
        """A sourced deploy helper must return the caller's trace state without leaking keys."""
        api_key = "vllm-deploy-restore-trace-sentinel"
        result, _ = self.run_vllm_deploy(
            cluster_secret_exists=True,
            cluster_secret_value=api_key,
            source_wrapper=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(api_key, result.stdout + result.stderr)
        self.assertNotIn(encoded(api_key), result.stdout + result.stderr)
        self.assertRegex(result.stderr, r"\+ printf ['\"]trace-restored-marker\\n['\"]")

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

    def test_vllm_test_rejects_explicit_placeholder_api_key(self):
        """An explicit placeholder key must stop before curl runs."""
        result, argv, _, _, _ = self.run_vllm_test("change-me")

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(argv, "")
        self.assertNotIn("change-me", result.stdout + result.stderr)

    def test_vllm_test_keeps_api_key_out_of_argv_and_removes_private_config(self):
        """A real key must reach curl only through a temporary private config file."""
        api_key = "vllm-fixture-secret"
        result, argv, config_copy, config_modes, removed_paths = self.run_vllm_test(api_key)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(api_key, argv)
        self.assertIn(f"Authorization: Bearer {api_key}", config_copy)
        self.assertEqual(config_modes, ["600", "600"])
        self.assertEqual(len(removed_paths), 2)

    def test_vllm_test_disables_inherited_xtrace_until_key_and_config_cleanup(self):
        """The smoke-test key and private curl config must not enter bash -x output."""
        api_key = "vllm-test-xtrace-sentinel"
        result, argv, _, _, removed_paths = self.run_vllm_test(
            api_key,
            source_wrapper=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(api_key, result.stdout + result.stderr + argv)
        self.assertEqual(len(removed_paths), 2)
        self.assertRegex(result.stderr, r"\+ printf ['\"]trace-restored-marker\\n['\"]")

    def test_agent_stack_deploy_exits_before_apply_when_secret_is_absent(self):
        """Missing agent credentials must prevent every stack mutation."""
        result, calls = self.run_vllm_deploy(deploy_script=self.AGENT_STACK_DEPLOY_SCRIPT)

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any(call.startswith("apply ") for call in calls), calls)

    def test_agent_stack_deploy_accepts_preexisting_named_cluster_secret(self):
        """The full stack must remain deployable with the exact live Secret/key."""
        result, calls = self.run_vllm_deploy(
            deploy_script=self.AGENT_STACK_DEPLOY_SCRIPT,
            cluster_secret_exists=True,
            cluster_secret_values=self.agent_secret_values(),
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            calls[0],
            "get secret glasslab-agent-secrets -n glasslab-agents -o jsonpath={.data.VLLM_API_KEY}",
        )
        self.assertTrue(any(call.endswith("/21-agent-api-deployment.yaml") for call in calls), calls)

    def test_agent_stack_deploy_accepts_explicit_live_secret(self):
        """The full stack must retain the explicit local live Secret path."""
        with tempfile.TemporaryDirectory() as directory:
            secret_path = Path(directory) / "agent-secrets.local.yaml"
            secret_path.write_text(self.agent_secret_manifest(), encoding="utf-8")
            result, calls = self.run_vllm_deploy(
                deploy_script=self.AGENT_STACK_DEPLOY_SCRIPT,
                secret_file=secret_path,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"apply -f {secret_path}", calls)
        self.assertTrue(any(call.endswith("/21-agent-api-deployment.yaml") for call in calls), calls)

    def test_agent_stack_rejects_local_secret_missing_each_required_key_before_apply(self):
        """Every full-stack local Secret key is mandatory before any mutation."""
        for missing_key in self.agent_secret_values():
            with self.subTest(missing_key=missing_key), tempfile.TemporaryDirectory() as directory:
                values = self.agent_secret_values()
                del values[missing_key]
                secret_path = Path(directory) / "agent-secrets.local.yaml"
                secret_path.write_text(self.agent_secret_manifest(values), encoding="utf-8")
                result, calls = self.run_vllm_deploy(
                    deploy_script=self.AGENT_STACK_DEPLOY_SCRIPT,
                    secret_file=secret_path,
                )

                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(any(call.startswith("apply ") for call in calls), calls)

    def test_agent_stack_rejects_preexisting_secret_missing_each_required_key_before_apply(self):
        """Every full-stack cluster Secret key is mandatory before any mutation."""
        for missing_key in self.agent_secret_values():
            with self.subTest(missing_key=missing_key):
                values = self.agent_secret_values()
                del values[missing_key]
                result, calls = self.run_vllm_deploy(
                    deploy_script=self.AGENT_STACK_DEPLOY_SCRIPT,
                    cluster_secret_exists=True,
                    cluster_secret_values=values,
                )

                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(any(call.startswith("apply ") for call in calls), calls)

    def test_agent_stack_rejects_placeholder_local_qwen_key_before_apply(self):
        """A local QWEN placeholder must not reach full-stack deployment."""
        with tempfile.TemporaryDirectory() as directory:
            values = self.agent_secret_values()
            values["GLASSLAB_AGENT_QWEN_API_KEY"] = "change-me"
            secret_path = Path(directory) / "agent-secrets.local.yaml"
            secret_path.write_text(self.agent_secret_manifest(values), encoding="utf-8")
            result, calls = self.run_vllm_deploy(
                deploy_script=self.AGENT_STACK_DEPLOY_SCRIPT,
                secret_file=secret_path,
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any(call.startswith("apply ") for call in calls), calls)
        self.assertNotIn("change-me", result.stdout + result.stderr)

    def test_agent_stack_rejects_placeholder_cluster_qwen_key_before_apply(self):
        """A cluster QWEN placeholder must not reach full-stack deployment."""
        values = self.agent_secret_values()
        values["GLASSLAB_AGENT_QWEN_API_KEY"] = "change-me"
        result, calls = self.run_vllm_deploy(
            deploy_script=self.AGENT_STACK_DEPLOY_SCRIPT,
            cluster_secret_exists=True,
            cluster_secret_values=values,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any(call.startswith("apply ") for call in calls), calls)
        self.assertNotIn("change-me", result.stdout + result.stderr)

    def run_gpu_deploy(
        self,
        *,
        secret_file: Path | None = None,
        cluster_secret_exists: bool = False,
        cluster_secret_has_key: bool = True,
        cluster_secret_value: str = "postgresql://fixture:live@db.invalid/app",
        apply_creates_live_secret: bool = True,
        applied_secret_has_key: bool = True,
        applied_secret_value: str = "postgresql://fixture:applied@db.invalid/app",
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
                    live_key_path.write_text(encoded(cluster_secret_value), encoding="utf-8")
            kubectl = bin_dir / "kubectl"
            kubectl.write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' \"$*\" >> \"$KUBECTL_CALLS\"\n"
                "if [[ \"${1-}\" == get && \"${2-}\" == secret ]]; then\n"
                "  [[ \"${3-}\" == glasslab-v2-runner && \"${4-}\" == -n && \"${5-}\" == glasslab-v2 ]] || exit 1\n"
                "  [[ -f \"$FAKE_LIVE_SECRET\" ]] || exit 1\n"
                "  if [[ -f \"$FAKE_LIVE_SECRET_KEY\" ]]; then cat \"$FAKE_LIVE_SECRET_KEY\"; fi\n"
                "  exit 0\n"
                "fi\n"
                "if [[ \"${1-}\" == apply && \"${3-}\" != *'/00-all.yaml' ]]; then\n"
                "  if [[ \"${FAKE_APPLY_CREATES_LIVE_SECRET:-0}\" == 1 ]]; then touch \"$FAKE_LIVE_SECRET\"; fi\n"
                "  if [[ \"${FAKE_APPLIED_SECRET_HAS_KEY:-0}\" == 1 ]]; then printf '%s' \"$FAKE_APPLIED_SECRET_VALUE\" > \"$FAKE_LIVE_SECRET_KEY\"; fi\n"
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
            environment["FAKE_APPLIED_SECRET_VALUE"] = encoded(applied_secret_value)
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
        value: str = "postgresql://fixture:local@db.invalid/app",
        section: str = "stringData",
    ) -> str:
        serialized_value = encoded(value) if section == "data" else value
        string_data = (
            f"  GLASSLAB_RUNNER_STORE_POSTGRES_DSN: {serialized_value}\n"
            if include_key
            else "  OTHER_KEY: fixture-value\n"
        )
        return (
            "apiVersion: v1\n"
            f"kind: {kind}\n"
            "metadata:\n"
            f"  name: {name}\n"
            f"  namespace: {namespace}\n"
            f"{section}:\n"
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

    def test_natural_gpu_secret_path_is_ignored_and_not_tracked(self):
        """The documented local Secret path must stay outside normal commits."""
        local_secret = "kubeadm/glasslab-v2/gpu-runner/40-secret.local.yaml"
        ignored = subprocess.run(
            ["git", "check-ignore", "--quiet", "--", local_secret],
            cwd=REPOSITORY_ROOT,
            check=False,
        )
        tracked = subprocess.run(
            ["git", "ls-files", "--error-unmatch", "--", local_secret],
            cwd=REPOSITORY_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(ignored.returncode, 0)
        self.assertNotEqual(tracked.returncode, 0)

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
            secret_path.write_text(self.gpu_secret_manifest(section="data"), encoding="utf-8")
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

    def test_gpu_deploy_rejects_placeholder_local_dsn_before_apply(self):
        """A nonempty local placeholder must not satisfy the DSN contract."""
        with tempfile.TemporaryDirectory() as directory:
            secret_path = Path(directory) / "gpu-runner-secret.local.yaml"
            secret_path.write_text(
                self.gpu_secret_manifest(value="change-me-postgres-dsn"),
                encoding="utf-8",
            )
            result, calls = self.run_gpu_deploy(secret_file=secret_path)

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(calls, [])
        self.assertNotIn("change-me-postgres-dsn", result.stdout + result.stderr)

    def test_gpu_deploy_rejects_non_postgresql_local_dsn_before_apply(self):
        """An arbitrary nonempty string is not a structurally valid Postgres DSN."""
        with tempfile.TemporaryDirectory() as directory:
            secret_path = Path(directory) / "gpu-runner-secret.local.yaml"
            secret_path.write_text(
                self.gpu_secret_manifest(value="https://fixture.invalid/not-postgres"),
                encoding="utf-8",
            )
            result, calls = self.run_gpu_deploy(secret_file=secret_path)

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(calls, [])

    def test_gpu_deploy_rejects_invalid_base64_local_dsn_before_apply(self):
        """The data form must be decoded and rejected when it is not valid base64."""
        with tempfile.TemporaryDirectory() as directory:
            secret_path = Path(directory) / "gpu-runner-secret.local.yaml"
            manifest = self.gpu_secret_manifest(section="data")
            manifest = manifest.replace(encoded("postgresql://fixture:local@db.invalid/app"), "not-base64!")
            secret_path.write_text(manifest, encoding="utf-8")
            result, calls = self.run_gpu_deploy(secret_file=secret_path)

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(calls, [])

    def test_gpu_deploy_rejects_placeholder_preexisting_live_dsn(self):
        """The cluster Secret path must decode and validate the same DSN contract."""
        sentinel = "change-me-live-dsn"
        result, calls = self.run_gpu_deploy(
            cluster_secret_exists=True,
            cluster_secret_value=sentinel,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any(call.startswith("apply ") for call in calls), calls)
        self.assertNotIn(sentinel, result.stdout + result.stderr)

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
