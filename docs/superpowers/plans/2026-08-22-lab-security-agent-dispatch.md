# Lab Security Agent Dispatch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a credentialless, worktree-isolated dispatcher that runs bounded security discovery and repair assignments through OpenCode against Glasslab exo.

**Architecture:** A small Python module owns validation, worktree lifecycle, environment sanitization, OpenCode configuration, execution, and result validation; a thin repository script exposes it. Static methodology and JSON Schema files define the model contract, while unit and fake-process integration tests prove containment without consuming inference.

**Tech Stack:** Python 3 standard library, Git worktrees, OpenCode JSON configuration, JSON Schema documents, `unittest`, Bash syntax checks

**Spec:** `docs/superpowers/specs/2026-08-22-lab-security-agent-dispatch-design.md`

## Global Constraints

- Lab-agent output is untrusted until independently validated by the primary session.
- Workers receive no SSH agent, Kubernetes configuration, SOPS identity, secret files, GitHub credentials, or permission to push or merge.
- Discovery may edit only its disposable detached worktree; its changes are
  never integrated automatically.
- Repair is permitted only for one explicitly identified, already validated finding.
- No mode automatically commits, pushes, merges, deletes a dirty worktree, or copies a patch into the canonical checkout.
- OpenCode sharing, web access, task delegation, external-directory access, and automatic updates remain disabled.
- Normal automated tests must not call exo or consume model inference.
- The live smoke test remains separately invoked and starts with one discovery worker.

---

### Task 1: Security assignment and result contracts

**Files:**
- Create: `security/lab-agent/methodology.md`
- Create: `security/lab-agent/discovery-result.schema.json`
- Create: `security/lab-agent/repair-result.schema.json`
- Create: `tests/security/test_lab_security_agent.py`

**Interfaces:**
- Consumes: the evidence and trust requirements in the design spec.
- Produces: `load_contract(repo_root: Path, mode: str) -> tuple[str, dict[str, object]]` and stable result schemas consumed by later tasks.

- [ ] **Step 1: Write failing contract-loading tests**

```python
from pathlib import Path
import unittest

from scripts.lab_security_agent import DispatchError, load_contract


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
```

- [ ] **Step 2: Run the focused tests and verify the missing module failure**

Run: `python3 -m unittest tests.security.test_lab_security_agent.ContractTests -v`

Expected: FAIL because `scripts.lab_security_agent` does not exist.

- [ ] **Step 3: Add the methodology and schemas**

Write `security/lab-agent/methodology.md` with explicit sections for threat boundaries, credential exposure, injection and command execution, authorization, unsafe parsing, filesystem boundaries, CI and dependency exposure, log leakage, operational defaults, exploitability validation, and false-positive discipline. State that discovery never edits files and repair addresses only the supplied validated finding.

Define `discovery-result.schema.json` as Draft 2020-12 JSON Schema with required top-level keys `mode`, `base_commit`, `scope`, `inspected`, and `findings`; set `mode` to the constant `discover`, disallow additional properties, and require every finding field asserted by the test. Restrict severity to `critical`, `high`, `medium`, `low`, or `informational`; restrict confidence to `high`, `medium`, or `low`.

Define `repair-result.schema.json` with required keys `mode`, `finding_id`, `base_commit`, `diff_sha256`, `files_changed`, `commands_run`, `residual_risk`, and `unresolved_questions`; set `mode` to `repair` and disallow additional properties. Each command record requires `command` and integer `exit_status`.

- [ ] **Step 4: Implement the contract loader**

Create `scripts/lab_security_agent.py` initially with:

```python
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class DispatchError(RuntimeError):
    pass


def load_contract(repo_root: Path, mode: str) -> tuple[str, dict[str, Any]]:
    if mode not in {"discover", "repair"}:
        raise DispatchError(f"unsupported mode: {mode}")
    contract_root = repo_root / "security" / "lab-agent"
    methodology = (contract_root / "methodology.md").read_text()
    schema_names = {
        "discover": "discovery-result.schema.json",
        "repair": "repair-result.schema.json",
    }
    schema = json.loads((contract_root / schema_names[mode]).read_text())
    return methodology, schema
```

- [ ] **Step 5: Run tests and validate JSON syntax**

Run: `python3 -m unittest tests.security.test_lab_security_agent.ContractTests -v`

Expected: 3 tests pass.

Run: `python3 -m json.tool security/lab-agent/discovery-result.schema.json >/dev/null && python3 -m json.tool security/lab-agent/repair-result.schema.json >/dev/null`

