"""Behavioral tests for encrypted-only secret vault backup and restore."""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import yaml


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
BACKUP_HELPER = REPOSITORY_ROOT / "scripts" / "backup-glasslab-secrets.sh"
RESTORE_HELPER = REPOSITORY_ROOT / "scripts" / "restore-glasslab-secrets.sh"
PULL_HELPER = REPOSITORY_ROOT / "scripts" / "pull-glasslab-secrets-backup.sh"
SENTINEL = "c2VjcmV0LWJhY2t1cC1zdGRvdXQtc2VudGluZWw="
FINGERPRINT = "A" * 40


def load_archive_boundary():
    module_path = REPOSITORY_ROOT / "scripts" / "secret_backup_restore.py"
    spec = importlib.util.spec_from_file_location("secret_backup_restore", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load secret backup/restore boundary")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def encrypted_document(*, sentinel: str = SENTINEL) -> str:
    envelope = (
        "ENC[AES256_GCM,data:Y2lwaGVydGV4dA==,"
        "iv:aXYtaXYtaXYtaXY=,tag:dGFnLXRhZw==,type:str]"
    )
    return yaml.safe_dump(
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "stringData": {
                "TOKEN": envelope.replace("Y2lwaGVydGV4dA==", sentinel),
            },
            "sops": {
                "pgp": [
                    {
                        "created_at": "2026-08-21T00:00:00Z",
                        "enc": (
                            "-----BEGIN PGP MESSAGE-----\n"
                            "\n"
                            "Zml4dHVyZQ==\n"
                            "-----END PGP MESSAGE-----"
                        ),
                        "fp": FINGERPRINT,
                    }
                ],
                "mac": envelope,
                "version": "3.9.0",
            },
        },
        sort_keys=False,
    )


def inventory(paths: list[str]) -> str:
    return yaml.safe_dump(
        {
            "version": 1,
            "secrets": [
                {
                    "name": f"secret-{index}",
                    "relative_path": path,
                    "target": f"namespace/secret-{index}",
                    "owner": "platform",
                }
                for index, path in enumerate(paths, start=1)
            ],
        },
        sort_keys=False,
    )


def policy() -> str:
    return yaml.safe_dump(
        {
            "creation_rules": [
                {
                    "path_regex": r".*\.sops\.yaml$",
                    "pgp": FINGERPRINT,
                }
            ]
        },
        sort_keys=False,
    )


