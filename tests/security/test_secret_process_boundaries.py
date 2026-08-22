"""Behavioral tests for secret-bearing process boundaries."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
VLLM_DEPLOYMENT = REPOSITORY_ROOT / "kubeadm" / "agent-stack" / "11-vllm-deployment.yaml"
UPLOAD_CIFAR100 = REPOSITORY_ROOT / "scripts" / "upload-cifar100.sh"
GHCR_HELPER = REPOSITORY_ROOT / "scripts" / "create-ghcr-pull-secret.sh"
POSTGRES_IMPORTER = (
    REPOSITORY_ROOT
    / "services"
    / "workflow-api"
    / "scripts"
    / "import-json-store-to-postgres.py"
)
POSTGRES_DSN_ENV = "GLASSLAB_WORKFLOW_API_STORE_POSTGRES_DSN"
RESEARCH_POSTGRES_IMPORTER = (
    REPOSITORY_ROOT
    / "services"
    / "research-orchestrator"
    / "scripts"
    / "import-sqlite-store-to-postgres.py"
)
RESEARCH_POSTGRES_DSN_ENV = "GLASSLAB_ORCHESTRATOR_STORE_POSTGRES_DSN"


class VllmPodBoundaryTests(unittest.TestCase):
    """The vLLM key is inherited from the Secret environment, never Python argv."""

    def test_manifest_launches_vllm_without_expanding_api_key_into_argv(self):
        """Restoring --api-key expansion would disclose the key through the pod process list."""
        deployment = yaml.safe_load(VLLM_DEPLOYMENT.read_text(encoding="utf-8"))
        container = deployment["spec"]["template"]["spec"]["containers"][0]
        command = container["command"]
        sentinel = "vllm-pod-argv-sentinel"

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            records = root / "records.jsonl"
            fake_python = root / "python3"
            fake_python.write_text(
                f"""#!{os.path.realpath(sys.executable)}
import json
import os
import sys
from pathlib import Path

Path(os.environ["VLLM_RECORDS"]).write_text(json.dumps({{
    "argv": sys.argv[1:],
    "api_key_environment": os.environ.get("VLLM_API_KEY"),
}}), encoding="utf-8")
""",
                encoding="utf-8",
            )
            fake_python.chmod(0o755)
            environment = os.environ.copy()
            environment.update(
                {
                    "PATH": f"{root}:{environment['PATH']}",
                    "MODEL_NAME": "fixture-model",
                    "MAX_MODEL_LEN": "128",
                    "VLLM_API_KEY": sentinel,
                    "VLLM_RECORDS": str(records),
                }
            )
            result = subprocess.run(
                command,
                cwd=REPOSITORY_ROOT,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )
            record = json.loads(records.read_text(encoding="utf-8"))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(sentinel, json.dumps(record["argv"]))
        self.assertEqual(record["api_key_environment"], sentinel)


class CifarUploadBoundaryTests(unittest.TestCase):
    """MinIO credentials use a scoped environment boundary, never defaults or argv."""

    def run_upload(
        self,
        *,
        access_key: str | None,
        secret_key: str | None,
        extra_arguments: list[str] | None = None,
        xtrace: bool = False,
    ) -> tuple[subprocess.CompletedProcess[str], list[dict[str, object]]]:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dataset = root / "dataset"
            (dataset / "train").mkdir(parents=True)
            (dataset / "test").mkdir()
            records = root / "mc-records.jsonl"
            fake_mc = root / "mc"
            fake_mc.write_text(
                f"""#!{os.path.realpath(sys.executable)}
import json
import os
import sys
from pathlib import Path

record = {{
    "argv": sys.argv[1:],
    "minio_input_environment": {{
        name: os.environ[name]
        for name in ("MINIO_ACCESS_KEY", "MINIO_SECRET_KEY")
        if name in os.environ
    }},
    "mc_host": os.environ.get("MC_HOST_glasslab"),
}}
with Path(os.environ["MC_RECORDS"]).open("a", encoding="utf-8") as stream:
    stream.write(json.dumps(record) + "\\n")