Expected: exit status 0.

- [ ] **Step 6: Commit the contracts**

```bash
git add security/lab-agent scripts/lab_security_agent.py tests/security/test_lab_security_agent.py
git commit -m "Define lab security agent contracts"
```

---

### Task 2: Validated run configuration and sanitized environment

**Files:**
- Modify: `scripts/lab_security_agent.py`
- Modify: `tests/security/test_lab_security_agent.py`

**Interfaces:**
- Consumes: `load_contract(repo_root, mode)` from Task 1.
- Produces: immutable `DispatchConfig`, `parse_args(argv) -> DispatchConfig`, `build_worker_environment(config, runtime_root) -> dict[str, str]`, and `build_opencode_config(config) -> dict[str, object]`.

- [ ] **Step 1: Write failing validation and environment tests**

```python
import os
from tempfile import TemporaryDirectory
from unittest.mock import patch

from scripts.lab_security_agent import (
    DispatchConfig,
    build_opencode_config,
    build_worker_environment,
    parse_args,
)


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

    def test_discovery_permissions_allow_local_edits_but_deny_network(self) -> None:
        config = DispatchConfig.for_test(REPO_ROOT, mode="discover")
        generated = build_opencode_config(config)
        permissions = generated["permission"]
        self.assertEqual(permissions["edit"], "allow")
        self.assertEqual(permissions["bash"], "deny")
        self.assertEqual(permissions["webfetch"], "deny")
        self.assertEqual(generated["share"], "disabled")

    def test_repair_permissions_allow_local_edit_only(self) -> None:
        config = DispatchConfig.for_test(
            REPO_ROOT, mode="repair", finding_id="GLASS-SEC-001"
        )
        generated = build_opencode_config(config)
        self.assertEqual(generated["permission"]["edit"], "allow")
        self.assertEqual(generated["permission"]["external_directory"], "deny")
        self.assertEqual(generated["permission"]["webfetch"], "deny")
```

- [ ] **Step 2: Run tests and verify they fail on undefined interfaces**

Run: `python3 -m unittest tests.security.test_lab_security_agent.ConfigurationTests -v`

Expected: FAIL because `DispatchConfig` and configuration helpers are undefined.

- [ ] **Step 3: Implement validated configuration**

Add a frozen dataclass with exact fields:

```python
@dataclass(frozen=True)
class DispatchConfig:
    repo_root: Path
    mode: Literal["discover", "repair"]
    run_name: str
    assignment_path: Path
    base_revision: str = "HEAD"
    finding_id: str | None = None
    api_base: str = "http://192.168.1.17:52415"
    model: str = "mlx-community/Qwen3-Coder-Next-4bit"
    opencode_bin: str = "opencode"
    timeout_seconds: int = 1800

    @classmethod
    def for_test(
        cls,
        repo_root: Path,
        *,
        mode: Literal["discover", "repair"],
        finding_id: str | None = None,
    ) -> "DispatchConfig":
        return cls(
            repo_root=repo_root,
            mode=mode,
            run_name="test-run",
            assignment_path=repo_root / "assignment.md",
            finding_id=finding_id,
        )
```

Use `argparse` for `MODE RUN_NAME --assignment PATH`, plus optional `--base`, `--finding-id`, `--api-base`, `--model`, `--opencode-bin`, and `--timeout`. Accept run names matching `^[a-z0-9][a-z0-9-]{0,62}$`; require a regular, non-symlink assignment file; require `--finding-id` only in repair mode; reject it in discovery mode; require an HTTP or HTTPS API URL with no username, password, query, or fragment.

- [ ] **Step 4: Implement environment and OpenCode configuration builders**

`build_worker_environment` returns a new dictionary containing only `PATH`, `LANG`, `LC_ALL`, `HOME`, `XDG_CONFIG_HOME`, `XDG_DATA_HOME`, `XDG_STATE_HOME`, `XDG_CACHE_HOME`, and `NO_COLOR`. Use `/usr/local/bin:/usr/bin:/bin` as the fallback PATH and never copy the parent mapping wholesale.

`build_opencode_config` uses provider ID `exo`, package `@ai-sdk/openai-compatible`, and `${api_base}/v1` after removing a trailing slash. Both modes set `share: disabled`, `autoupdate: false`, `lsp: false`, deny `external_directory`, `task`, `skill`, `webfetch`, `websearch`, and `question`, allow edit/write/patch only inside the disposable worktree, and deny bash in the first release. The primary session runs tests independently.

