"""Behavioral tests for secret-bearing process boundaries."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
GHCR_HELPER = REPOSITORY_ROOT / "scripts" / "create-ghcr-pull-secret.sh"
POSTGRES_IMPORTER = (
    REPOSITORY_ROOT
    / "services"
    / "workflow-api"
    / "scripts"
    / "import-json-store-to-postgres.py"
)
POSTGRES_DSN_ENV = "GLASSLAB_WORKFLOW_API_STORE_POSTGRES_DSN"


class GhcrPullSecretBoundaryTests(unittest.TestCase):
    """The GHCR token may reach a private file, but never a child argv."""

    def run_helper(
        self,
        *,
        token_environment: str | None = None,
        stdin: str | None = None,
    ) -> tuple[subprocess.CompletedProcess[str], list[dict[str, object]]]:
        with tempfile.TemporaryDirectory() as directory:
            fixture_root = Path(directory)
            calls_path = fixture_root / "kubectl-calls.jsonl"
            kubectl = fixture_root / "kubectl"
            kubectl.write_text(
                """#!/usr/bin/env python3
import json
import os
import stat
import sys
from pathlib import Path

record = {
    "argv": sys.argv[1:],
    "ghcr_token": os.environ.get("GHCR_TOKEN"),
}
for argument in sys.argv[1:]:
    prefix = "--from-file=.dockerconfigjson="
    if argument.startswith(prefix):
        config_path = Path(argument[len(prefix):])
        record["config"] = config_path.read_text(encoding="utf-8")
        record["config_mode"] = stat.S_IMODE(config_path.stat().st_mode)
        record["directory_mode"] = stat.S_IMODE(config_path.parent.stat().st_mode)
        record["config_path"] = str(config_path)
with Path(os.environ["KUBECTL_CALLS"]).open("a", encoding="utf-8") as stream:
    stream.write(json.dumps(record) + "\\n")
if "create" in sys.argv[1:]:
    print("apiVersion: v1")
    print("kind: Secret")
""",
                encoding="utf-8",
            )
            kubectl.chmod(0o755)

            environment = os.environ.copy()
            environment["KUBECTL"] = str(kubectl)
            environment["KUBECTL_CALLS"] = str(calls_path)
            environment["GHCR_USERNAME"] = "fixture-user"
            if token_environment is None:
                environment.pop("GHCR_TOKEN", None)
            else:
                environment["GHCR_TOKEN"] = token_environment

            result = subprocess.run(
                ["bash", str(GHCR_HELPER)],
                cwd=REPOSITORY_ROOT,
                env=environment,
                input=stdin,
                check=False,
                capture_output=True,
                text=True,
            )
            calls = (
                [json.loads(line) for line in calls_path.read_text(encoding="utf-8").splitlines()]
                if calls_path.exists()
                else []
            )
            for call in calls:
                config_path = call.get("config_path")
                if isinstance(config_path, str):
                    call["config_was_removed"] = not Path(config_path).exists()
            return result, calls

    def assert_token_stayed_out_of_process_boundary(
        self,
        token: str,
        result: subprocess.CompletedProcess[str],
        calls: list[dict[str, object]],
    ) -> None:
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(token, result.stdout + result.stderr)
        self.assertEqual(len(calls), 2, calls)
        for call in calls:
            self.assertNotIn(token, json.dumps(call["argv"]))
            self.assertNotEqual(call["ghcr_token"], token)

        create_call = next(call for call in calls if "create" in call["argv"])
        config = json.loads(str(create_call["config"]))
        self.assertEqual(config["auths"]["ghcr.io"]["username"], "fixture-user")
        self.assertEqual(config["auths"]["ghcr.io"]["password"], token)
        self.assertEqual(create_call["directory_mode"], 0o700)
        self.assertEqual(create_call["config_mode"], 0o600)
        self.assertTrue(create_call["config_was_removed"])

    def test_environment_token_uses_private_docker_config_not_kubectl_argv(self):
        """Removing the private-file path would put the environment token in kubectl argv."""
        token = "ghcr-environment-token-sentinel"
        result, calls = self.run_helper(token_environment=token)

        self.assert_token_stayed_out_of_process_boundary(token, result, calls)

    def test_stdin_token_uses_private_docker_config_not_kubectl_argv(self):
        """Dropping stdin support would force operators back to argument-bearing workarounds."""
        token = "ghcr-stdin-token-sentinel"
        result, calls = self.run_helper(stdin=token + "\n")

        self.assert_token_stayed_out_of_process_boundary(token, result, calls)

    def test_missing_token_fails_before_kubectl(self):
        """Absent token input must not create or apply an empty registry Secret."""
        result, calls = self.run_helper(stdin="")

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(calls, [])


class PostgresImporterBoundaryTests(unittest.TestCase):
    """The importer accepts non-argv DSN channels and scrubs inherited state."""

    def run_importer(
        self,
        extra_arguments: list[str] | None = None,
        *,
        dsn_environment: str | None = None,
        dsn_fd_contents: str | None = None,
        driver_echoes_dsn: bool = False,
    ) -> tuple[subprocess.CompletedProcess[str], list[dict[str, object]]]:
        with tempfile.TemporaryDirectory() as directory:
            fixture_root = Path(directory)
            json_path = fixture_root / "run-store.json"
            json_path.write_text('{"runs": []}\n', encoding="utf-8")
            records_path = fixture_root / "psycopg-records.jsonl"
            fake_module_root = fixture_root / "fake-module"
            fake_module_root.mkdir()
            (fake_module_root / "psycopg.py").write_text(
                """import json
