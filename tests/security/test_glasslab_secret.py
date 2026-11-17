"""Behavioral tests for the SOPS operator boundary (scripts/glasslab-secret)."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPOSITORY_ROOT / "scripts" / "glasslab_secret.py"
SHELL_ENTRYPOINT = REPOSITORY_ROOT / "scripts" / "glasslab-secret"

SECRET_DOCUMENT = """apiVersion: v1
kind: Secret
metadata:
  name: workflow-api
  namespace: glasslab-v2
type: Opaque
stringData:
  TOKEN: super-secret-value
  OTHER: second-value
"""


def load_module():
    spec = importlib.util.spec_from_file_location("glasslab_secret", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def write_tool(directory: Path, name: str, body: str) -> None:
    path = directory / name
    path.write_text("#!/usr/bin/env bash\nset -euo pipefail\n" + body, encoding="utf-8")
    path.chmod(0o755)


class VaultFixture:
    def __init__(self, base: Path, inventory: str | None = None) -> None:
        self.vault = base / "vault"
        self.vault.mkdir(mode=0o700)
        self.vault.chmod(0o700)
        default = """version: 1
secrets:
  - name: workflow-api
    relative_path: kubeadm/glasslab-v2/workflow-api.sops.yaml
    target: glasslab-v2/glasslab-workflow-api
    owner: platform
"""
        (self.vault / "inventory.yaml").write_text(inventory or default, encoding="utf-8")
        (self.vault / "inventory.yaml").chmod(0o600)
        document = self.vault / "kubeadm" / "glasslab-v2" / "workflow-api.sops.yaml"
        document.parent.mkdir(parents=True)
        document.write_text("apiVersion: v1\nkind: Secret\n", encoding="utf-8")
        document.chmod(0o600)
        self.document = document


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.module = load_module()
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def load(self, inventory: str):
        fixture = VaultFixture(self.base, inventory)
        return self.module.load_inventory(fixture.vault / "inventory.yaml")

    def test_accepts_the_documented_inventory(self):
        records = self.load("""version: 1
secrets:
  - name: workflow-api
    relative_path: kubeadm/glasslab-v2/workflow-api.sops.yaml
    target: glasslab-v2/glasslab-workflow-api
    owner: platform
""")
        self.assertEqual([record.name for record in records], ["workflow-api"])

    def test_rejects_traversal(self):
        with self.assertRaises(self.module.SecretError):
            self.load("""version: 1
secrets:
  - name: escape
    relative_path: ../outside.sops.yaml
    target: glasslab-v2/escape
    owner: platform
""")

    def test_rejects_duplicate_name_or_path(self):
        entry = """version: 1
secrets:
  - name: one
    relative_path: a.sops.yaml
    target: glasslab-v2/one
    owner: platform
  - name: one
    relative_path: b.sops.yaml
    target: glasslab-v2/two
    owner: platform
"""
        with self.assertRaises(self.module.SecretError):
            self.load(entry)

    def test_rejects_a_document_without_the_sops_suffix(self):
        with self.assertRaises(self.module.SecretError):
            self.load("""version: 1
secrets:
  - name: plain
    relative_path: plain.yaml
    target: glasslab-v2/plain
    owner: platform
""")

    def test_rejects_an_unknown_field(self):
        with self.assertRaises(self.module.SecretError):
            self.load("""version: 1
secrets:
  - name: one
    relative_path: a.sops.yaml
    target: glasslab-v2/one
    owner: platform
    value: leak
""")

    def test_resolve_rejects_a_symlink_escape(self):
        fixture = VaultFixture(self.base)
        outside = self.base / "outside.sops.yaml"
        outside.write_text("data: {}\n", encoding="utf-8")
        link = fixture.vault / "escape.sops.yaml"
        link.symlink_to(outside)
        (fixture.vault / "inventory.yaml").write_text("""version: 1
secrets:
  - name: escape
    relative_path: escape.sops.yaml
    target: glasslab-v2/escape
    owner: platform