- [ ] **Step 5: Run configuration tests**

Run: `python3 -m unittest tests.security.test_lab_security_agent.ConfigurationTests -v`

Expected: all configuration tests pass.

- [ ] **Step 6: Commit validated configuration**

```bash
git add scripts/lab_security_agent.py tests/security/test_lab_security_agent.py
git commit -m "Validate lab security agent configuration"
```

---

### Task 3: Worktree isolation and safe lifecycle

**Files:**
- Modify: `scripts/lab_security_agent.py`
- Modify: `tests/security/test_lab_security_agent.py`

**Interfaces:**
- Consumes: `DispatchConfig` from Task 2.
- Produces: `RunPaths`, `prepare_run(config) -> RunPaths`, `capture_worktree_diff(paths) -> str`, and `cleanup_run(config, paths, discard_changes=False) -> None`.

- [ ] **Step 1: Write failing worktree tests using temporary Git repositories**

```python
import subprocess

from scripts.lab_security_agent import (
    capture_worktree_diff,
    cleanup_run,
    prepare_run,
)


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

    def test_discovery_uses_detached_worktree_at_resolved_commit(self) -> None:
        with TemporaryDirectory() as raw:
            repo = self.make_repo(Path(raw))
            config = DispatchConfig.for_test(repo, mode="discover")
            paths = prepare_run(config)
            self.assertEqual(git(paths.worktree, "branch", "--show-current"), "")
            self.assertEqual(git(paths.worktree, "rev-parse", "HEAD"), paths.base_commit)

    def test_dirty_source_repo_is_rejected(self) -> None:
        with TemporaryDirectory() as raw:
            repo = self.make_repo(Path(raw))
            (repo / "tracked.txt").write_text("dirty\n")
            with self.assertRaisesRegex(DispatchError, "source repository is dirty"):
                prepare_run(DispatchConfig.for_test(repo, mode="discover"))

    def test_discovery_mutation_is_captured_and_cleanup_needs_confirmation(self) -> None:
        with TemporaryDirectory() as raw:
            repo = self.make_repo(Path(raw))
            config = DispatchConfig.for_test(repo, mode="discover")
            paths = prepare_run(config)
            (paths.worktree / "tracked.txt").write_text("changed\n")
            self.assertIn("changed", capture_worktree_diff(paths))
            with self.assertRaisesRegex(DispatchError, "refusing to remove dirty"):
                cleanup_run(config, paths)
            cleanup_run(config, paths, discard_changes=True)
```

- [ ] **Step 2: Run worktree tests and verify they fail**

Run: `python3 -m unittest tests.security.test_lab_security_agent.WorktreeTests -v`

Expected: FAIL because lifecycle interfaces are undefined.

- [ ] **Step 3: Implement worktree path validation and creation**

Add:

```python
@dataclass(frozen=True)
class RunPaths:
    run_root: Path
    worktree: Path
    runtime: Path
    result_json: Path
    summary_md: Path
    log: Path
    metadata: Path
    base_commit: str
    branch: str | None
```

`prepare_run` must use `git rev-parse --show-toplevel`, `git status --porcelain`, `git rev-parse --verify <base>^{commit}`, and `git check-ignore -q .lab-agents`. Refuse a source repository whose top level differs from `config.repo_root.resolve()`, a dirty source, an unignored `.lab-agents`, a pre-existing run directory, or a symlink in any run path component. Create discovery with `git worktree add --detach <path> <commit>`; create repair with `git worktree add -b lab-agent/<run-name> <path> <commit>`. Store canonical paths and the resolved base commit in `metadata.json`.

- [ ] **Step 4: Implement diff capture and explicit cleanup**

`capture_worktree_diff` returns `git diff --binary <base_commit>` plus a manifest and contents for bounded untracked files. `cleanup_run` reads and compares `metadata.json`, verifies the recorded worktree is beneath `<repo>/.lab-agents/<run-name>/`, refuses a changed worktree unless `discard_changes=True`, runs `git worktree remove --force -- <exact-path>` only after that explicit confirmation, and removes only the now-empty runtime/run directories. It never deletes a repair branch.

- [ ] **Step 5: Run worktree tests**

Run: `python3 -m unittest tests.security.test_lab_security_agent.WorktreeTests -v`

Expected: all worktree tests pass.

- [ ] **Step 6: Commit worktree lifecycle**

