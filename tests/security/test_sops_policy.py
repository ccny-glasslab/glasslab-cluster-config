"""Policy checks for the committed SOPS enrollment.

A pending operator has created no key yet and is not a document recipient;
every active operator and the offline recovery key must be. The tooling fails
closed without a policy, so these tests activate as soon as it exists.
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
STATES = {"active", "pending"}
FINGERPRINT = re.compile(r"[0-9A-F]{40}")


@unittest.skipUnless(RECIPIENTS.is_file(), "SOPS enrollment is not committed yet")
class SopsPolicyTests(unittest.TestCase):
    def setUp(self):
        self.recipients = yaml.safe_load(RECIPIENTS.read_text(encoding="utf-8"))
        self.policy = yaml.safe_load(POLICY.read_text(encoding="utf-8"))

    def test_policy_file_is_committed(self):
        self.assertTrue(POLICY.is_file(),
                        ".sops.yaml is missing: check that the vault ignore rule negates it")

    def active_fingerprints(self) -> set[str]:
        return {item["fingerprint"] for item in self.recipients["operators"] if item["state"] == "active"}

    def test_the_three_named_operators_are_recorded(self):
        operators = self.recipients["operators"]
        self.assertEqual({item["username"] for item in operators}, OPERATORS)
        self.assertTrue({item["state"] for item in operators} <= STATES)

    def test_at_least_one_operator_is_active(self):
        self.assertTrue(self.active_fingerprints())

    def test_active_operators_have_real_fingerprints(self):
        for item in self.recipients["operators"]:
            if item["state"] != "active":
                continue
            self.assertRegex(item["fingerprint"], FINGERPRINT)

    def test_pending_operators_have_no_fingerprint(self):
        for item in self.recipients["operators"]:
            if item["state"] == "pending":
                self.assertIn(item.get("fingerprint"), (None, ""))

    def test_fingerprints_are_unique_and_not_placeholders(self):
        active = [item["fingerprint"] for item in self.recipients["operators"] if item["state"] == "active"]
        fingerprints = active + [self.recipients["recovery"]["fingerprint"]]
        for fingerprint in fingerprints:
            self.assertRegex(fingerprint, FINGERPRINT)
        self.assertEqual(len(set(fingerprints)), len(fingerprints))
        self.assertNotIn("0" * 40, fingerprints)

    def test_recovery_key_is_offline(self):
        self.assertEqual(self.recipients["recovery"]["storage"], "offline")

    def test_policy_lists_every_active_recipient(self):
        rules = self.policy["creation_rules"]
        matches = [rule for rule in rules if re.fullmatch(rule["path_regex"], "vault/example.sops.yaml")]
        self.assertEqual(len(matches), 1)
        expected = self.active_fingerprints() | {self.recipients["recovery"]["fingerprint"]}
        pgp = matches[0]["pgp"]
        listed = set(pgp.split(",")) if isinstance(pgp, str) else set(pgp)
        self.assertEqual({value.strip() for value in listed}, expected)

    def test_local_vault_material_is_ignored(self):
        ignored = GITIGNORE.read_text(encoding="utf-8")
        for entry in (".glasslab-secrets", "glasslab-secrets/", "*.sops.yaml"):
            self.assertIn(entry, ignored)


if __name__ == "__main__":
    unittest.main()
