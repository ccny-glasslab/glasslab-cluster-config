"""Behavioral tests for the non-revealing credential hygiene scanner."""

# credential-hygiene: fixture

import base64
import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


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
                    "passwd: $6$rounds=4096$publicsalt$"
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
            self.rule_ids({"deploy.sh": "sshpass -p 'REDACTED' ssh host\n"}),
            {"sshpass-password"},
        )

    def test_detects_known_exposed_value_by_digest(self):
        """Known exposed values are detected by SHA-256 without outputting them."""
        self.assertEqual(
            self.rule_ids({"legacy.env": "LEGACY_VALUE=credential-hygiene-fixture-only\n"}),
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
                "deprecated-whatsapp/legacy.sh": "sshpass -p 'REDACTED' ssh host\n",
                "scan-artifacts/previous.txt": "sshpass -p 'REDACTED' ssh host\n",
            }
        )

        self.assertEqual(findings, [])

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


if __name__ == "__main__":
    unittest.main()