```bash
git add scripts/lab_security_agent.py tests/security/test_lab_security_agent.py
git commit -m "Isolate lab security agents in worktrees"
```

---

### Task 4: Assignment assembly, execution, and result validation

**Files:**
- Modify: `scripts/lab_security_agent.py`
- Modify: `tests/security/test_lab_security_agent.py`
- Create: `tests/security/fixtures/fake-opencode.py`

**Interfaces:**
- Consumes: `DispatchConfig`, `RunPaths`, schemas, methodology, environment builder, and OpenCode configuration from Tasks 1–3.
- Produces: `assemble_assignment(config, paths) -> str`, `run_worker(config, paths) -> CompletedProcess[str]`, `validate_result(config, paths) -> dict[str, object]`, and `dispatch(config) -> RunPaths`.

- [ ] **Step 1: Write failing assignment and fake-process integration tests**

```python
from scripts.lab_security_agent import assemble_assignment, dispatch, validate_result


class ExecutionTests(WorktreeTests):
    def test_assignment_contains_repo_rules_scope_and_exact_output_path(self) -> None:
        with TemporaryDirectory() as raw:
            repo = self.make_repo(Path(raw))
            (repo / "AGENTS.md").write_text("Do not expose secrets.\n")
            assignment = repo / "scope.md"
            assignment.write_text("Inspect scripts only.\n")
            git(repo, "add", ".")
            git(repo, "commit", "-m", "instructions")
            config = replace(
                DispatchConfig.for_test(repo, mode="discover"),
                assignment_path=assignment,
            )
            paths = prepare_run(config)
            prompt = assemble_assignment(config, paths)
            self.assertIn("Do not expose secrets.", prompt)
            self.assertIn("Inspect scripts only.", prompt)
            self.assertIn("Return the result as JSON", prompt)
            self.assertIn("disposable worktree", prompt)

    def test_fake_discovery_completes_with_valid_result_and_clean_worktree(self) -> None:
        with TemporaryDirectory() as raw:
            repo = self.make_repo(Path(raw))
            self.copy_contract_files(REPO_ROOT, repo)
            assignment = repo / "scope.md"
            assignment.write_text("Inspect tracked.txt.\n")
            git(repo, "add", ".")
            git(repo, "commit", "-m", "contracts")
            config = replace(
                DispatchConfig.for_test(repo, mode="discover"),
                assignment_path=assignment,
                opencode_bin=str(REPO_ROOT / "tests/security/fixtures/fake-opencode.py"),
            )
            with patch("scripts.lab_security_agent.check_exo_health"):
                paths = dispatch(config)
            result = validate_result(config, paths)
            self.assertEqual(result["mode"], "discover")
            self.assertEqual(result["findings"], [])
            self.assertEqual(capture_worktree_diff(paths), "")

    def test_malformed_result_fails(self) -> None:
        with TemporaryDirectory() as raw:
            repo = self.make_repo(Path(raw))
            config = DispatchConfig.for_test(repo, mode="discover")
            paths = prepare_run(config)
            paths.result_json.write_text('{"mode":"discover"}\n')
            with self.assertRaisesRegex(DispatchError, "result schema"):
                validate_result(config, paths)
```

- [ ] **Step 2: Run execution tests and verify missing-interface failures**

Run: `python3 -m unittest tests.security.test_lab_security_agent.ExecutionTests -v`

Expected: FAIL because execution interfaces and the fake executable are absent.

- [ ] **Step 3: Implement deterministic schema validation**

Avoid adding a package dependency. Implement a focused recursive validator for the schema features used here: object `required`, `properties`, `additionalProperties: false`, arrays and `items`, strings, integers, `enum`, and `const`. Error messages include a JSON path such as `$.findings[0].locations`. `validate_result` rejects symlinks, files outside the recorded runtime root, files larger than 1 MiB, malformed JSON, schema violations, mismatched mode/base commit/finding ID, and a repair `diff_sha256` that differs from the SHA-256 of `git diff --binary <base_commit>`.

- [ ] **Step 4: Implement assignment assembly and worker execution**

`assemble_assignment` concatenates clear delimiters around the committed `AGENTS.md`, methodology, caller assignment, schema, base commit, mode, and finding ID. It requires the final answer to contain exactly one fenced JSON result followed by a Markdown summary. Discovery text says: `You may experiment only inside this disposable worktree; do not commit.` Repair text says: `Change only files necessary to remediate finding <id>; do not commit.`