class SecretBackupRestoreTests(unittest.TestCase):
    """The vault archive boundary never decrypts or trusts archive metadata."""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.vault = self.root / "source-vault"
        self.output = self.root / "backups"
        self.policy = self.root / ".sops.yaml"
        self.relative_secret = "kubeadm/glasslab-v2/app.sops.yaml"
        self.write_vault([self.relative_secret])
        self.policy.write_text(policy(), encoding="utf-8")
        self.policy.chmod(0o600)

    def write_vault(self, paths: list[str]) -> None:
        self.vault.mkdir(mode=0o700, parents=True, exist_ok=True)
        inventory_path = self.vault / "inventory.yaml"
        inventory_path.write_text(inventory(paths), encoding="utf-8")
        inventory_path.chmod(0o600)
        for relative_path in paths:
            document_path = self.vault / relative_path
            document_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            document_path.write_text(encrypted_document(), encoding="utf-8")
            document_path.chmod(0o600)

    def run_backup(self, **overrides: str) -> subprocess.CompletedProcess[str]:
        command = [
            str(BACKUP_HELPER),
            "--vault-dir",
            overrides.get("vault", str(self.vault)),
            "--policy",
            overrides.get("policy", str(self.policy)),
            "--output-dir",
            overrides.get("output", str(self.output)),
            "--stamp",
            overrides.get("stamp", "20260821-120000"),
        ]
        return subprocess.run(
            command,
            cwd=REPOSITORY_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )

    def archive_path(self) -> Path:
        return self.output / "glasslab-secrets-20260821-120000.tar.gz"

    def run_restore(
        self,
        archive: Path,
        vault: Path,
        *,
        confirm: bool = True,
        environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        command = [
            str(RESTORE_HELPER),
            "--archive",
            str(archive),
            "--vault-dir",
            str(vault),
        ]
        if confirm:
            command.append("--yes")
        return subprocess.run(
            command,
            cwd=REPOSITORY_ROOT,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )

    def assert_sentinel_absent(self, result: subprocess.CompletedProcess[str]) -> None:
        self.assertNotIn(SENTINEL, result.stdout)
        self.assertNotIn(SENTINEL, result.stderr)

    def write_archive_checksum(self, archive: Path) -> None:
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        archive.with_name(archive.name + ".sha256").write_text(
            f"{digest}  {archive.name}\n",
            encoding="ascii",
        )

    def rewrite_archive(
        self,
        archive: Path,
        transform,
    ) -> None:
        with tarfile.open(archive, "r:gz") as source:
            members: list[tuple[tarfile.TarInfo, bytes]] = []
            for member in source.getmembers():
                stream = source.extractfile(member) if member.isreg() else None
                members.append((member, stream.read() if stream is not None else b""))
        replacement = archive.with_name("replacement.tar.gz")
        transformed = transform(members)
        with tarfile.open(replacement, "w:gz", format=tarfile.PAX_FORMAT) as target:
            for member, contents in transformed:
                copied = tarfile.TarInfo(member.name)
                copied.mode = member.mode
                copied.type = member.type
                copied.linkname = member.linkname
                copied.size = len(contents) if copied.isreg() else 0
                target.addfile(copied, io.BytesIO(contents) if copied.isreg() else None)
        replacement.replace(archive)
        self.write_archive_checksum(archive)

    def test_successful_round_trip_restores_private_vault_and_preserves_rollback(self):
        """Losing the atomic swap or rollback would destroy the last known-good vault."""
        backup = self.run_backup()
        self.assertEqual(backup.returncode, 0, backup.stderr)
        self.assert_sentinel_absent(backup)

        archive = self.archive_path()
        self.assertTrue(archive.is_file())
        self.assertTrue(archive.with_name(archive.name + ".sha256").is_file())
        with tarfile.open(archive, "r:gz") as bundle:
            self.assertEqual(
                {member.name for member in bundle.getmembers()},
                {
                    "SHA256SUMS",
                    "policy/.sops.yaml",
                    "vault/inventory.yaml",
                    f"vault/{self.relative_secret}",
                },
            )
            self.assertTrue(all(member.isreg() for member in bundle.getmembers()))

        restored_vault = self.root / "restored-vault"
        restored_vault.mkdir(mode=0o700)
        old_path = restored_vault / "old.sops.yaml"
        old_path.write_text("old-vault-sentinel\n", encoding="utf-8")
        old_path.chmod(0o600)

        restore = self.run_restore(archive, restored_vault)
        self.assertEqual(restore.returncode, 0, restore.stderr)
        self.assert_sentinel_absent(restore)
        self.assertEqual(
            (restored_vault / self.relative_secret).read_text(encoding="utf-8"),
            (self.vault / self.relative_secret).read_text(encoding="utf-8"),
        )
        self.assertEqual(stat.S_IMODE(restored_vault.stat().st_mode), 0o700)
        self.assertEqual(
            stat.S_IMODE((restored_vault / self.relative_secret).stat().st_mode),
            0o600,
        )
        rollbacks = list(self.root.glob("restored-vault.rollback-*-*"))
        self.assertEqual(len(rollbacks), 1, rollbacks)
        self.assertEqual(
            (rollbacks[0] / "old.sops.yaml").read_text(encoding="utf-8"),
            "old-vault-sentinel\n",
        )

    def test_backup_rejects_inventory_path_whose_file_is_missing(self):
        """A stale inventory record must not produce a falsely complete backup."""
        (self.vault / self.relative_secret).unlink()

        result = self.run_backup()

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.archive_path().exists())
        self.assert_sentinel_absent(result)

    def test_backup_rejects_sops_document_missing_from_inventory(self):
        """A newly added encrypted document must not silently fall outside recovery."""
        extra = self.vault / "unlisted.sops.yaml"
        extra.write_text(encrypted_document(), encoding="utf-8")
        extra.chmod(0o600)

        result = self.run_backup()

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.archive_path().exists())
        self.assert_sentinel_absent(result)

    def test_backup_rejects_symlinked_inventory_document(self):
        """Following a vault symlink could archive ciphertext outside the approved vault."""
        outside = self.root / "outside.sops.yaml"
        outside.write_text(encrypted_document(), encoding="utf-8")
        (self.vault / self.relative_secret).unlink()
        (self.vault / self.relative_secret).symlink_to(outside)

        result = self.run_backup()

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.archive_path().exists())
        self.assert_sentinel_absent(result)

    def test_backup_fails_closed_when_an_unlisted_subtree_cannot_be_traversed(self):
        """A privileged test must still prove unreadable subtrees cannot be omitted."""
        blocked = self.vault / "unreadable"
        blocked.mkdir(mode=0o700)
        hidden = blocked / "unlisted.sops.yaml"
        hidden.write_text(encrypted_document(), encoding="utf-8")
        boundary = load_archive_boundary()
        real_scandir = os.scandir

        def unreadable_scandir(path):
            if not isinstance(path, int) and Path(path) == blocked:
                raise PermissionError("controlled unreadable subtree")
            return real_scandir(path)

        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.object(boundary.os, "scandir", side_effect=unreadable_scandir):
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                result = boundary.main(
                    [
                        "backup",
                        "--vault-dir",
                        str(self.vault),
                        "--policy",
                        str(self.policy),
                        "--output",
                        str(self.archive_path()),
                    ]
                )

        self.assertNotEqual(result, 0)
        self.assertFalse(self.archive_path().exists())
        self.assertNotIn(SENTINEL, stdout.getvalue() + stderr.getvalue())

    def test_extracted_validation_fails_closed_when_a_subtree_cannot_be_traversed(self):
        """Post-extraction traversal errors must not make an incomplete tree look exact."""
        workspace = self.root / "extracted"
        blocked = workspace / "unreadable"
        blocked.mkdir(mode=0o700, parents=True)
        (blocked / "hidden.sops.yaml").write_text(encrypted_document(), encoding="utf-8")
        boundary = load_archive_boundary()
        real_scandir = os.scandir

        def unreadable_scandir(path):
            if not isinstance(path, int) and Path(path) == blocked:
                raise PermissionError("controlled unreadable extracted subtree")
            return real_scandir(path)

        with mock.patch.object(boundary.os, "scandir", side_effect=unreadable_scandir):
            with self.assertRaises(boundary.ArchiveBoundaryError):
                boundary._walk_extracted_files(workspace)

    def test_backup_interruption_after_first_publish_removes_pair_and_allows_retry(self):
        """A signal between hard links must not strand a one-file backup stamp."""
        boundary = load_archive_boundary()
        output = self.archive_path()
        real_link = os.link
        link_count = 0

        def interrupt_after_link(source, destination):
            nonlocal link_count
            real_link(source, destination)
            link_count += 1
            if link_count == 1:
                raise boundary.OperationInterrupted("controlled publication interruption")

        with mock.patch.object(boundary.os, "link", side_effect=interrupt_after_link):
            with self.assertRaises(boundary.OperationInterrupted):
                boundary.create_backup(vault=self.vault, policy=self.policy, output=output)

        self.assertFalse(output.exists())
        self.assertFalse(output.with_name(output.name + ".sha256").exists())
        boundary.create_backup(vault=self.vault, policy=self.policy, output=output)
        self.assertTrue(output.is_file())
        self.assertTrue(output.with_name(output.name + ".sha256").is_file())

    def test_backup_publication_race_preserves_file_not_linked_from_its_stage(self):
        """Rollback must never delete a no-clobber destination created by another actor."""
        boundary = load_archive_boundary()
        output = self.archive_path()
        output.parent.mkdir(mode=0o700, parents=True)
        preexisting = b"preexisting-artifact"

        def lose_publication_race(_source, destination):
            Path(destination).write_bytes(preexisting)
            raise FileExistsError("controlled no-clobber race")

        with mock.patch.object(boundary.os, "link", side_effect=lose_publication_race):
            with self.assertRaises(boundary.ArchiveBoundaryError):
                boundary.create_backup(vault=self.vault, policy=self.policy, output=output)

        self.assertEqual(output.read_bytes(), preexisting)
        self.assertFalse(output.with_name(output.name + ".sha256").exists())

    def test_backup_rejects_mixed_plaintext_secret_payload_without_echoing_it(self):
        """One encrypted key must not hide another plaintext data or stringData key."""
        document_path = self.vault / self.relative_secret
        document = yaml.safe_load(document_path.read_text(encoding="utf-8"))
        document["stringData"]["PLAINTEXT"] = SENTINEL
        document_path.write_text(yaml.safe_dump(document), encoding="utf-8")

        result = self.run_backup()

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.archive_path().exists())
        self.assert_sentinel_absent(result)

    def test_backup_rejects_malformed_sops_envelope_beside_valid_ciphertext(self):
        """A string beginning with ENC is not ciphertext unless the full envelope is valid."""
        document_path = self.vault / self.relative_secret
        document = yaml.safe_load(document_path.read_text(encoding="utf-8"))
        document["data"] = {"BROKEN": "ENC[not-a-sops-envelope]"}
        document_path.write_text(yaml.safe_dump(document), encoding="utf-8")

        result = self.run_backup()

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.archive_path().exists())
        self.assert_sentinel_absent(result)

    def test_backup_rejects_non_pgp_recipient_metadata(self):
        """Unsupported recipient lists must not satisfy the approved OpenPGP boundary."""
        document_path = self.vault / self.relative_secret
        document = yaml.safe_load(document_path.read_text(encoding="utf-8"))
        document["sops"]["pgp"] = []
        document["sops"]["age"] = ["malformed-age-recipient"]
        document_path.write_text(yaml.safe_dump(document), encoding="utf-8")

        result = self.run_backup()

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.archive_path().exists())
        self.assert_sentinel_absent(result)

    def test_backup_rejects_malformed_openpgp_recipient_record(self):
        """A recipient needs a fingerprint, timestamp, and armored encrypted data key."""
        document_path = self.vault / self.relative_secret
        document = yaml.safe_load(document_path.read_text(encoding="utf-8"))
        document["sops"]["pgp"][0]["enc"] = "not-an-armored-message"
        document_path.write_text(yaml.safe_dump(document), encoding="utf-8")

        result = self.run_backup()

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.archive_path().exists())
        self.assert_sentinel_absent(result)

    def test_backup_rejects_armored_openpgp_record_with_invalid_body(self):
        """Armor markers alone must not make malformed OpenPGP payload data valid."""
        document_path = self.vault / self.relative_secret
        document = yaml.safe_load(document_path.read_text(encoding="utf-8"))
        document["sops"]["pgp"][0]["enc"] = (
            "-----BEGIN PGP MESSAGE-----\n\nnot-base64!\n-----END PGP MESSAGE-----"
        )
        document_path.write_text(yaml.safe_dump(document), encoding="utf-8")

        result = self.run_backup()

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.archive_path().exists())
        self.assert_sentinel_absent(result)

    def test_backup_rejects_duplicate_yaml_key_hiding_plaintext_payload(self):
        """A later encrypted duplicate must not hide earlier plaintext in the file."""
        document_path = self.vault / self.relative_secret
        duplicated = encrypted_document().replace(
            "stringData:\n",
            f"stringData:\n  PLAINTEXT: {SENTINEL}\nstringData:\n",
            1,
        )
        document_path.write_text(duplicated, encoding="utf-8")

        result = self.run_backup()

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.archive_path().exists())
        self.assert_sentinel_absent(result)

    def test_backup_rejects_malformed_policy_pgp_fingerprint(self):
        """A truthy policy value is not a usable approved OpenPGP recipient."""
        malformed_policy = yaml.safe_load(self.policy.read_text(encoding="utf-8"))
        malformed_policy["creation_rules"][0]["pgp"] = "not-a-fingerprint"
        self.policy.write_text(yaml.safe_dump(malformed_policy), encoding="utf-8")

        result = self.run_backup()

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.archive_path().exists())
        self.assert_sentinel_absent(result)

    def test_restore_rejects_corrupted_internal_checksum_before_replacement(self):
        """A modified encrypted document must not replace the active vault."""
        self.assertEqual(self.run_backup().returncode, 0)
        archive = self.archive_path()

        def corrupt(members):
            return [
                (member, contents + b"\ncorrupt")
                if member.name == f"vault/{self.relative_secret}"
                else (member, contents)
                for member, contents in members
            ]

        self.rewrite_archive(archive, corrupt)
        active = self.root / "active-vault"
        active.mkdir(mode=0o700)
        marker = active / "active-marker"
        marker.write_text("unchanged", encoding="utf-8")

        result = self.run_restore(archive, active)

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(marker.read_text(encoding="utf-8"), "unchanged")
        self.assertEqual(list(self.root.glob("active-vault.rollback-*")), [])
        self.assert_sentinel_absent(result)

    def test_restore_rejects_inventory_entry_missing_from_archive(self):
        """An archive missing an inventory-listed ciphertext file is not recoverable."""
        self.assertEqual(self.run_backup().returncode, 0)
        archive = self.archive_path()

        def remove_document(members):
            return [
                (member, contents)
                for member, contents in members
                if member.name != f"vault/{self.relative_secret}"
            ]

        self.rewrite_archive(archive, remove_document)
        active = self.root / "active-vault"
        active.mkdir(mode=0o700)

        result = self.run_restore(archive, active)

        self.assertNotEqual(result.returncode, 0)
        self.assert_sentinel_absent(result)

    def test_restore_rejects_absolute_archive_path_before_extraction(self):
        """An absolute tar member must never write outside private staging."""
        archive = self.root / "absolute.tar.gz"
        with tarfile.open(archive, "w:gz") as bundle:
            member = tarfile.TarInfo("/tmp/glasslab-secret-escape")
            member.size = 1
            bundle.addfile(member, io.BytesIO(b"x"))
        self.write_archive_checksum(archive)

        result = self.run_restore(archive, self.root / "active-vault")

        self.assertNotEqual(result.returncode, 0)
        self.assert_sentinel_absent(result)

    def test_restore_rejects_parent_traversal_before_extraction(self):
        """A parent traversal member must never escape private staging."""
        archive = self.root / "traversal.tar.gz"
        with tarfile.open(archive, "w:gz") as bundle:
            member = tarfile.TarInfo("vault/../../escape.sops.yaml")
            member.size = 1
            bundle.addfile(member, io.BytesIO(b"x"))
        self.write_archive_checksum(archive)

        result = self.run_restore(archive, self.root / "active-vault")

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / "escape.sops.yaml").exists())
        self.assert_sentinel_absent(result)

    def test_restore_rejects_symlink_member_before_extraction(self):
        """Archive symlinks must not redirect later writes outside staging."""
        archive = self.root / "symlink.tar.gz"
        with tarfile.open(archive, "w:gz") as bundle:
            member = tarfile.TarInfo("vault/secret.sops.yaml")
            member.type = tarfile.SYMTYPE
            member.linkname = "/tmp/glasslab-secret-escape"
            bundle.addfile(member)
        self.write_archive_checksum(archive)

        result = self.run_restore(archive, self.root / "active-vault")

        self.assertNotEqual(result.returncode, 0)
        self.assert_sentinel_absent(result)

    def test_restore_rejects_document_without_sops_metadata(self):
        """A checksum-valid plaintext impostor must not become the active vault."""
        self.assertEqual(self.run_backup().returncode, 0)
        archive = self.archive_path()

        def replace_document_and_checksums(members):
            bad_document = b"kind: Secret\nstringData:\n  TOKEN: " + SENTINEL.encode() + b"\n"
            replacements = {
                member.name: contents for member, contents in members
            }
            replacements[f"vault/{self.relative_secret}"] = bad_document
            checksum_lines = []
            for name in sorted(name for name in replacements if name != "SHA256SUMS"):
                checksum_lines.append(
                    f"{hashlib.sha256(replacements[name]).hexdigest()}  {name}\n"
                )
            replacements["SHA256SUMS"] = "".join(checksum_lines).encode("ascii")
            return [
                (member, replacements[member.name]) for member, _ in members
            ]

        self.rewrite_archive(archive, replace_document_and_checksums)

        result = self.run_restore(archive, self.root / "active-vault")

        self.assertNotEqual(result.returncode, 0)
        self.assert_sentinel_absent(result)

    def test_restore_requires_explicit_confirmation_and_leaves_active_vault_unchanged(self):
        """A valid archive alone must not authorize replacement."""
        self.assertEqual(self.run_backup().returncode, 0)
        active = self.root / "active-vault"
        active.mkdir(mode=0o700)
        marker = active / "active-marker"
        marker.write_text("unchanged", encoding="utf-8")

        result = self.run_restore(self.archive_path(), active, confirm=False)

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(marker.read_text(encoding="utf-8"), "unchanged")
        self.assert_sentinel_absent(result)

    def test_sigterm_cleans_random_private_staging_and_keeps_active_vault(self):
        """Interrupted extraction must leave neither a partial vault nor a fixed temp path."""
        self.assertEqual(self.run_backup().returncode, 0)
        active = self.root / "active-vault"
        active.mkdir(mode=0o700)
        marker = active / "active-marker"
        marker.write_text("unchanged", encoding="utf-8")
        tar_started = self.root / "tar-started"
        tar_arguments = self.root / "tar-arguments"
        fake_tar = self.root / "tar"
        fake_tar.write_text(
            f"""#!{os.path.realpath(sys.executable)}
import os
import signal
import sys
import time
from pathlib import Path

Path(os.environ["TAR_ARGUMENTS"]).write_text("\\n".join(sys.argv[1:]), encoding="utf-8")
Path(os.environ["TAR_STARTED"]).write_text("ready", encoding="utf-8")
signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
while True:
    time.sleep(0.1)
""",
            encoding="utf-8",
        )
        fake_tar.chmod(0o755)
        environment = os.environ.copy()
        environment["TAR_BIN"] = str(fake_tar)
        environment["GLASSLAB_SECRET_TEST_MODE"] = "1"
        environment["TAR_STARTED"] = str(tar_started)
        environment["TAR_ARGUMENTS"] = str(tar_arguments)
        process = subprocess.Popen(
            [
                str(RESTORE_HELPER),
                "--archive",
                str(self.archive_path()),
                "--vault-dir",
                str(active),
                "--yes",
            ],
            cwd=REPOSITORY_ROOT,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(lambda: process.poll() is None and process.kill())
        deadline = time.monotonic() + 5
        while not tar_started.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(tar_started.exists(), "restore never reached controlled tar boundary")
        arguments = tar_arguments.read_text(encoding="utf-8").splitlines()
        stage = Path(arguments[arguments.index("--directory") + 1])
        self.assertEqual(stage.parent, self.root)
        self.assertRegex(stage.name, r"^\.active-vault\.restore-[A-Za-z0-9_-]{6,}$")
        self.assertEqual(stat.S_IMODE(stage.stat().st_mode), 0o700)
        self.assertNotEqual(stage, self.root / ".active-vault.restore")

        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=5)

        self.assertNotEqual(process.returncode, 0)
        self.assertEqual(marker.read_text(encoding="utf-8"), "unchanged")
        self.assertFalse(stage.exists())
        self.assertEqual(list(self.root.glob("active-vault.rollback-*")), [])
        self.assertNotIn(SENTINEL, stdout + stderr)

    def test_restore_ignores_untrusted_tar_override_outside_explicit_test_mode(self):
        """Live restore must not execute a TAR_BIN selected through ambient environment."""
        self.assertEqual(self.run_backup().returncode, 0)
        fake_tar_ran = self.root / "untrusted-tar-ran"
        fake_tar = self.root / "untrusted-tar"
        fake_tar.write_text(
            f"""#!{os.path.realpath(sys.executable)}
from pathlib import Path
Path({str(fake_tar_ran)!r}).write_text("ran", encoding="utf-8")
raise SystemExit(99)
""",
            encoding="utf-8",
        )
        fake_tar.chmod(0o755)
        environment = os.environ.copy()
        environment["TAR_BIN"] = str(fake_tar)
        environment.pop("GLASSLAB_SECRET_TEST_MODE", None)
        active = self.root / "active-vault"

        result = self.run_restore(self.archive_path(), active, environment=environment)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(fake_tar_ran.exists())
        self.assertTrue((active / self.relative_secret).is_file())
        self.assert_sentinel_absent(result)

    def test_signal_pending_after_restore_commit_returns_success_matching_active_vault(self):
        """A post-swap signal must not report failure after the new vault is committed."""
        self.assertEqual(self.run_backup().returncode, 0)
        active = self.root / "active-vault"
        active.mkdir(mode=0o700)
        marker = active / "active-marker"
        marker.write_text("old-vault-must-be-rollback", encoding="utf-8")
        boundary = load_archive_boundary()
        real_replace = boundary._replace_vault_atomically

        def replace_then_signal(staged_vault: Path, vault: Path):
            rollback = real_replace(staged_vault, vault)
            os.kill(os.getpid(), signal.SIGTERM)
            return rollback

        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.object(
            boundary, "_replace_vault_atomically", side_effect=replace_then_signal
        ):
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                result = boundary.main(
                    [
                        "restore",
                        "--archive",
                        str(self.archive_path()),
                        "--vault-dir",
                        str(active),
                        "--yes",
                    ]
                )

        self.assertEqual(result, 0, stderr.getvalue())
        self.assertTrue((active / self.relative_secret).is_file())
        rollbacks = list(self.root.glob("active-vault.rollback-*-*"))
        self.assertEqual(len(rollbacks), 1, rollbacks)
        self.assertEqual(
            (rollbacks[0] / "active-marker").read_text(encoding="utf-8"),
            "old-vault-must-be-rollback",
        )
        self.assertIn("Validated encrypted vault restored", stdout.getvalue())
        self.assertIn("signal", stderr.getvalue().lower())
        self.assertIn("commit", stderr.getvalue().lower())
        self.assertNotIn(SENTINEL, stdout.getvalue() + stderr.getvalue())

    def test_irrecoverable_exchange_failure_preserves_old_vault_for_manual_recovery(self):
        """Cleanup must not erase the old vault after both rollback operations fail."""
        self.assertEqual(self.run_backup().returncode, 0)
        active = self.root / "active-vault"
        active.mkdir(mode=0o700)
        marker = active / "active-marker"
        marker.write_text("old-vault-must-survive", encoding="utf-8")
        blocked_rollback = self.root / "blocked-rollback"
        blocked_rollback.mkdir()
        (blocked_rollback / "occupied").write_text("occupied", encoding="utf-8")

        boundary = load_archive_boundary()
        real_exchange = boundary._rename_exchange
        exchange_calls: list[tuple[Path, Path]] = []

        def fail_exchange_back(first: Path, second: Path) -> None:
            exchange_calls.append((first, second))
            if len(exchange_calls) == 1:
                real_exchange(first, second)
                return
            raise boundary.ArchiveBoundaryError("fixture exchange-back failure")

        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.object(boundary, "_rollback_path", return_value=blocked_rollback):
            with mock.patch.object(boundary, "_rename_exchange", side_effect=fail_exchange_back):
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    result = boundary.main(
                        [
                            "restore",
                            "--archive",
                            str(self.archive_path()),
                            "--vault-dir",
                            str(active),
                            "--yes",
                        ]
                    )

        self.assertNotEqual(result, 0)
        self.assertEqual(len(exchange_calls), 2)
        recovery_workspace = exchange_calls[0][0].parent
        self.assertTrue(recovery_workspace.is_dir())
        self.assertEqual(
            (recovery_workspace / "vault" / "active-marker").read_text(encoding="utf-8"),
            "old-vault-must-survive",
        )
        self.assertTrue((active / self.relative_secret).is_file())
        self.assertNotIn(SENTINEL, stdout.getvalue() + stderr.getvalue())


