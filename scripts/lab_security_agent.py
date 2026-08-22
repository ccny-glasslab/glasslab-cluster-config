from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Sequence
from urllib.parse import urlsplit


class DispatchError(RuntimeError):
    """A fail-closed dispatcher error safe to show to an operator."""


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


def parse_args(argv: Sequence[str]) -> DispatchConfig:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("mode", choices=("discover", "repair"))
    parser.add_argument("run_name")
    parser.add_argument("--assignment", required=True)
    parser.add_argument("--base", default="HEAD")
    parser.add_argument("--finding-id")
    parser.add_argument("--api-base", default="http://192.168.1.17:52415")
    parser.add_argument("--model", default="mlx-community/Qwen3-Coder-Next-4bit")
    parser.add_argument("--opencode-bin", default="opencode")
    parser.add_argument("--timeout", type=int, default=1800)
    try:
        args = parser.parse_args(list(argv))
    except SystemExit as exc:
        raise DispatchError("invalid arguments") from exc
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", args.run_name):
        raise DispatchError("invalid run name")
    if args.mode == "repair" and not args.finding_id:
        raise DispatchError("repair mode requires a finding id")
    if args.mode == "discover" and args.finding_id:
        raise DispatchError("discovery mode does not accept a finding id")
    parsed_url = urlsplit(args.api_base)
    if (
        parsed_url.scheme not in {"http", "https"}
        or not parsed_url.hostname
        or parsed_url.username
        or parsed_url.password
        or parsed_url.query
        or parsed_url.fragment
    ):
        raise DispatchError("invalid API base URL")
    assignment = Path(args.assignment)
    if assignment.exists() and (not assignment.is_file() or assignment.is_symlink()):
        raise DispatchError("assignment must be a regular non-symlink file")
    return DispatchConfig(
        repo_root=Path.cwd().resolve(),
        mode=args.mode,
        run_name=args.run_name,
        assignment_path=assignment.resolve(),
        base_revision=args.base,
        finding_id=args.finding_id,
        api_base=args.api_base.rstrip("/"),
        model=args.model,
        opencode_bin=args.opencode_bin,
        timeout_seconds=args.timeout,
    )


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


def _git(repo: Path, *args: str, check: bool = True) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if check and completed.returncode:
        raise DispatchError(completed.stderr.strip() or "git command failed")
    return completed.stdout.strip()


def prepare_run(config: DispatchConfig) -> RunPaths:
    repo = config.repo_root.resolve()
    if Path(_git(repo, "rev-parse", "--show-toplevel")).resolve() != repo:
        raise DispatchError("repo root does not match Git top level")
    if _git(repo, "status", "--porcelain", "--untracked-files=all"):
        raise DispatchError("source repository is dirty")
    base_commit = _git(repo, "rev-parse", "--verify", f"{config.base_revision}^{{commit}}")
    ignored = subprocess.run(
        ["git", "check-ignore", "-q", ".lab-agents/probe"], cwd=repo, check=False
    )
    if ignored.returncode != 0:
        raise DispatchError(".lab-agents must be ignored")
    run_root = repo / ".lab-agents" / config.run_name
    if run_root.exists() or run_root.is_symlink():
        raise DispatchError("run directory already exists")
    worktree = run_root / "worktree"
    runtime = run_root / "runtime"
    runtime.mkdir(parents=True)
    branch = None if config.mode == "discover" else f"lab-agent/{config.run_name}"
    if branch:
        _git(repo, "worktree", "add", "-b", branch, str(worktree), base_commit)
    else:
        _git(repo, "worktree", "add", "--detach", str(worktree), base_commit)
    paths = RunPaths(
        run_root=run_root,
        worktree=worktree,
        runtime=runtime,
        result_json=runtime / "result.json",
        summary_md=runtime / "summary.md",
        log=runtime / "opencode.log",
        metadata=runtime / "metadata.json",
        base_commit=base_commit,
        branch=branch,
    )
    paths.metadata.write_text(json.dumps({
        "repo_root": str(repo),
        "run_root": str(run_root.resolve()),
        "worktree": str(worktree.resolve()),
        "base_commit": base_commit,
        "branch": branch,
    }, indent=2, sort_keys=True) + "\n")
    return paths


