from __future__ import annotations

import os
import json
import shutil
import subprocess
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from scripts.lab_security_agent import (
    DispatchConfig,
    DispatchError,
    build_opencode_config,
    build_worker_environment,
    assemble_assignment,
    capture_worktree_diff,
    cleanup_run,
    dispatch,
    extract_final_answer,
    load_contract,
    parse_model_answer,
    parse_args,
    prepare_run,
    validate_schema,
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

    def test_missing_assignment_is_rejected(self) -> None:
        with self.assertRaisesRegex(DispatchError, "assignment"):
            parse_args(["discover", "scan", "--assignment", "/missing/scope.md"])

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
        self.assertEqual(permissions["*"], "allow")
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


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ).stdout.strip()


class WorktreeTests(unittest.TestCase):
    def make_repo(self, root: Path) -> Path:
        repo = root / "repo"
        repo.mkdir()
        git(repo, "init")
        git(repo, "config", "user.email", "test@example.invalid")
        git(repo, "config", "user.name", "Test")
        (repo / ".gitignore").write_text(".lab-agents/\n")
        (repo / "tracked.txt").write_text("safe\n")
        git(repo, "add", ".")
        git(repo, "commit", "-m", "base")
        return repo

    def config(self, repo: Path, mode: str = "discover") -> DispatchConfig:
        return replace(
            DispatchConfig.for_test(
                repo,
                mode=mode,  # type: ignore[arg-type]
                finding_id="SEC-1" if mode == "repair" else None,
            ),
            assignment_path=repo / "tracked.txt",
        )

    def test_discovery_uses_detached_worktree_at_resolved_commit(self) -> None:
        with TemporaryDirectory() as raw:
            repo = self.make_repo(Path(raw))
            paths = prepare_run(self.config(repo))
            self.assertEqual(git(paths.worktree, "branch", "--show-current"), "")
            self.assertEqual(git(paths.worktree, "rev-parse", "HEAD"), paths.base_commit)

    def test_dirty_source_repo_is_rejected(self) -> None:
        with TemporaryDirectory() as raw:
            repo = self.make_repo(Path(raw))
            (repo / "tracked.txt").write_text("dirty\n")
            with self.assertRaisesRegex(DispatchError, "source repository is dirty"):
                prepare_run(self.config(repo))

    def test_discovery_mutation_is_captured_and_cleanup_needs_confirmation(self) -> None:
        with TemporaryDirectory() as raw:
            repo = self.make_repo(Path(raw))
            config = self.config(repo)
            paths = prepare_run(config)
            (paths.worktree / "tracked.txt").write_text("changed\n")
            self.assertIn("changed", capture_worktree_diff(paths))
            with self.assertRaisesRegex(DispatchError, "refusing to remove dirty"):
                cleanup_run(config, paths)
            cleanup_run(config, paths, discard_changes=True)
            self.assertFalse(paths.worktree.exists())


class ExecutionContractTests(unittest.TestCase):
    def test_assignment_requires_fenced_json_and_disposable_boundary(self) -> None:
        with TemporaryDirectory() as raw:
            repo = WorktreeTests().make_repo(Path(raw))
            shutil.copytree(
                REPO_ROOT / "security" / "lab-agent",
                repo / "security" / "lab-agent",
            )
            (repo / "AGENTS.md").write_text("Do not expose secrets.\n")
            (repo / "scope.md").write_text("Inspect tracked.txt.\n")
            git(repo, "add", ".")
            git(repo, "commit", "-m", "instructions")
            config = replace(
                DispatchConfig.for_test(repo, mode="discover"),
                assignment_path=repo / "scope.md",
            )
            paths = prepare_run(config)
            prompt = assemble_assignment(config, paths)
            self.assertIn("Do not expose secrets.", prompt)
            self.assertIn("Inspect tracked.txt.", prompt)
            self.assertIn("exactly one fenced JSON", prompt)
            self.assertIn("disposable worktree", prompt)

    def test_extracts_final_text_from_json_events(self) -> None:
        events = "\n".join([
            '{"type":"step_start"}',
            '{"type":"text","part":{"text":"```json\\n{\\\"mode\\\":\\\"discover\\\"}\\n```\\nSummary"}}',
        ])
        self.assertIn('"mode":"discover"', extract_final_answer(events))

    def test_schema_validator_reports_nested_path(self) -> None:
        _, schema = load_contract(REPO_ROOT, "discover")
        invalid = {
            "mode": "discover", "base_commit": "abc", "scope": "x",
            "inspected": [], "findings": [{"id": "SEC-1"}],
        }
        with self.assertRaisesRegex(DispatchError, r"\$\.findings\[0\]"):
            validate_schema(invalid, schema)

    def test_model_answer_requires_exactly_one_json_fence(self) -> None:
        answer = '```json\n{"mode":"discover"}\n```\nSafe summary.'
        result, summary = parse_model_answer(answer)
        self.assertEqual(result["mode"], "discover")
        self.assertEqual(summary, "Safe summary.")
        with self.assertRaisesRegex(DispatchError, "exactly one fenced JSON"):
            parse_model_answer(answer + "\n```json\n{}\n```")

    def test_fake_discovery_writes_validated_runtime_artifacts(self) -> None:
        with TemporaryDirectory() as raw:
            repo = WorktreeTests().make_repo(Path(raw))
            shutil.copytree(
                REPO_ROOT / "security" / "lab-agent",
                repo / "security" / "lab-agent",
            )
            (repo / "AGENTS.md").write_text("Stay bounded.\n")
            (repo / "scope.md").write_text("Inspect tracked.txt.\n")
            git(repo, "add", ".")
            git(repo, "commit", "-m", "contracts")
            config = replace(
                DispatchConfig.for_test(repo, mode="discover"),
                assignment_path=repo / "scope.md",
                opencode_bin=str(REPO_ROOT / "tests/security/fixtures/fake-opencode.py"),
            )
            with patch("scripts.lab_security_agent.check_exo_health"):
                paths = dispatch(config)
            result = json.loads(paths.result_json.read_text())
            self.assertEqual(result["findings"], [])
            self.assertEqual(result["base_commit"], paths.base_commit)
            self.assertEqual(paths.summary_md.read_text(), "No candidate findings.\n")


class CliTests(unittest.TestCase):
    def test_help_documents_modes_and_no_automatic_merge(self) -> None:
        completed = subprocess.run(
            [str(REPO_ROOT / "scripts/lab-security-agent"), "--help"],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self.assertEqual(completed.returncode, 0)
        self.assertIn("discover", completed.stdout)
        self.assertIn("repair", completed.stdout)
        self.assertIn("never commits, pushes, or merges", completed.stdout)


if __name__ == "__main__":
    unittest.main()
