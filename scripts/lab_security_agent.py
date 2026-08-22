from __future__ import annotations

import argparse
import json
import os
import re
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