`run_worker` writes the OpenCode config beneath the isolated XDG config root, invokes `[opencode_bin, "run", "--format", "json", "-m", f"exo/{model}", prompt]` with `cwd=paths.worktree`, the sanitized environment, captured text output, and `timeout=config.timeout_seconds`. Parse the JSON Lines event stream, concatenate final assistant text events, extract exactly one fenced JSON document, validate it, and have the trusted dispatcher write `result.json` and `summary.md` beneath `paths.runtime`. Write stderr and event metadata—not hidden reasoning or raw secret-like content—to `paths.log` after replacing the API URL and worktree parent with stable labels. On timeout or nonzero exit, raise `DispatchError` and preserve the run.

`dispatch` checks `${api_base}/v1/models` with `urllib.request` before worktree creation, calls `prepare_run`, executes the worker, validates its result, captures the worktree diff and its digest, records final metadata, and prints no model response or result content to stdout.

- [ ] **Step 5: Add the fake OpenCode executable**

The fixture reads the final prompt argument, extracts the lines beginning `RESULT_JSON=` and `SUMMARY_MD=`, writes a valid empty discovery result using `BASE_COMMIT=`, and exits zero. An environment variable `FAKE_OPENCODE_MODE=malformed|mutate|fail|timeout` selects deterministic failure behavior. The fixture must never contact a network endpoint.

- [ ] **Step 6: Run execution tests**

Run: `python3 -m unittest tests.security.test_lab_security_agent.ExecutionTests -v`

Expected: all execution tests pass, including malformed output and disposable-worktree diff capture.

- [ ] **Step 7: Commit execution support**

```bash
git add scripts/lab_security_agent.py tests/security
git commit -m "Run and validate lab security agents"
```

---

### Task 5: Command wrapper, operator documentation, and pre-push coverage

**Files:**
- Create: `scripts/lab-security-agent`
- Create: `docs/security/lab-security-agents.md`
- Create: `security/lab-agent/assignments/first-secret-audit.md`
- Modify: `scripts/check-before-push.sh`
- Modify: `.gitignore`
- Modify: `tests/security/test_lab_security_agent.py`

**Interfaces:**
- Consumes: `main(argv: Sequence[str] | None = None) -> int` added to `scripts.lab_security_agent`.
- Produces: the supported operator command and explicit cleanup/live-smoke procedures.

- [ ] **Step 1: Write failing CLI tests**

```python
class CliTests(unittest.TestCase):
    def test_help_documents_both_modes_and_no_automatic_merge(self) -> None:
        completed = subprocess.run(
            [str(REPO_ROOT / "scripts/lab-security-agent"), "--help"],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self.assertEqual(completed.returncode, 0)
        self.assertIn("discover", completed.stdout)
        self.assertIn("repair", completed.stdout)
        self.assertIn("never commits, pushes, or merges", completed.stdout)

    def test_cleanup_refuses_unknown_run(self) -> None:
        completed = subprocess.run(
            [str(REPO_ROOT / "scripts/lab-security-agent"), "cleanup", "missing"],
            cwd=REPO_ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("recorded run", completed.stderr)
```

- [ ] **Step 2: Run CLI tests and verify the wrapper is missing**

Run: `python3 -m unittest tests.security.test_lab_security_agent.CliTests -v`

Expected: ERROR because `scripts/lab-security-agent` does not exist.

- [ ] **Step 3: Implement the thin wrapper and CLI entry point**

Create executable `scripts/lab-security-agent`:

```bash
#!/usr/bin/env bash
set -euo pipefail
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
export PYTHONPATH="$repo_root${PYTHONPATH:+:$PYTHONPATH}"
exec python3 -m scripts.lab_security_agent "$@"
```

Add `main()` with `discover`, `repair`, and `cleanup` subcommands. On success print only labeled paths and the resolved base commit. On failure print `lab-security-agent: <message>` to stderr and return 1. The help epilog states that workers are untrusted and the command never commits, pushes, or merges.

- [ ] **Step 4: Document the operator workflow and ignore run state**

Add `/.lab-agents/` to `.gitignore`. Document prerequisites, the trust model, one discovery example, validation steps, one repair example, explicit cleanup, artifact locations, exo overrides, the fact that prompts and reports must contain no plaintext secrets, and the opt-in live smoke sequence:

```bash
curl -fsS http://192.168.1.17:52415/v1/models | jq '.data[].id'
scripts/lab-security-agent discover secret-boundaries \
  --assignment security/lab-agent/assignments/secret-boundaries.md
git -C .lab-agents/secret-boundaries/worktree status --short
python3 -m json.tool .lab-agents/secret-boundaries/runtime/result.json
```

