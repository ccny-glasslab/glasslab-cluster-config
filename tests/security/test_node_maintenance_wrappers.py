"""Security tests for the passwordless-sudo node-maintenance wrappers.

Issue #262: the Titanic dataset sync wrapper validated its staging path by
prefix glob only, so a traversal-shaped argument could reach the subsequent
root ``rm -rf`` / root ``install`` operations (arbitrary-directory deletion
plus root-file read through ``$stage/file``). The wrappers now canonicalize
their arguments with ``realpath`` and re-check the canonical path before
any destructive or read operation.

These tests run the extracted wrapper scripts directly (they are plain bash
files under ``ansible/playbooks/files/``); the traversal refusal paths are
exercised here, and the live installation plus the same refusal checks are
verified by the playbook's own verification tasks on real nodes.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TITANIC_WRAPPER = REPO_ROOT / 'ansible/playbooks/files/glasslab-install-titanic-dataset'
IMPORT_WRAPPER = REPO_ROOT / 'ansible/playbooks/files/glasslab-import-k8s-image'
EXPECTED_DATASET = '/var/lib/glasslab-agent/datasets/titanic'
STAGING_PREFIX = '/tmp/glasslab-titanic-sync-'


def _run(wrapper: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ['bash', str(wrapper), *args],
        capture_output=True,
        text=True,
        check=False,
    )


class WrapperSyntaxTests(unittest.TestCase):
    def test_wrappers_parse_with_bash(self) -> None:
        for wrapper in (TITANIC_WRAPPER, IMPORT_WRAPPER):
            result = _run(wrapper, '--help')
            self.assertEqual(result.returncode, 0, result.stderr)


class TitanicWrapperTraversalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(
            f'{STAGING_PREFIX}test-{uuid.uuid4().hex[:10]}'
        )
        self.stage = self.root / 'stage'
        self.stage.mkdir(parents=True)
        (self.stage / 'train.csv').write_text('x\n')

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.root, ignore_errors=True)

    def test_dotdot_traversal_stage_refused(self) -> None:
        # stage is two levels below /, so three ".." reach /var.
        traversal = f'{self.stage}/../../../var'
        result = _run(TITANIC_WRAPPER, traversal, EXPECTED_DATASET)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('refusing', result.stderr)
        self.assertIn('resolves to /var', result.stderr)

    def test_symlink_escape_stage_refused(self) -> None:
        link = self.root / 'escape'
        os.symlink('/var', link)
        result = _run(TITANIC_WRAPPER, str(link), EXPECTED_DATASET)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('refusing', result.stderr)
        self.assertIn('resolves to /var', result.stderr)

    def test_sync_root_itself_refused(self) -> None:
        # The bare prefix root (no run-specific suffix) must not be
        # deletable; create it so realpath -e resolves it.
        prefix_root = Path(STAGING_PREFIX.rstrip('/'))
        prefix_root.mkdir(exist_ok=True)
        try:
            result = _run(TITANIC_WRAPPER, str(prefix_root), EXPECTED_DATASET)
        finally:
            prefix_root.rmdir()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('sync root itself', result.stderr)

    def test_non_prefixed_stage_refused(self) -> None:
        with tempfile.TemporaryDirectory(prefix='unrelated-') as other:
            result = _run(TITANIC_WRAPPER, other, EXPECTED_DATASET)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('refusing', result.stderr)

    def test_missing_stage_refused(self) -> None:
        missing = f'{STAGING_PREFIX}missing-{uuid.uuid4().hex[:8]}'
        result = _run(TITANIC_WRAPPER, missing, EXPECTED_DATASET)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('unresolvable', result.stderr)

    def test_dataset_dir_traversal_refused(self) -> None:
        traversal_dataset = (
            '/var/lib/glasslab-agent/datasets/titanic/../../titanic-evil'
        )
        result = _run(TITANIC_WRAPPER, str(self.stage), traversal_dataset)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('refusing unexpected dataset path', result.stderr)


class ImportWrapperBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix='glasslab-import-test-'))

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.dir, ignore_errors=True)

    def test_outside_tmp_archive_refused(self) -> None:
        result = _run(IMPORT_WRAPPER, '/etc/hostname')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('refusing archive outside /tmp', result.stderr)

    def test_symlink_into_etc_refused(self) -> None:
        link = self.dir / 'escaped.tar'
        os.symlink('/etc/hostname', link)
        result = _run(IMPORT_WRAPPER, str(link))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('refusing archive outside /tmp', result.stderr)
        self.assertIn('resolves to /etc/hostname', result.stderr)

    def test_dotdot_escape_refused(self) -> None:
        probe_dir = self.dir / 'probe'
        probe_dir.mkdir()
        # probe is two levels below /, so three ".." reach /etc/hostname.
        traversal = f'{probe_dir}/../../../etc/hostname'
        result = _run(IMPORT_WRAPPER, traversal)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('refusing archive outside /tmp', result.stderr)

    def test_missing_archive_refused(self) -> None:
        missing = f'{self.dir}/does-not-exist.tar'
        result = _run(IMPORT_WRAPPER, missing)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('unresolvable', result.stderr)

    def test_legit_tmp_archive_passes_validation(self) -> None:
        # A real /tmp archive passes validation and proceeds to ctr, which
        # is not installed in test environments; assert it was NOT refused.
        archive = Path(f'/tmp/glasslab-import-ok-{uuid.uuid4().hex[:8]}.tar')
        archive.write_bytes(b'')
        try:
            result = _run(IMPORT_WRAPPER, str(archive))
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn('refusing', result.stderr + result.stdout)
        finally:
            archive.unlink(missing_ok=True)


if __name__ == '__main__':
    unittest.main()