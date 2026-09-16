"""Security tests for the passwordless-sudo node-maintenance wrappers.

The wrappers validate and canonicalize their arguments with ``realpath`` and
re-check the canonical path before any destructive or read operation, so a
traversal-shaped argument cannot escape the intended staging area. These tests
run the extracted wrapper scripts directly (they are plain bash files under
``ansible/playbooks/files/``); the refusal paths are exercised here, and the
live installation plus the same refusal checks are verified by the playbook's
own verification tasks on real nodes.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
IMPORT_WRAPPER = REPO_ROOT / 'ansible/playbooks/files/glasslab-import-k8s-image'


def _run(wrapper: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ['bash', str(wrapper), *args],
        capture_output=True,
        text=True,
        check=False,
    )


class WrapperSyntaxTests(unittest.TestCase):
    def test_wrapper_parses_with_bash(self) -> None:
        result = _run(IMPORT_WRAPPER, '--help')
        self.assertEqual(result.returncode, 0, result.stderr)


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
