#!/usr/bin/env python3
"""The operator boundary around the external SOPS vault.

Secret values exist in exactly two places: the encrypted vault documents and
the decrypted bytes this process holds in a private file for the length of one
command. They never enter argv, logs, shell history, or a tracked file.

Commands: doctor, list, edit, create, apply, get, exec-env, updatekeys,
backup, restore. See docs/security/operator-secret-enrollment.md.
"""

from __future__ import annotations

import argparse
import base64
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_VAULT = Path("/home/glasslab/.local/share/glasslab-secrets")
EXPECTED_SOPS_VERSION = "3.13.3"
INVENTORY_VERSION = 1
SECRET_SUFFIX = ".sops.yaml"


class SecretError(Exception):
    """A refusal the operator must fix; the message never contains a value."""


@dataclass(frozen=True)
class SecretRecord:
    name: str
    relative_path: str
    target: str
    owner: str


def vault_dir() -> Path:
    return Path(os.environ.get("GLASSLAB_SECRET_VAULT", DEFAULT_VAULT))


def policy_path() -> Path:
    return Path(os.environ.get("SOPS_CONFIG", REPOSITORY_ROOT / ".sops.yaml"))


def inventory_path(vault: Path) -> Path:
    return vault / "inventory.yaml"


def require_no_xtrace() -> None:
    if "xtrace" in os.environ.get("SHELLOPTS", "").split(":"):
        raise SecretError("shell tracing is enabled (set +x before running)")


def _single_line(value: object, field: str) -> str:
    text = str(value).strip()
    if not text or len(text) > 200 or "\n" in text or "\r" in text or not text.isprintable():
        raise SecretError(f"{field} must be one printable line of at most 200 characters")
    return text


def load_inventory(path: Path) -> list[SecretRecord]:
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise SecretError(f"inventory not found: {path}") from error
    except yaml.YAMLError as error:
        raise SecretError(f"inventory is not valid YAML: {error}") from error
    if not isinstance(document, dict) or document.get("version") != INVENTORY_VERSION:
        raise SecretError(f"inventory must be a mapping with version: {INVENTORY_VERSION}")
    entries = document.get("secrets")
    if not isinstance(entries, list) or not entries:
        raise SecretError("inventory.secrets must be a non-empty list")
    records: list[SecretRecord] = []
    names: set[str] = set()
    paths: set[str] = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict) or set(entry) != {"name", "relative_path", "target", "owner"}:
            raise SecretError(f"inventory.secrets[{index}] must have exactly name, relative_path, target, owner")
        record = SecretRecord(
            name=_single_line(entry["name"], "name"),
            relative_path=_single_line(entry["relative_path"], "relative_path"),
            target=_single_line(entry["target"], "target"),
            owner=_single_line(entry["owner"], "owner"),
        )
        relative = Path(record.relative_path)
        if relative.is_absolute() or ".." in relative.parts or "" in relative.parts:
            raise SecretError(f"{record.name}: relative_path must stay below the vault")
        if not record.relative_path.endswith(SECRET_SUFFIX):
            raise SecretError(f"{record.name}: relative_path must end with {SECRET_SUFFIX}")
        if record.name in names or record.relative_path in paths:
            raise SecretError(f"{record.name}: duplicate name or path in inventory")
        names.add(record.name)
        paths.add(record.relative_path)
        records.append(record)
    return records


def resolve_secret(name: str, vault: Path | None = None) -> tuple[SecretRecord, Path]:
    vault = vault or vault_dir()
    for record in load_inventory(inventory_path(vault)):
        if record.name == name:
            path = (vault / record.relative_path).resolve()
            if not path.is_relative_to(vault.resolve()):
                raise SecretError(f"{name}: relative_path escapes the vault")
            if not path.is_file():
                raise SecretError(f"{name}: missing document {record.relative_path}")
            return record, path
    raise SecretError(f"unknown secret {name!r}; see {inventory_path(vault)}")


def check_mode(path: Path, expected: int, kind: str) -> None:
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode != expected:
        raise SecretError(f"{kind} {path} has mode {mode:o}; expected {expected:o}")


def run(argv: list[str], *, input_bytes: bytes | None = None, check: bool = True,
        stdout=None) -> subprocess.CompletedProcess:
    return subprocess.run(argv, input=input_bytes, stdout=stdout, check=check, shell=False)


