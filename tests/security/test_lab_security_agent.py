from __future__ import annotations

import unittest
import os
from pathlib import Path
from unittest.mock import patch

from scripts.lab_security_agent import (
    DispatchConfig,
    DispatchError,
    build_opencode_config,
    build_worker_environment,
    load_contract,
    parse_args,
)


REPO_ROOT = Path(__file__).resolve().parents[2]


class ContractTests(unittest.TestCase):
    def test_discovery_contract_requires_evidence_fields(self) -> None:
        methodology, schema = load_contract(REPO_ROOT, "discover")
        self.assertIn("source-to-sink", methodology)
        finding = schema["properties"]["findings"]["items"]
        self.assertEqual(
            set(finding["required"]),
            {
                "id", "title", "severity", "confidence", "locations",
                "attacker_preconditions", "source_to_sink", "impact",
                "evidence", "recommended_validation", "remediation_outline",
            },
        )

    def test_repair_contract_requires_validated_finding_and_checks(self) -> None:
        methodology, schema = load_contract(REPO_ROOT, "repair")
        self.assertIn("validated finding", methodology.lower())
        self.assertIn("finding_id", schema["required"])
        self.assertIn("commands_run", schema["required"])

    def test_unknown_mode_fails_closed(self) -> None:
        with self.assertRaisesRegex(DispatchError, "unsupported mode"):
            load_contract(REPO_ROOT, "anything")


class ConfigurationTests(unittest.TestCase):
    def test_run_name_rejects_path_syntax(self) -> None:
        with self.assertRaisesRegex(DispatchError, "run name"):
            parse_args(["discover", "../escape", "--assignment", "scope.md"])

    def test_repair_requires_finding_id(self) -> None:
        with self.assertRaisesRegex(DispatchError, "finding id"):
            parse_args(["repair", "fix-one", "--assignment", "scope.md"])

    def test_environment_does_not_propagate_credentials(self) -> None:
        config = DispatchConfig.for_test(REPO_ROOT, mode="discover")
        with patch.dict(os.environ, {
            "SSH_AUTH_SOCK": "/tmp/agent.sock",
            "GITHUB_TOKEN": "secret",
            "SOPS_AGE_KEY": "secret",
            "KUBECONFIG": "/tmp/kubeconfig",
        }, clear=False):
            env = build_worker_environment(config, Path("/tmp/runtime"))
        self.assertNotIn("SSH_AUTH_SOCK", env)
        self.assertNotIn("GITHUB_TOKEN", env)
        self.assertNotIn("SOPS_AGE_KEY", env)
        self.assertNotIn("KUBECONFIG", env)
        self.assertEqual(env["HOME"], "/tmp/runtime/home")

    def test_discovery_allows_local_edits_but_denies_network(self) -> None:
        config = DispatchConfig.for_test(REPO_ROOT, mode="discover")
        generated = build_opencode_config(config)
        permissions = generated["permission"]
        self.assertEqual(permissions["edit"], "allow")
        self.assertEqual(permissions["bash"], "deny")
        self.assertEqual(permissions["webfetch"], "deny")
        self.assertEqual(generated["share"], "disabled")

    def test_repair_permissions_deny_external_access(self) -> None:
        config = DispatchConfig.for_test(
            REPO_ROOT, mode="repair", finding_id="GLASS-SEC-001"
        )
        generated = build_opencode_config(config)
        self.assertEqual(generated["permission"]["edit"], "allow")
        self.assertEqual(generated["permission"]["external_directory"], "deny")
        self.assertEqual(generated["permission"]["webfetch"], "deny")


if __name__ == "__main__":
    unittest.main()
