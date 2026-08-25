"""Regression coverage for digest-pinned runner_image resolution.

The release workflow for the research workspace runner must treat

    ghcr.io/<owner>/glasslab-research-workspace-runner@sha256:<64 lowercase hex>

as the authoritative contract, matching the digest-pinning rule enforced by
``services/common/schemas/workflow_registry.py`` for active workflows. These
tests lock the parser behavior so a return to mutable-tag parsing cannot
silently regress the release path.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import resolve_runner_image  # noqa: E402

RUNNER_REPOSITORY = "ghcr.io/ccny-glasslab/glasslab-research-workspace-runner"
VALID_DIGEST = "3c169671563c165e5a72e16d20daf955dfefcba7cb99d44aca47931dd093ed6a"
VALID_REF = f"{RUNNER_REPOSITORY}@sha256:{VALID_DIGEST}"
FULL_SHA = "968dcb203879aaf9ea15a1cd1497d4da83f59b8d"
MATRIX_NAME = "research-cpu"


class ParseRunnerImageTests(unittest.TestCase):
    """Unit coverage for the digest-pinned ref parser."""

    def test_accepts_digest_pinned_ref(self) -> None:
        repository, digest = resolve_runner_image.parse_runner_image(VALID_REF)
        self.assertEqual(repository, RUNNER_REPOSITORY)
        self.assertEqual(digest, f"sha256:{VALID_DIGEST}")

    def test_rejects_mutable_tag_ref(self) -> None:
        with self.assertRaises(ValueError):
            resolve_runner_image.parse_runner_image(f"{RUNNER_REPOSITORY}:0.1.0")

    def test_rejects_missing_digest_separator(self) -> None:
        with self.assertRaises(ValueError):
            resolve_runner_image.parse_runner_image(RUNNER_REPOSITORY)

    def test_rejects_non_sha256_digest_algorithm(self) -> None:
        with self.assertRaises(ValueError):
            resolve_runner_image.parse_runner_image(
                f"{RUNNER_REPOSITORY}@md5:{VALID_DIGEST}"
            )

    def test_rejects_short_digest(self) -> None:
        with self.assertRaises(ValueError):
            resolve_runner_image.parse_runner_image(
                f"{RUNNER_REPOSITORY}@sha256:{VALID_DIGEST[:63]}"
            )

    def test_rejects_non_hex_digest(self) -> None:
        with self.assertRaises(ValueError):
            resolve_runner_image.parse_runner_image(
                f"{RUNNER_REPOSITORY}@sha256:{'g' * 64}"
            )

    def test_rejects_uppercase_hex_digest(self) -> None:
        with self.assertRaises(ValueError):
            resolve_runner_image.parse_runner_image(
                f"{RUNNER_REPOSITORY}@sha256:{VALID_DIGEST.upper()}"
            )

    def test_rejects_empty_repository(self) -> None:
        with self.assertRaises(ValueError):
            resolve_runner_image.parse_runner_image(f"@sha256:{VALID_DIGEST}")


class ResolveRunnerImageScriptTests(unittest.TestCase):
    """Coverage for the script entry point used by the release workflow."""

    def _run(self, *argv: str) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = resolve_runner_image.main(list(argv))
        return code, stdout.getvalue(), stderr.getvalue()

    def _write_definition(self, runner_image: str) -> str:
        handle = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        )
        json.dump({"runner_image": runner_image}, handle)
        handle.close()
        return handle.name

    def test_valid_definition_emits_expected_outputs(self) -> None:
        definition = self._write_definition(VALID_REF)
        try:
            code, out, err = self._run(
                "--definition",
                definition,
                "--owner",
                "CCNY-GLASSLAB",
                "--image",
                "glasslab-research-workspace-runner",
                "--sha",
                FULL_SHA,
                "--matrix-name",
                MATRIX_NAME,
            )
        finally:
            Path(definition).unlink(missing_ok=True)
        self.assertEqual(code, 0, err)
        outputs = dict(line.split("=", 1) for line in out.strip().splitlines())
        self.assertEqual(outputs["image"], RUNNER_REPOSITORY)
        self.assertEqual(outputs["digest"], f"sha256:{VALID_DIGEST}")
        self.assertEqual(outputs["image_ref"], VALID_REF)
        self.assertEqual(outputs["tag"], f"sha-{FULL_SHA}-{MATRIX_NAME}")
        self.assertEqual(
            outputs["sha_ref"],
            f"{RUNNER_REPOSITORY}:sha-{FULL_SHA}-{MATRIX_NAME}",
        )

    def test_repository_mismatch_fails(self) -> None:
        definition = self._write_definition(
            "ghcr.io/other-org/glasslab-research-workspace-runner"
            f"@sha256:{VALID_DIGEST}"
        )
        try:
            code, _, err = self._run(
                "--definition",
                definition,
                "--owner",
                "ccny-glasslab",
                "--image",
                "glasslab-research-workspace-runner",
                "--sha",
                FULL_SHA,
                "--matrix-name",
                MATRIX_NAME,
            )
        finally:
            Path(definition).unlink(missing_ok=True)
        self.assertNotEqual(code, 0)
        self.assertIn("repository mismatch", err)

    def test_tag_pinned_definition_fails(self) -> None:
        definition = self._write_definition(f"{RUNNER_REPOSITORY}:0.1.0")
        try:
            code, _, err = self._run(
                "--definition",
                definition,
                "--owner",
                "ccny-glasslab",
                "--image",
                "glasslab-research-workspace-runner",
                "--sha",
                FULL_SHA,
                "--matrix-name",
                MATRIX_NAME,
            )
        finally:
            Path(definition).unlink(missing_ok=True)
        self.assertNotEqual(code, 0)
        self.assertIn("digest-pinned", err)


class MatrixDefinitionTests(unittest.TestCase):
    """The release matrix definitions must stay digest-pinned with the runner repo."""

    MATRIX_DEFINITIONS = (
        "research-workspace-cpu-v1.json",
        "benchmark-workspace-cpu-v1.json",
        "benchmark-workspace-gpu-v1.json",
    )

    def test_all_matrix_definitions_are_digest_pinned(self) -> None:
        definitions_root = REPO_ROOT / "services/workflow-registry/definitions"
        for name in self.MATRIX_DEFINITIONS:
            with self.subTest(definition=name):
                definition = json.loads((definitions_root / name).read_text())
                image_ref = definition["runner_image"]
                repository, digest = resolve_runner_image.parse_runner_image(image_ref)
                self.assertEqual(repository, RUNNER_REPOSITORY)
                self.assertTrue(digest.startswith("sha256:"))


if __name__ == "__main__":
    unittest.main()