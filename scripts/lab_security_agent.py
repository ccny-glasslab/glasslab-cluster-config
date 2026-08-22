from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Sequence
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


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
    if not assignment.is_file() or assignment.is_symlink():
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


def assemble_assignment(config: DispatchConfig, paths: RunPaths) -> str:
    methodology, schema = load_contract(config.repo_root, config.mode)
    agents_path = config.repo_root / "AGENTS.md"
    agents = agents_path.read_text() if agents_path.is_file() else ""
    scope = config.assignment_path.read_text()
    mode_rule = (
        "You may experiment only inside this disposable worktree; do not commit."
        if config.mode == "discover"
        else f"Change only files necessary to remediate finding {config.finding_id}; do not commit."
    )
    return "\n".join([
        "You are an untrusted lab security worker in a disposable worktree.",
        mode_rule,
        "Do not access external directories, credentials, networks, Git remotes, or live systems.",
        f"MODE={config.mode}",
        f"BASE_COMMIT={paths.base_commit}",
        f"FINDING_ID={config.finding_id or ''}",
        "\n--- REPOSITORY AGENTS.md ---\n", agents,
        "\n--- SECURITY METHODOLOGY ---\n", methodology,
        "\n--- ASSIGNMENT ---\n", scope,
        "\n--- RESULT SCHEMA ---\n", json.dumps(schema, sort_keys=True),
        "Return the result as exactly one fenced JSON document matching the schema, followed by a Markdown summary.",
    ])