def sops_version() -> str:
    result = subprocess.run(["sops", "--version"], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise SecretError(f"sops is not usable: {result.stderr.strip()[:200]}")
    return result.stdout.strip().splitlines()[0] if result.stdout.strip() else "unknown"


def private_tempdir(parent: str | None = None) -> tempfile.TemporaryDirectory:
    directory = parent or os.environ.get("XDG_RUNTIME_DIR")
    temp = tempfile.TemporaryDirectory(prefix="glasslab-secret-", dir=directory)
    os.chmod(temp.name, 0o700)
    return temp


def decrypt_to_private_file(source: Path, temp_dir: str) -> Path:
    target = Path(temp_dir) / "secret.yaml"
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        run(["sops", "--decrypt", str(source)], stdout=handle)
    os.chmod(target, 0o600)
    return target


def load_document(path: Path) -> dict:
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as error:
        raise SecretError(f"decrypted document is not valid YAML: {error}") from error
    if not isinstance(document, dict) or document.get("kind") != "Secret":
        raise SecretError("decrypted document is not a Kubernetes Secret")
    metadata = document.get("metadata")
    if not isinstance(metadata, dict) or not metadata.get("name"):
        raise SecretError("decrypted Secret has no metadata.name")
    return document


def secret_values(document: dict) -> dict[str, str]:
    values: dict[str, str] = {}
    for key, value in (document.get("data") or {}).items():
        values[key] = base64.b64decode(value).decode("utf-8")
    values.update({key: str(value) for key, value in (document.get("stringData") or {}).items()})
    return values


def command_doctor(args: argparse.Namespace) -> int:
    problems: list[str] = []
    vault = vault_dir()
    try:
        require_no_xtrace()
        print(f"sops: {sops_version()}")
        if EXPECTED_SOPS_VERSION not in sops_version():
            problems.append(f"sops is not the pinned {EXPECTED_SOPS_VERSION}")
        if not shutil.which("gpg"):
            problems.append("gpg is not installed")
        if not vault.is_dir():
            problems.append(f"vault is missing: {vault}")
        else:
            check_mode(vault, 0o700, "vault")
            inventory = inventory_path(vault)
            if not inventory.is_file():
                problems.append(f"inventory is missing: {inventory}")
            else:
                check_mode(inventory, 0o600, "inventory")
                for record in load_inventory(inventory):
                    path = (vault / record.relative_path).resolve()
                    if not path.is_relative_to(vault.resolve()):
                        problems.append(f"{record.name}: path escapes the vault")
                        continue
                    if not path.is_file():
                        problems.append(f"{record.name}: missing document")
                        continue
                    check_mode(path, 0o600, record.name)
        if not policy_path().is_file():
            problems.append(f"SOPS policy is missing: {policy_path()}")
        print(f"vault: {vault}")
        print(f"policy: {policy_path()}")
    except SecretError as error:
        problems.append(str(error))
    for problem in problems:
        print(f"FAIL {problem}", file=sys.stderr)
    print("doctor: " + ("fail" if problems else "ok"))
    return 1 if problems else 0


def command_list(args: argparse.Namespace) -> int:
    for record in load_inventory(inventory_path(vault_dir())):
        print(f"{record.name}\t{record.target}\t{record.owner}")
    return 0


def command_edit(args: argparse.Namespace) -> int:
    record, path = resolve_secret(args.name)
    print(f"editing {record.name} ({record.target}) in sops", file=sys.stderr)
    return subprocess.call(["sops", "edit", str(path)], shell=False)


def command_create(args: argparse.Namespace) -> int:
    record, path = resolve_secret(args.name)
    source = Path(args.source)
    if not source.is_file():
        raise SecretError(f"plaintext manifest not found: {source}")
    if path.exists() and not args.force:
        raise SecretError(f"{path} exists; pass --force to replace it")
    document = load_document(source)
    plaintext = source.read_bytes()
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    encrypted = subprocess.run(
        ["sops", "--encrypt", "--input-type", "yaml", "--output-type", "yaml",
         "--filename-override", str(path)],
        input=plaintext, capture_output=True, check=False,
    )
    if encrypted.returncode != 0:
        raise SecretError(f"sops refused to encrypt {record.name}: {encrypted.stderr.decode(errors='replace')[:300]}")
    if b"ENC[" not in encrypted.stdout:
        raise SecretError("sops output does not look encrypted; refusing to write it")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(encrypted.stdout)
    os.chmod(path, 0o600)
    print(f"wrote {record.name} from {source.name} ({document['metadata']['name']})")
    return 0


def command_apply(args: argparse.Namespace) -> int:
    record, source = resolve_secret(args.name)
    if ".example." in str(source):
        raise SecretError("refusing to apply a documentation example")
    with private_tempdir() as temp:
        target = decrypt_to_private_file(source, temp)
        load_document(target)
        run(["kubectl", "apply", "-f", "-"], input_bytes=target.read_bytes())
    print(f"applied {record.name} to {record.target}")
    return 0


def command_get(args: argparse.Namespace) -> int:
    record, source = resolve_secret(args.name)
    if sys.stdout.isatty():
        print("warning: printing a secret to a terminal", file=sys.stderr)
    with private_tempdir() as temp:
        document = load_document(decrypt_to_private_file(source, temp))
    values = secret_values(document)
    if args.key not in values:
        raise SecretError(f"{record.name} has no key {args.key!r}")
    print(values[args.key])
    return 0


def command_exec_env(args: argparse.Namespace) -> int:
    record, source = resolve_secret(args.name)
    keys = [key.strip() for key in args.keys.split(",") if key.strip()]
    if not keys:
        raise SecretError("name at least one key to expose")
    with private_tempdir() as temp:
        values = secret_values(load_document(decrypt_to_private_file(source, temp)))
    missing = [key for key in keys if key not in values]
    if missing:
        raise SecretError(f"{record.name} has no key(s) {', '.join(missing)}")
    environment = {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")}
    environment.update({key: values[key] for key in keys})
    print(f"running {args.command[0]} with {', '.join(keys)} from {record.name}", file=sys.stderr)
    return subprocess.call(args.command, env=environment, shell=False)


def command_updatekeys(args: argparse.Namespace) -> int:
    changed = 0
    for record in load_inventory(inventory_path(vault_dir())):
        _, path = resolve_secret(record.name)
        result = subprocess.run(["sops", "updatekeys", "--yes", str(path)], capture_output=True, text=True, check=False)
        if result.returncode != 0:
            raise SecretError(f"sops updatekeys failed for {record.name}: {result.stderr.strip()[:300]}")
        changed += 1
    print(f"updatekeys: {changed} document(s) checked")
    return 0


def command_backup(args: argparse.Namespace) -> int:
    return subprocess.call(
        [str(REPOSITORY_ROOT / "scripts" / "backup-glasslab-secrets.sh"),
         "--vault-dir", str(vault_dir()), "--policy", str(policy_path()),
         "--output-dir", args.output_dir], shell=False)


def command_restore(args: argparse.Namespace) -> int:
    return subprocess.call(
        [str(REPOSITORY_ROOT / "scripts" / "restore-glasslab-secrets.sh"),
         "--archive", args.archive, "--vault-dir", str(vault_dir()), "--yes"], shell=False)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="glasslab-secret", description=__doc__.splitlines()[0])
    subparsers = parser.add_subparsers(dest="operation", required=True)

    subparsers.add_parser("doctor", help="check sops, gpg, vault, inventory, and policy")
    subparsers.add_parser("list", help="list inventory names, targets, and owners")

    edit = subparsers.add_parser("edit", help="edit one encrypted document with sops")
    edit.add_argument("name")

    create = subparsers.add_parser("create", help="encrypt a plaintext Secret manifest into the vault")
    create.add_argument("name")
    create.add_argument("--from", dest="source", required=True)
    create.add_argument("--force", action="store_true")

    apply_command = subparsers.add_parser("apply", help="decrypt and kubectl apply one document")
    apply_command.add_argument("name")

    get = subparsers.add_parser("get", help="print one key from a document")
    get.add_argument("name")
    get.add_argument("key")

    exec_env = subparsers.add_parser("exec-env", help="run a command with selected keys as environment")
    exec_env.add_argument("name")
    exec_env.add_argument("keys")
    exec_env.add_argument("command", nargs=argparse.REMAINDER)

    subparsers.add_parser("updatekeys", help="re-encrypt every document for the current recipients")

    backup = subparsers.add_parser("backup", help="archive the encrypted vault")
    backup.add_argument("--output-dir", required=True)

    restore = subparsers.add_parser("restore", help="restore the encrypted vault from an archive")
    restore.add_argument("--archive", required=True)
    return parser


COMMANDS = {
    "doctor": command_doctor, "list": command_list, "edit": command_edit, "create": command_create,
    "apply": command_apply, "get": command_get, "exec-env": command_exec_env,
    "updatekeys": command_updatekeys, "backup": command_backup, "restore": command_restore,
}


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    args = build_parser().parse_args(argv)
    try:
        require_no_xtrace()
        return COMMANDS[args.operation](args)
    except SecretError as error:
        print(f"glasslab-secret: {error}", file=sys.stderr)
        return 2
    except FileNotFoundError as error:
        print(f"glasslab-secret: missing tool: {error.filename}", file=sys.stderr)
        return 2
    except subprocess.CalledProcessError as error:
        print(f"glasslab-secret: {error.cmd[0]} exited {error.returncode}", file=sys.stderr)
        return error.returncode or 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