class SecretBackupPullTests(unittest.TestCase):
    """The laptop pull flow transfers verified ciphertext without a passphrase path."""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.fake_bin = self.root / "bin"
        self.fake_bin.mkdir()
        self.remote_artifacts = self.root / "remote-artifacts"
        self.remote_artifacts.mkdir()
        self.local_artifacts = self.root / "local-artifacts"
        self.ssh_calls = self.root / "ssh-calls.jsonl"
        self.scp_calls = self.root / "scp-calls.jsonl"
        self._write_fake_commands()
        self._write_valid_remote_artifacts()

    def _write_fake_commands(self) -> None:
        ssh = self.fake_bin / "ssh"
        ssh.write_text(
            f"""#!{os.path.realpath(sys.executable)}
import json
import os
import sys
from pathlib import Path

with Path(os.environ["SSH_CALLS"]).open("a", encoding="utf-8") as stream:
    stream.write(json.dumps(sys.argv[1:]) + "\\n")
""",
            encoding="utf-8",
        )
        ssh.chmod(0o755)
        scp = self.fake_bin / "scp"
        scp.write_text(
            f"""#!{os.path.realpath(sys.executable)}
import json
import os
import shutil
import sys
from pathlib import Path

arguments = sys.argv[1:]
with Path(os.environ["SCP_CALLS"]).open("a", encoding="utf-8") as stream:
    stream.write(json.dumps(arguments) + "\\n")
sources = [item for item in arguments[:-1] if item != "--"]
destination = Path(arguments[-1])
destination.mkdir(mode=0o700, parents=True, exist_ok=True)
for source in sources:
    name = source.rsplit("/", 1)[-1]
    shutil.copy2(Path(os.environ["REMOTE_ARTIFACTS"]) / name, destination / name)
""",
            encoding="utf-8",
        )
        scp.chmod(0o755)
        link = self.fake_bin / "ln"
        link.write_text(
            f"""#!{os.path.realpath(sys.executable)}
import os
import signal
import sys
from pathlib import Path

arguments = [item for item in sys.argv[1:] if item != "--"]
os.link(arguments[0], arguments[1])
marker_name = os.environ.get("LINK_INTERRUPT_MARKER")
if marker_name:
    marker = Path(marker_name)
    if not marker.exists():
        marker.write_text("published", encoding="utf-8")
        os.kill(os.getppid(), signal.SIGUSR1)
""",
            encoding="utf-8",
        )
        link.chmod(0o755)

    def _write_valid_remote_artifacts(self) -> None:
        archive = self.remote_artifacts / "glasslab-secrets-20260821-130000.tar.gz"
        files = {
            "vault/inventory.yaml": inventory(["app.sops.yaml"]).encode(),
            "policy/.sops.yaml": policy().encode(),
            "vault/app.sops.yaml": encrypted_document().encode(),
        }
        files["SHA256SUMS"] = "".join(
            f"{hashlib.sha256(files[name]).hexdigest()}  {name}\n"
            for name in sorted(files)
        ).encode("ascii")
        with tarfile.open(archive, "w:gz") as bundle:
            for name in sorted(files):
                member = tarfile.TarInfo(name)
                member.size = len(files[name])
                member.mode = 0o600
                bundle.addfile(member, io.BytesIO(files[name]))
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        archive.with_name(archive.name + ".sha256").write_text(
            f"{digest}  {archive.name}\n", encoding="ascii"
        )

    def environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        environment["PATH"] = f"{self.fake_bin}:{environment['PATH']}"
        environment["SSH_CALLS"] = str(self.ssh_calls)
        environment["SCP_CALLS"] = str(self.scp_calls)
        environment["REMOTE_ARTIFACTS"] = str(self.remote_artifacts)
        return environment

    def test_pull_runs_noninteractive_encrypted_only_backup_and_verifies_download(self):
        """Reintroducing passphrase/GPG output or skipping transport verification is unsafe."""
        remote_repo = "/srv/cluster$(touch should-not-run)"
        result = subprocess.run(
            [
                str(PULL_HELPER),
                "--remote-host",
                "operator@provisioner",
                "--remote-repo",
                remote_repo,
                "--remote-output-dir",
                "/srv/backups",
                "--local-output-dir",
                str(self.local_artifacts),
                "--vault-dir",
                "/srv/vault",
                "--policy",
                "/srv/cluster/.sops.yaml",
                "--stamp",
                "20260821-130000",
            ],
            cwd=REPOSITORY_ROOT,
            env=self.environment(),
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(SENTINEL, result.stdout + result.stderr)
        ssh_calls = [json.loads(line) for line in self.ssh_calls.read_text().splitlines()]
        self.assertEqual(len(ssh_calls), 1)
        self.assertEqual(ssh_calls[0][:3], ["-T", "--", "operator@provisioner"])
        remote_command = ssh_calls[0][3]
        self.assertNotIn("$(touch should-not-run)", remote_command)
        self.assertNotIn("passphrase", remote_command)
        self.assertNotIn("gpg", remote_command)
        self.assertIn("--vault-dir /srv/vault", remote_command)
        self.assertIn("--policy /srv/cluster/.sops.yaml", remote_command)
        scp_calls = [json.loads(line) for line in self.scp_calls.read_text().splitlines()]
        self.assertEqual(len(scp_calls), 1)
        self.assertIn("glasslab-secrets-20260821-130000.tar.gz", scp_calls[0][1])
        self.assertIn("glasslab-secrets-20260821-130000.tar.gz.sha256", scp_calls[0][2])
        self.assertTrue(
            (self.local_artifacts / "glasslab-secrets-20260821-130000.tar.gz").is_file()
        )

    def test_pull_rejects_option_like_remote_host_before_ssh(self):
        """An option-shaped host must not become an ssh client flag."""
        result = subprocess.run(
            [str(PULL_HELPER), "--remote-host", "-oProxyCommand=unsafe"],
            cwd=REPOSITORY_ROOT,
            env=self.environment(),
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.ssh_calls.exists())
        self.assertFalse(self.scp_calls.exists())

    def test_pull_signal_after_first_publish_removes_pair_and_allows_retry(self):
        """A terminating signal in the link window must not reserve the backup stamp."""
        environment = self.environment()
        environment["LINK_INTERRUPT_MARKER"] = str(self.root / "link-interrupted")
        command = [
            str(PULL_HELPER),
            "--remote-host",
            "operator@provisioner",
            "--remote-repo",
            "/srv/cluster",
            "--remote-output-dir",
            "/srv/backups",
            "--local-output-dir",
            str(self.local_artifacts),
            "--stamp",
            "20260821-130000",
        ]

        interrupted = subprocess.run(
            command,
            cwd=REPOSITORY_ROOT,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )

        archive = self.local_artifacts / "glasslab-secrets-20260821-130000.tar.gz"
        checksum = archive.with_name(archive.name + ".sha256")
        self.assertNotEqual(interrupted.returncode, 0)
        self.assertFalse(archive.exists())
        self.assertFalse(checksum.exists())
        self.assertNotIn(SENTINEL, interrupted.stdout + interrupted.stderr)

        environment.pop("LINK_INTERRUPT_MARKER")
        retried = subprocess.run(
            command,
            cwd=REPOSITORY_ROOT,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(retried.returncode, 0, retried.stderr)
        self.assertTrue(archive.is_file())
        self.assertTrue(checksum.is_file())


if __name__ == "__main__":
    unittest.main()