import os
import sys
from pathlib import Path

def record(event, **details):
    payload = {
        "event": event,
        "argv": sys.argv[1:],
        "dsn_environment": os.environ.get("GLASSLAB_WORKFLOW_API_STORE_POSTGRES_DSN"),
        **details,
    }
    with Path(os.environ["PSYCOPG_RECORDS"]).open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(payload) + "\\n")

class Cursor:
    def __enter__(self):
        return self
    def __exit__(self, *_args):
        return False
    def execute(self, *_args):
        return None

class Connection:
    def __enter__(self):
        return self
    def __exit__(self, *_args):
        return False
    def cursor(self):
        return Cursor()
    def commit(self):
        return None

class Error(Exception):
    pass

def connect(dsn):
    record("connect", dsn=dsn)
    if os.environ.get("PSYCOPG_ECHO_DSN") == "1":
        raise Error(dsn)
    return Connection()
""",
                encoding="utf-8",
            )
            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(fake_module_root)
            environment["PSYCOPG_RECORDS"] = str(records_path)
            environment["PSYCOPG_ECHO_DSN"] = "1" if driver_echoes_dsn else "0"
            if dsn_environment is None:
                environment.pop(POSTGRES_DSN_ENV, None)
            else:
                environment[POSTGRES_DSN_ENV] = dsn_environment

            arguments = [
                sys.executable,
                str(POSTGRES_IMPORTER),
                "--json-path",
                str(json_path),
                *(extra_arguments or []),
            ]
            read_fd: int | None = None
            pass_fds: tuple[int, ...] = ()
            if dsn_fd_contents is not None:
                dsn_path = fixture_root / "postgres-dsn"
                dsn_path.write_text(dsn_fd_contents, encoding="utf-8")
                dsn_path.chmod(0o600)
                read_fd = os.open(dsn_path, os.O_RDONLY)
                arguments.extend(["--dsn-fd", str(read_fd)])
                pass_fds = (read_fd,)

            try:
                result = subprocess.run(
                    arguments,
                    cwd=REPOSITORY_ROOT,
                    env=environment,
                    pass_fds=pass_fds,
                    check=False,
                    capture_output=True,
                    text=True,
                )
            finally:
                if read_fd is not None:
                    os.close(read_fd)

            records = (
                [json.loads(line) for line in records_path.read_text(encoding="utf-8").splitlines()]
                if records_path.exists()
                else []
            )
            return result, records

    def assert_dsn_stayed_out_of_process_boundary(
        self,
        dsn: str,
        result: subprocess.CompletedProcess[str],
        records: list[dict[str, object]],
    ) -> None:
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(dsn, result.stdout + result.stderr)
        self.assertEqual(len(records), 1, records)
        self.assertEqual(records[0]["dsn"], dsn)
        self.assertNotIn(dsn, json.dumps(records[0]["argv"]))
        self.assertIsNone(records[0]["dsn_environment"])

    def test_environment_dsn_is_scrubbed_before_database_driver_use(self):
        """Passing the DSN in importer argv or retaining it in the environment is unsafe."""
        dsn = "postgresql" + "://fixture:environment-sentinel@db.invalid/workflow"
        result, records = self.run_importer(dsn_environment=dsn)

        self.assert_dsn_stayed_out_of_process_boundary(dsn, result, records)

    def test_protected_file_descriptor_supplies_dsn_without_exposing_it(self):
        """Removing fd input would leave no non-environment channel for the DSN."""
        dsn = "postgresql" + "://fixture:fd-sentinel@db.invalid/workflow"
        result, records = self.run_importer(dsn_fd_contents=dsn + "\n")

        self.assert_dsn_stayed_out_of_process_boundary(dsn, result, records)

    def test_legacy_dsn_argument_is_rejected_without_echoing_value(self):
        """Re-enabling --dsn would restore the original process-list disclosure."""
        dsn = "postgresql" + "://fixture:argv-sentinel@db.invalid/workflow"
        result, records = self.run_importer(["--dsn", dsn])

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(records, [])
        self.assertNotIn(dsn, result.stdout + result.stderr)

    def test_database_driver_failure_does_not_echo_dsn(self):
        """A driver exception containing connection input must be reported generically."""
        dsn = "postgresql" + "://fixture:driver-error-sentinel@db.invalid/workflow"
        result, records = self.run_importer(
            dsn_environment=dsn,
            driver_echoes_dsn=True,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(records), 1, records)
        self.assertNotIn(dsn, result.stdout + result.stderr)

    def test_missing_dsn_fails_before_database_driver_use(self):
        """Absent DSN input must not attempt a connection with an implicit default."""
        result, records = self.run_importer()

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(records, [])


if __name__ == "__main__":
    unittest.main()
