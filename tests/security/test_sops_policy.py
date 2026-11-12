"""Policy checks that activate when SOPS enrollment is committed.

Until config/sops-recipients.yaml and .sops.yaml exist these tests skip: the
repository must not carry illustrative fingerprints, and the tooling fails
closed without a policy.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
RECIPIENTS = REPOSITORY_ROOT / "config" / "sops-recipients.yaml"
POLICY = REPOSITORY_ROOT / ".sops.yaml"
GITIGNORE = REPOSITORY_ROOT / ".gitignore"
OPERATORS = {"gr66ss-glasslab", "denic", "tristanc"}
FINGERPRINT = re.compile(r"[0-9A-F]{40}")


@unittest.skipUnless(RECIPIENTS.is_file() and POLICY.is_file(), "SOPS enrollment is not committed yet")
class SopsPolicyTests(unittest.TestCase):
    def setUp(self):
        self.recipients = yaml.safe_load(RECIPIENTS.read_text(encoding="utf-8"))
        self.policy = yaml.safe_load(POLICY.read_text(encoding="utf-8"))

    def test_three_active_operators(self):
        operators = self.recipients["operators"]
        self.assertEqual({item["username"] for item in operators}, OPERATORS)
        self.assertTrue(all(item["state"] == "active" for item in operators))

    def test_fingerprints_are_real_and_unique(self):
        items = list(self.recipients["operators"]) + [self.recipients["recovery"]]
        fingerprints = [item["fingerprint"] for item in items]
        for fingerprint in fingerprints:
            self.assertRegex(fingerprint, FINGERPRINT)
        self.assertEqual(len(set(fingerprints)), len(fingerprints))
        self.assertNotIn("0" * 40, fingerprints)

    def test_recovery_key_is_offline(self):
        self.assertEqual(self.recipients["recovery"]["storage"], "offline")

    def test_policy_lists_every_recipient_for_vault_documents(self):
        rules = self.policy["creation_rules"]
        matches = [rule for rule in rules if re.fullmatch(rule["path_regex"], "vault/example.sops.yaml")]
        self.assertEqual(len(matches), 1)
        expected = {item["fingerprint"] for item in self.recipients["operators"]}
        expected.add(self.recipients["recovery"]["fingerprint"])
        pgp = matches[0]["pgp"]
        listed = set(pgp.split(",")) if isinstance(pgp, str) else set(pgp)
        self.assertEqual({value.strip() for value in listed}, expected)

    def test_local_vault_material_is_ignored(self):
        ignored = GITIGNORE.read_text(encoding="utf-8")
        for entry in (".glasslab-secrets", "glasslab-secrets/", "*.sops.yaml"):
            self.assertIn(entry, ignored)


if __name__ == "__main__":
    unittest.main()