def capture_worktree_diff(paths: RunPaths) -> str:
    tracked = _git(paths.worktree, "diff", "--binary", paths.base_commit)
    untracked = _git(
        paths.worktree, "ls-files", "--others", "--exclude-standard"
    ).splitlines()
    sections = [tracked] if tracked else []
    for relative in untracked:
        path = paths.worktree / relative
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 1_048_576:
            raise DispatchError(f"unsafe untracked file in worktree: {relative}")
        sections.append(f"--- untracked: {relative}\n{path.read_text(errors='replace')}")
    return "\n".join(sections)


def cleanup_run(
    config: DispatchConfig, paths: RunPaths, *, discard_changes: bool = False
) -> None:
    expected_parent = (config.repo_root / ".lab-agents" / config.run_name).resolve()
    if paths.run_root.resolve() != expected_parent:
        raise DispatchError("recorded run path is outside dispatcher root")
    metadata = json.loads(paths.metadata.read_text())
    if Path(metadata["worktree"]).resolve() != paths.worktree.resolve():
        raise DispatchError("recorded worktree does not match run metadata")
    changed = bool(_git(paths.worktree, "status", "--porcelain", "--untracked-files=all"))
    if changed and not discard_changes:
        raise DispatchError("refusing to remove dirty worktree without confirmation")
    args = ["worktree", "remove"]
    if changed:
        args.append("--force")
    args.append(str(paths.worktree))
    _git(config.repo_root, *args)
    shutil.rmtree(paths.run_root)


def build_worker_environment(
    config: DispatchConfig, runtime_root: Path
) -> dict[str, str]:
    del config
    return {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "LC_ALL": "C.UTF-8",
        "HOME": str(runtime_root / "home"),
        "XDG_CONFIG_HOME": str(runtime_root / "config"),
        "XDG_DATA_HOME": str(runtime_root / "data"),
        "XDG_STATE_HOME": str(runtime_root / "state"),
        "XDG_CACHE_HOME": str(runtime_root / "cache"),
        "NO_COLOR": "1",
    }


def build_opencode_config(config: DispatchConfig) -> dict[str, Any]:
    denied = {
        "external_directory": "deny",
        "task": "deny",
        "skill": "deny",
        "webfetch": "deny",
        "websearch": "deny",
        "question": "deny",
        "bash": "deny",
        "edit": "allow",
        "write": "allow",
        "patch": "allow",
    }
    return {
        "$schema": "https://opencode.ai/config.json",
        "model": f"exo/{config.model}",
        "small_model": f"exo/{config.model}",
        "default_agent": "build",
        "share": "disabled",
        "autoupdate": False,
        "lsp": False,
        "permission": denied,
        "agent": {"build": {"temperature": 0, "permission": denied}},
        "provider": {
            "exo": {
                "npm": "@ai-sdk/openai-compatible",
                "name": "Glasslab Exo",
                "options": {"baseURL": f"{config.api_base}/v1"},
                "models": {config.model: {"name": config.model}},
            }
        },
    }


def load_contract(repo_root: Path, mode: str) -> tuple[str, dict[str, Any]]:
    schema_names = {
        "discover": "discovery-result.schema.json",
        "repair": "repair-result.schema.json",
    }
    if mode not in schema_names:
        raise DispatchError(f"unsupported mode: {mode}")
    contract_root = repo_root / "security" / "lab-agent"
    methodology = (contract_root / "methodology.md").read_text()
    schema = json.loads((contract_root / schema_names[mode]).read_text())
    return methodology, schema