def extract_final_answer(event_stream: str) -> str:
    texts: list[str] = []
    for line_number, line in enumerate(event_stream.splitlines(), 1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise DispatchError(f"invalid OpenCode JSON event at line {line_number}") from exc
        if event.get("type") == "text":
            part = event.get("part", {})
            text = part.get("text") if isinstance(part, dict) else None
            if isinstance(text, str):
                texts.append(text)
    if not texts:
        raise DispatchError("OpenCode event stream contained no final text")
    return "".join(texts)


def parse_model_answer(answer: str) -> tuple[dict[str, Any], str]:
    matches = re.findall(r"```json\s*\n(.*?)\n```", answer, flags=re.DOTALL)
    if len(matches) != 1:
        raise DispatchError("model answer must contain exactly one fenced JSON document")
    try:
        result = json.loads(matches[0])
    except json.JSONDecodeError as exc:
        raise DispatchError("model result is not valid JSON") from exc
    fence = re.search(r"```json\s*\n.*?\n```", answer, flags=re.DOTALL)
    assert fence is not None
    summary = (answer[:fence.start()] + answer[fence.end():]).strip()
    return result, summary


def validate_schema(value: Any, schema: dict[str, Any], path: str = "$") -> None:
    if "const" in schema and value != schema["const"]:
        raise DispatchError(f"result schema violation at {path}: wrong constant")
    if "enum" in schema and value not in schema["enum"]:
        raise DispatchError(f"result schema violation at {path}: invalid enum")
    kind = schema.get("type")
    valid_type = {
        "object": lambda item: isinstance(item, dict),
        "array": lambda item: isinstance(item, list),
        "string": lambda item: isinstance(item, str),
        "integer": lambda item: isinstance(item, int) and not isinstance(item, bool),
    }.get(kind)
    if valid_type and not valid_type(value):
        raise DispatchError(f"result schema violation at {path}: expected {kind}")
    if kind == "object":
        properties = schema.get("properties", {})
        missing = [key for key in schema.get("required", []) if key not in value]
        if missing:
            raise DispatchError(f"result schema violation at {path}: missing {', '.join(missing)}")
        if schema.get("additionalProperties") is False:
            extras = set(value) - set(properties)
            if extras:
                raise DispatchError(f"result schema violation at {path}: extra properties")
        for key, child in value.items():
            if key in properties:
                validate_schema(child, properties[key], f"{path}.{key}")
    if kind == "array":
        item_schema = schema.get("items", {})
        for index, item in enumerate(value):
            validate_schema(item, item_schema, f"{path}[{index}]")


def check_exo_health(config: DispatchConfig) -> None:
    request = Request(f"{config.api_base}/v1/models", method="GET")
    try:
        with urlopen(request, timeout=10) as response:
            if response.status != 200:
                raise DispatchError(f"exo health check returned HTTP {response.status}")
            json.loads(response.read())
    except DispatchError:
        raise
    except Exception as exc:
        raise DispatchError("exo health check failed") from exc


def run_worker(config: DispatchConfig, paths: RunPaths) -> tuple[dict[str, Any], str]:
    env = build_worker_environment(config, paths.runtime)
    for name in ("home", "config", "data", "state", "cache"):
        (paths.runtime / name).mkdir(parents=True, exist_ok=True)
    opencode_root = paths.runtime / "config" / "opencode"
    opencode_root.mkdir(parents=True)
    (opencode_root / "opencode.json").write_text(
        json.dumps(build_opencode_config(config), indent=2, sort_keys=True) + "\n"
    )
    prompt = assemble_assignment(config, paths)
    try:
        completed = subprocess.run(
            [
                config.opencode_bin, "run", "--pure", "--format", "json",
                "-m", f"exo/{config.model}", prompt,
            ],
            cwd=paths.worktree,
            env=env,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=config.timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise DispatchError("OpenCode worker timed out") from exc
    safe_stderr = completed.stderr.replace(config.api_base, "<EXO_API>")
    paths.log.write_text(safe_stderr[-131072:])
    if completed.returncode:
        raise DispatchError(f"OpenCode worker exited with status {completed.returncode}")
    answer = extract_final_answer(completed.stdout)
    return parse_model_answer(answer)


def dispatch(config: DispatchConfig) -> RunPaths:
    check_exo_health(config)
    paths = prepare_run(config)
    result, summary = run_worker(config, paths)
    _, schema = load_contract(config.repo_root, config.mode)
    validate_schema(result, schema)
    if result.get("base_commit") != paths.base_commit:
        raise DispatchError("result base commit does not match worktree")
    if config.mode == "repair" and result.get("finding_id") != config.finding_id:
        raise DispatchError("result finding id does not match assignment")
    diff = capture_worktree_diff(paths)
    diff_sha256 = hashlib.sha256(diff.encode()).hexdigest()
    if config.mode == "repair" and result.get("diff_sha256") != diff_sha256:
        raise DispatchError("repair result diff digest does not match worktree")
    paths.result_json.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    paths.summary_md.write_text(summary + "\n")
    (paths.runtime / "worktree.diff").write_text(diff)
    metadata = json.loads(paths.metadata.read_text())
    metadata.update({"status": "complete", "diff_sha256": diff_sha256})
    paths.metadata.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n")
    return paths


def _usage() -> str:
    return """Usage:
  lab-security-agent discover RUN --assignment FILE [options]
  lab-security-agent repair RUN --assignment FILE --finding-id ID [options]
  lab-security-agent cleanup RUN [--discard-changes]

Runs an untrusted lab model in a disposable worktree. The command never commits, pushes, or merges.
"""


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in {"-h", "--help"}:
        print(_usage())
        return 0
    try:
        if args[0] == "cleanup":
            if len(args) not in {2, 3} or (len(args) == 3 and args[2] != "--discard-changes"):
                raise DispatchError("cleanup requires RUN and optional --discard-changes")
            run_name = args[1]
            if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", run_name):
                raise DispatchError("invalid run name")
            repo = Path.cwd().resolve()
            run_root = repo / ".lab-agents" / run_name
            metadata_path = run_root / "runtime" / "metadata.json"
            if not metadata_path.is_file() or metadata_path.is_symlink():
                raise DispatchError("recorded run was not found")
            metadata = json.loads(metadata_path.read_text())
            worktree = Path(metadata["worktree"])
            runtime = run_root / "runtime"
            paths = RunPaths(
                run_root=run_root,
                worktree=worktree,
                runtime=runtime,
                result_json=runtime / "result.json",
                summary_md=runtime / "summary.md",
                log=runtime / "opencode.log",
                metadata=metadata_path,
                base_commit=metadata["base_commit"],
                branch=metadata.get("branch"),
            )
            cleanup_config = DispatchConfig(
                repo_root=repo,
                mode="repair" if paths.branch else "discover",
                run_name=run_name,
                assignment_path=repo / ".gitignore",
            )
            cleanup_run(
                cleanup_config,
                paths,
                discard_changes="--discard-changes" in args,
            )
            print(f"removed={run_root}")
            return 0
        config = parse_args(args)
        paths = dispatch(config)
    except DispatchError as exc:
        print(f"lab-security-agent: {exc}", file=sys.stderr)
        return 1
    print(f"base_commit={paths.base_commit}")
    print(f"worktree={paths.worktree}")
    print(f"result={paths.result_json}")
    print(f"summary={paths.summary_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


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
        "*": "allow",
        "doom_loop": "deny",
        "external_directory": "deny",
        "lsp": "deny",
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