""",
                encoding="utf-8",
            )
            fake_mc.chmod(0o755)
            environment = os.environ.copy()
            environment["PATH"] = f"{root}:{environment['PATH']}"
            environment["MC_RECORDS"] = str(records)
            environment["DATASET_PATH"] = str(dataset)
            environment["MINIO_ENDPOINT"] = "minio.invalid:9000"
            if access_key is None:
                environment.pop("MINIO_ACCESS_KEY", None)
            else:
                environment["MINIO_ACCESS_KEY"] = access_key
            if secret_key is None:
                environment.pop("MINIO_SECRET_KEY", None)
            else:
                environment["MINIO_SECRET_KEY"] = secret_key

            command = ["bash", *(["-x"] if xtrace else []), str(UPLOAD_CIFAR100)]
            command.extend(extra_arguments or [])
            result = subprocess.run(
                command,
                cwd=REPOSITORY_ROOT,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )
            calls = (
                [json.loads(line) for line in records.read_text(encoding="utf-8").splitlines()]
                if records.exists()
                else []
            )
            return result, calls

    def test_environment_credentials_never_reach_mc_argv_or_inherited_input_names(self):
        """Replacing the scoped MC_HOST boundary would expose credentials to child argv."""
        access_key = "minio-access-sentinel"
        secret_key = "minio-secret-sentinel"
        result, calls = self.run_upload(access_key=access_key, secret_key=secret_key)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(calls)
        self.assertNotIn(access_key, result.stdout + result.stderr)
        self.assertNotIn(secret_key, result.stdout + result.stderr)
        for call in calls:
            self.assertNotIn(access_key, json.dumps(call["argv"]))
            self.assertNotIn(secret_key, json.dumps(call["argv"]))
            self.assertEqual(call["minio_input_environment"], {})
            self.assertIn(access_key, str(call["mc_host"]))
            self.assertIn(secret_key, str(call["mc_host"]))

    def test_missing_minio_credentials_fails_before_mc(self):
        """The uploader must not restore default MinIO administrator credentials."""
        result, calls = self.run_upload(access_key=None, secret_key=None)

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(calls, [])

    def test_legacy_secret_arguments_are_rejected_without_echoing_values(self):
        """Compatibility flags must not keep secret-bearing command lines usable."""
        sentinel = "legacy-minio-argv-sentinel"
        result, calls = self.run_upload(
            access_key="safe-environment-access",
            secret_key="safe-environment-secret",
            extra_arguments=["--secret-key", sentinel],
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(calls, [])
        self.assertNotIn(sentinel, result.stdout + result.stderr)

    def test_inherited_xtrace_is_disabled_before_minio_secret_expansion(self):
        """An operator's bash -x setting must not print uploader credentials."""
        access_key = "minio-xtrace-access-sentinel"
        secret_key = "minio-xtrace-secret-sentinel"
        result, calls = self.run_upload(
            access_key=access_key,
            secret_key=secret_key,
            xtrace=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(calls)
        self.assertNotIn(access_key, result.stdout + result.stderr)
        self.assertNotIn(secret_key, result.stdout + result.stderr)


class RoutineSecurityGateTests(unittest.TestCase):
    """The expensive secret boundary suites must run in routine local and CI gates."""

    def test_default_pre_push_executes_both_secret_boundary_modules(self):
        """Dropping either unittest module from the default gate would hide regressions."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            records = root / "python-calls.jsonl"
            fake_python = root / "python3"
            fake_python.write_text(
                f"""#!{os.path.realpath(sys.executable)}
import json
import os
import sys
from pathlib import Path
with Path(os.environ["PYTHON_CALLS"]).open("a", encoding="utf-8") as stream:
    stream.write(json.dumps(sys.argv[1:]) + "\\n")
""",
                encoding="utf-8",
            )
            fake_python.chmod(0o755)
            fake_pytest = root / "pytest"
            fake_pytest.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
            fake_pytest.chmod(0o755)
            environment = os.environ.copy()
            environment["PATH"] = f"{root}:{environment['PATH']}"
            environment["PYTHON_CALLS"] = str(records)
            result = subprocess.run(
                [str(REPOSITORY_ROOT / "scripts" / "check-before-push.sh"), "--default"],
                cwd=REPOSITORY_ROOT,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )
            calls = [json.loads(line) for line in records.read_text().splitlines()]

        flattened = "\n".join(" ".join(call) for call in calls)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("tests.security.test_secret_process_boundaries", flattened)
        self.assertIn("tests.security.test_secret_backup_restore", flattened)

    def test_config_ci_executes_both_secret_boundary_modules(self):
        """The CI config gate must mirror the local secret boundary coverage."""
        workflow = yaml.safe_load(
            (REPOSITORY_ROOT / ".github" / "workflows" / "ci-configs.yml").read_text(
                encoding="utf-8"
            )
        )
        commands = "\n".join(
            str(step.get("run", ""))
            for job in workflow["jobs"].values()
            for step in job.get("steps", [])
        )

        self.assertIn("tests.security.test_secret_process_boundaries", commands)
        self.assertIn("tests.security.test_secret_backup_restore", commands)


class GhcrPullSecretBoundaryTests(unittest.TestCase):
    """The GHCR token may reach a private file, but never a child argv."""

    def run_helper(
        self,
        *,
        token_environment: str | None = None,
        stdin: str | None = None,
        xtrace: bool = False,
        preexported_registry_token: str | None = None,
    ) -> tuple[
        subprocess.CompletedProcess[str],
        list[dict[str, object]],
        list[dict[str, object]],
    ]:
        with tempfile.TemporaryDirectory() as directory:
            fixture_root = Path(directory)
            calls_path = fixture_root / "kubectl-calls.jsonl"
            generator_calls_path = fixture_root / "generator-calls.jsonl"
            kubectl = fixture_root / "kubectl"
            kubectl.write_text(
                """#!PYTHON
import json
import os
import stat
import sys
from pathlib import Path

record = {
    "argv": sys.argv[1:],
    "secret_environment": {
        name: os.environ[name]
        for name in ("GHCR_TOKEN", "REGISTRY_TOKEN")
        if name in os.environ
    },
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
""".replace("#!PYTHON", f"#!{os.path.realpath(sys.executable)}"),
                encoding="utf-8",
            )
            kubectl.chmod(0o755)
            real_python = os.path.realpath(sys.executable)
            python_wrapper = fixture_root / "python3"
            python_wrapper.write_text(
                f"""#!{real_python}
import json
import os
import sys
from pathlib import Path

record = {{
    "argv": sys.argv[1:],
    "secret_environment": {{
        name: os.environ[name]
        for name in ("GHCR_TOKEN", "REGISTRY_TOKEN")
        if name in os.environ
    }},
}}
with Path(os.environ["GENERATOR_CALLS"]).open("a", encoding="utf-8") as stream:
    stream.write(json.dumps(record) + "\\n")
os.execv({real_python!r}, [{real_python!r}, *sys.argv[1:]])
""",
                encoding="utf-8",
            )
            python_wrapper.chmod(0o755)

            environment = os.environ.copy()
            environment["PATH"] = f"{fixture_root}:{environment['PATH']}"
            environment["KUBECTL"] = str(kubectl)
            environment["KUBECTL_CALLS"] = str(calls_path)
            environment["GENERATOR_CALLS"] = str(generator_calls_path)
            environment["GHCR_USERNAME"] = "fixture-user"
            if token_environment is None:
                environment.pop("GHCR_TOKEN", None)
            else:
                environment["GHCR_TOKEN"] = token_environment
            if preexported_registry_token is None:
                environment.pop("REGISTRY_TOKEN", None)
            else:
                environment["REGISTRY_TOKEN"] = preexported_registry_token

            command = ["bash"]
            if xtrace:
                command.append("-x")
            command.append(str(GHCR_HELPER))
            result = subprocess.run(
                command,
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
            generator_calls = (
                [
                    json.loads(line)
                    for line in generator_calls_path.read_text(encoding="utf-8").splitlines()
                ]
                if generator_calls_path.exists()
                else []
            )
            for call in calls:
                config_path = call.get("config_path")
                if isinstance(config_path, str):
                    call["config_was_removed"] = not Path(config_path).exists()
            return result, calls, generator_calls

    def assert_token_stayed_out_of_process_boundary(
        self,
        token: str,
        result: subprocess.CompletedProcess[str],
        calls: list[dict[str, object]],
        generator_calls: list[dict[str, object]],
    ) -> None:
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(token, result.stdout + result.stderr)
        self.assertEqual(len(calls), 2, calls)
        self.assertEqual(len(generator_calls), 1, generator_calls)
        for call in [*generator_calls, *calls]:
            self.assertNotIn(token, json.dumps(call["argv"]))
            self.assertEqual(call["secret_environment"], {})

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
        result, calls, generator_calls = self.run_helper(token_environment=token)

        self.assert_token_stayed_out_of_process_boundary(token, result, calls, generator_calls)

    def test_stdin_token_uses_private_docker_config_not_kubectl_argv(self):
        """Dropping stdin support would force operators back to argument-bearing workarounds."""
        token = "ghcr-stdin-token-sentinel"
        result, calls, generator_calls = self.run_helper(stdin=token + "\n")

        self.assert_token_stayed_out_of_process_boundary(token, result, calls, generator_calls)

    def test_xtrace_never_prints_token_expansions(self):
        """Shell xtrace must be disabled across every token-bearing expansion."""
        token = "ghcr-xtrace-token-sentinel"
        result, calls, generator_calls = self.run_helper(
            token_environment=token,
            xtrace=True,
        )

        self.assert_token_stayed_out_of_process_boundary(token, result, calls, generator_calls)

    def test_preexported_internal_variable_is_absent_from_every_child_environment(self):
        """An inherited export attribute must not carry the internal token into children."""
        token = "ghcr-inherited-export-token-sentinel"
        result, calls, generator_calls = self.run_helper(
            token_environment=token,
            preexported_registry_token="preexisting-registry-token-sentinel",
        )

        self.assert_token_stayed_out_of_process_boundary(token, result, calls, generator_calls)

    def test_missing_token_fails_before_kubectl(self):
        """Absent token input must not create or apply an empty registry Secret."""
        result, calls, generator_calls = self.run_helper(stdin="")

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(calls, [])
        self.assertEqual(generator_calls, [])


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


class ResearchPostgresImporterBoundaryTests(unittest.TestCase):
    """The research SQLite importer follows the same non-argv DSN contract."""

    def run_importer(
        self,
        *,
        dsn_environment: str | None = None,
        dsn_fd_contents: str | None = None,
        extra_arguments: list[str] | None = None,
        driver_echoes_dsn: bool = False,
    ) -> tuple[subprocess.CompletedProcess[str], list[dict[str, object]]]:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "orchestrator.db"
            connection = sqlite3.connect(database)
            connection.close()
            records = root / "records.jsonl"
            modules = root / "modules"
            modules.mkdir()
            (modules / "sitecustomize.py").write_text(
                """import contextlib
import json
import os
import sys
import types
from pathlib import Path

def record(event, **details):
    payload = {
        "event": event,
        "argv": sys.argv[1:],
        "dsn_environment": os.environ.get("GLASSLAB_ORCHESTRATOR_STORE_POSTGRES_DSN"),
        **details,
    }
    with Path(os.environ["RESEARCH_IMPORT_RECORDS"]).open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(payload) + "\\n")

class Result:
    def fetchone(self):
        return {"count": 0}

class Connection:
    def execute(self, *_args):
        return Result()

class PostgresStore:
    def __init__(self, dsn):
        record("connect", dsn=dsn)
        if os.environ.get("RESEARCH_IMPORT_ECHO_DSN") == "1":
            raise RuntimeError(dsn)
    @contextlib.contextmanager
    def transaction(self):
        yield Connection()

app = types.ModuleType("app")
app.__path__ = []
postgres_store = types.ModuleType("app.postgres_store")
postgres_store.PostgresStore = PostgresStore
app.postgres_store = postgres_store
sys.modules["app"] = app
sys.modules["app.postgres_store"] = postgres_store

psycopg = types.ModuleType("psycopg")
psycopg_types = types.ModuleType("psycopg.types")
psycopg_json = types.ModuleType("psycopg.types.json")
psycopg_json.Jsonb = lambda value: value
psycopg.types = psycopg_types
psycopg_types.json = psycopg_json
sys.modules["psycopg"] = psycopg
sys.modules["psycopg.types"] = psycopg_types
sys.modules["psycopg.types.json"] = psycopg_json
""",
                encoding="utf-8",
            )
            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(modules)
            environment["RESEARCH_IMPORT_RECORDS"] = str(records)
            environment["RESEARCH_IMPORT_ECHO_DSN"] = "1" if driver_echoes_dsn else "0"
            if dsn_environment is None:
                environment.pop(RESEARCH_POSTGRES_DSN_ENV, None)
            else:
                environment[RESEARCH_POSTGRES_DSN_ENV] = dsn_environment

            arguments = [
                sys.executable,
                str(RESEARCH_POSTGRES_IMPORTER),
                "--sqlite-path",
                str(database),
                "--apply",
                *(extra_arguments or []),
            ]
            read_fd: int | None = None
            pass_fds: tuple[int, ...] = ()
            if dsn_fd_contents is not None:
                dsn_path = root / "postgres-dsn"
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
            captured = (
                [json.loads(line) for line in records.read_text(encoding="utf-8").splitlines()]
                if records.exists()
                else []
            )
            return result, captured

    def assert_safe_boundary(
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

    def test_environment_dsn_is_scrubbed_before_store_construction(self):
        """The migration DSN may enter through env but must not remain inherited."""
        dsn = "postgresql" + "://fixture:research-env@db.invalid/orchestrator"
        result, records = self.run_importer(dsn_environment=dsn)

        self.assert_safe_boundary(dsn, result, records)

    def test_protected_file_descriptor_supplies_research_import_dsn(self):
        """A private descriptor gives operators a non-environment safe input mode."""
        dsn = "postgresql" + "://fixture:research-fd@db.invalid/orchestrator"
        result, records = self.run_importer(dsn_fd_contents=dsn + "\n")

        self.assert_safe_boundary(dsn, result, records)

    def test_legacy_postgres_dsn_argument_is_rejected_without_echo(self):
        """The historical --postgres-dsn process-list exposure must stay disabled."""
        dsn = "postgresql" + "://fixture:research-argv@db.invalid/orchestrator"
        result, records = self.run_importer(extra_arguments=["--postgres-dsn", dsn])

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(records, [])
        self.assertNotIn(dsn, result.stdout + result.stderr)

    def test_store_failure_cannot_echo_research_import_dsn(self):
        """A dependency exception containing the DSN must become a generic error."""
        dsn = "postgresql" + "://fixture:research-error@db.invalid/orchestrator"
        result, records = self.run_importer(
            dsn_environment=dsn,
            driver_echoes_dsn=True,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(records), 1, records)
        self.assertNotIn(dsn, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