State that the primary reviewer reproduces evidence and classifies every finding as rejected, validated, or escalated before repair.

Add `security/lab-agent/assignments/first-secret-audit.md` with the exact narrow assignment shown in Task 6 Step 5 so the first run is reproducible and reviewable.

- [ ] **Step 5: Add fast checks**

Add `scripts/lab-security-agent` to `run_shell`. Add `tests.security.test_lab_security_agent` to `run_secret_boundary_tests`. Do not add the live smoke sequence to `check-before-push.sh`.

- [ ] **Step 6: Run CLI and complete security tests**

Run: `python3 -m unittest tests.security.test_lab_security_agent -v`

Expected: all tests pass.

Run: `bash -n scripts/lab-security-agent scripts/check-before-push.sh`

Expected: exit status 0.

Run: `python3 scripts/check-credential-hygiene.py .`

Expected: credential hygiene check passes.

- [ ] **Step 7: Commit the operator surface**

```bash
git add .gitignore scripts/lab-security-agent scripts/check-before-push.sh docs/security/lab-security-agents.md security/lab-agent/assignments/first-secret-audit.md tests/security/test_lab_security_agent.py
git commit -m "Add lab security agent operator workflow"
```

---

### Task 6: Independent verification and first controlled discovery

**Files:**
- Modify only if verification exposes a defect in files from Tasks 1–5.
- Create at runtime, ignored: `.lab-agents/first-secret-audit/`

**Interfaces:**
- Consumes: the complete dispatcher command.
- Produces: verified local test evidence and one untrusted discovery report for primary-session review.

- [ ] **Step 1: Run the focused deterministic suite**

Run: `python3 -m unittest tests.security.test_lab_security_agent -v`

Expected: all tests pass with no exo requests.

- [ ] **Step 2: Run repository checks**

Run: `./scripts/check-before-push.sh --default`

Expected: config, credential hygiene, secret boundary, docs, shell, Python, workflow-api, runner, and orchestrator checks pass.

- [ ] **Step 3: Inspect the implementation diff and credential boundary**

Run: `git diff HEAD~4..HEAD --check`

Expected: exit status 0.

Run: `git diff HEAD~4..HEAD -- scripts/lab_security_agent.py scripts/lab-security-agent security/lab-agent tests/security/test_lab_security_agent.py`

Expected: every subprocess receives the constructed environment, all Git targets derive from resolved recorded paths, no destructive broad path or inherited credential appears, and no automatic commit/push/merge exists.

- [ ] **Step 4: Perform the opt-in exo health check**

Run: `curl -fsS --max-time 10 http://192.168.1.17:52415/v1/models | python3 -m json.tool >/dev/null`

Expected: exit status 0. If unavailable, stop the live smoke test while retaining the passing deterministic suite.

- [ ] **Step 5: Run one narrow discovery worker**

Verify `security/lab-agent/assignments/first-secret-audit.md` contains:

```markdown
Inspect tests/security and scripts involved in SOPS backup, restore, and
credential hygiene. Identify candidate cases where plaintext secrets can be
persisted, exposed through process arguments or environment, restored outside
the intended boundary, or executed through an attacker-controlled program.
Do not inspect ignored secret values. You may edit only this disposable
worktree for experiments; do not commit any change.
```

Run:

```bash
scripts/lab-security-agent discover first-secret-audit \
  --base HEAD \
  --assignment security/lab-agent/assignments/first-secret-audit.md
```

Expected: the command reports the base commit and artifact paths, result JSON validates, and any discovery changes remain only in the disposable worktree.

- [ ] **Step 6: Independently validate the report**

For every candidate, open the cited lines, trace the claimed source to sink, run the recommended non-destructive reproduction where safe, and record one of `rejected`, `validated`, or `escalated` in the primary session notes. Do not invoke repair mode for rejected or unresolved candidates.

- [ ] **Step 7: Commit any verification fixes, then record readiness**

If verification required code changes, rerun Steps 1–3 and commit only those fixes:

```bash
git add scripts/lab_security_agent.py scripts/lab-security-agent security/lab-agent tests/security/test_lab_security_agent.py docs/security/lab-security-agents.md scripts/check-before-push.sh .gitignore
git commit -m "Harden lab security agent dispatcher"
```

Record the live smoke run path and counts of rejected, validated, and escalated candidates in the handoff without copying any secret-bearing report content.