""", encoding="utf-8")
        os.chmod(fixture.vault / "inventory.yaml", 0o600)
        with mock.patch.dict(os.environ, {"GLASSLAB_SECRET_VAULT": str(fixture.vault)}):
            with self.assertRaises(self.module.SecretError):
                self.module.resolve_secret("escape")


class ToolTests(unittest.TestCase):
    def setUp(self):
        self.module = load_module()
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.fixture = VaultFixture(self.base)
        self.bin = self.base / "bin"
        self.bin.mkdir()
        self.recorded_argv = self.base / "argv.txt"
        self.sops_argv = self.base / "sops-argv.txt"
        self.recorded_stdin = self.base / "stdin.txt"
        self.secret_doc = self.base / "document.yaml"
        self.secret_doc.write_text(SECRET_DOCUMENT, encoding="utf-8")
        write_tool(self.bin, "sops", '''printf "%s\\n" "$*" >> "$FAKE_SOPS_ARGV"
case "${1:-}" in
  --version) echo "sops 3.13.3 (latest)";;
  --decrypt) cat "$FAKE_SECRET_DOC";;
  --encrypt) printf 'apiVersion: v1\\nkind: Secret\\ndata:\\n  TOKEN: ENC[AES256_GCM,data:abc]\\n';;
  updatekeys) echo "Updated";;
  *) echo "unsupported $*" >&2; exit 1;;
esac
''')
        write_tool(self.bin, "kubectl", 'printf "%s\\n" "$*" > "$FAKE_KUBECTL_ARGV"\ncat > "$FAKE_KUBECTL_STDIN"\n')
        self.policy = self.base / ".sops.yaml"
        self.policy.write_text("creation_rules: []\n", encoding="utf-8")
        self.environment = {
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "GLASSLAB_SECRET_VAULT": str(self.fixture.vault),
            "SOPS_CONFIG": str(self.policy),
            "FAKE_SECRET_DOC": str(self.secret_doc),
            "FAKE_KUBECTL_ARGV": str(self.recorded_argv),
            "FAKE_KUBECTL_STDIN": str(self.recorded_stdin),
            "FAKE_SOPS_ARGV": str(self.sops_argv),
            "XDG_RUNTIME_DIR": str(self.base),
        }

    def tearDown(self):
        self.temp.cleanup()

    def run_cli(self, argv):
        output, errors = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, self.environment), contextlib.redirect_stdout(output), \
                contextlib.redirect_stderr(errors):
            code = self.module.main(argv)
        return code, output.getvalue(), errors.getvalue()

    def test_doctor_accepts_a_valid_vault(self):
        code, output, errors = self.run_cli(["doctor"])
        self.assertEqual(code, 0, errors)
        self.assertIn("doctor: ok", output)

    def test_doctor_rejects_a_group_readable_vault(self):
        self.fixture.vault.chmod(0o750)
        code, output, errors = self.run_cli(["doctor"])
        self.assertEqual(code, 1)
        self.assertIn("FAIL", errors)

    def test_apply_keeps_values_out_of_argv_and_uses_stdin(self):
        code, _, _ = self.run_cli(["apply", "workflow-api"])
        self.assertEqual(code, 0)
        argv = self.recorded_argv.read_text(encoding="utf-8")
        self.assertIn("apply -f -", argv)
        self.assertNotIn("super-secret-value", argv)
        self.assertIn("super-secret-value", self.recorded_stdin.read_text(encoding="utf-8"))

    def test_apply_cleans_up_after_a_failure(self):
        write_tool(self.bin, "sops", 'if [[ "${1:-}" == "--decrypt" ]]; then echo "decrypt failed" >&2; exit 1; fi\nexit 0\n')
        code, _, _ = self.run_cli(["apply", "workflow-api"])
        self.assertNotEqual(code, 0)
        leftovers = [path for path in self.base.iterdir() if path.name.startswith("glasslab-secret-")]
        self.assertEqual(leftovers, [])

    def test_get_prints_only_the_requested_key(self):
        code, output, errors = self.run_cli(["get", "workflow-api", "TOKEN"])
        self.assertEqual(code, 0, errors)
        self.assertEqual(output.strip(), "super-secret-value")

    def test_exec_env_exposes_only_requested_keys(self):
        target = self.base / "child-env.json"
        code, output, errors = self.run_cli([
            "exec-env", "workflow-api", "TOKEN", "--",
            sys.executable, "-c",
            f"import json, os; open({str(target)!r}, 'w').write(json.dumps(dict(os.environ)))",
        ])
        self.assertEqual(code, 0, errors)
        child = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(child.get("TOKEN"), "super-secret-value")
        self.assertNotIn("OTHER", child)

    def test_shell_tracing_is_refused(self):
        with mock.patch.dict(os.environ, {**self.environment, "SHELLOPTS": "braceexpand:xtrace"}):
            self.assertEqual(self.module.main(["list"]), 2)

    def test_create_writes_encrypted_ciphertext_with_private_mode(self):
        plaintext = self.base / "plain.yaml"
        plaintext.write_text(SECRET_DOCUMENT, encoding="utf-8")
        code, output, errors = self.run_cli(["create", "workflow-api", "--from", str(plaintext), "--force"])
        self.assertEqual(code, 0, errors)
        written = self.fixture.document.read_text(encoding="utf-8")
        self.assertIn("ENC[", written)
        self.assertNotIn("super-secret-value", written)
        self.assertEqual(stat.S_IMODE(self.fixture.document.stat().st_mode), 0o600)

    def test_create_passes_the_repository_policy_to_sops(self):
        plaintext = self.base / "plain.yaml"
        plaintext.write_text(SECRET_DOCUMENT, encoding="utf-8")
        code, _, errors = self.run_cli(["create", "workflow-api", "--from", str(plaintext), "--force"])
        self.assertEqual(code, 0, errors)
        recorded = self.sops_argv.read_text(encoding="utf-8")
        self.assertIn("--config", recorded)
        self.assertIn(str(self.policy), recorded)
        self.assertIn("plaintext.yaml", recorded, "sops needs a real file, not stdin")
        leftovers = [path for path in self.base.iterdir() if path.name.startswith("glasslab-secret-")]
        self.assertEqual(leftovers, [])

    def test_create_makes_a_document_that_does_not_exist_yet(self):
        plaintext = self.base / "plain.yaml"
        plaintext.write_text(SECRET_DOCUMENT, encoding="utf-8")
        self.fixture.document.unlink()
        code, _, errors = self.run_cli(["create", "workflow-api", "--from", str(plaintext)])
        self.assertEqual(code, 0, errors)
        self.assertTrue(self.fixture.document.is_file())
        self.assertIn("ENC[", self.fixture.document.read_text(encoding="utf-8"))

    def test_create_requires_a_policy(self):
        plaintext = self.base / "plain.yaml"
        plaintext.write_text(SECRET_DOCUMENT, encoding="utf-8")
        self.policy.unlink()
        before = self.fixture.document.read_text(encoding="utf-8")
        code, _, errors = self.run_cli(["create", "workflow-api", "--from", str(plaintext), "--force"])
        self.assertEqual(code, 2)
        self.assertIn("policy is missing", errors)
        self.assertEqual(self.fixture.document.read_text(encoding="utf-8"), before)

    def test_create_refuses_unencrypted_output(self):
        plaintext = self.base / "plain.yaml"
        plaintext.write_text(SECRET_DOCUMENT, encoding="utf-8")
        write_tool(self.bin, "sops", 'printf "apiVersion: v1\\nkind: Secret\\nstringData:\\n  TOKEN: leak\\n"\n')
        before = self.fixture.document.read_text(encoding="utf-8")
        code, _, _ = self.run_cli(["create", "workflow-api", "--from", str(plaintext), "--force"])
        self.assertEqual(code, 2)
        self.assertEqual(self.fixture.document.read_text(encoding="utf-8"), before)

    def test_shell_entrypoint_is_the_thin_wrapper(self):
        source = SHELL_ENTRYPOINT.read_text(encoding="utf-8").splitlines()
        self.assertEqual(source[0], "#!/usr/bin/env bash")
        self.assertIn("exec python3", source[-1])
        self.assertLessEqual(len(source), 4)


if __name__ == "__main__":
    unittest.main()
