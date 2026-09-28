"""Re-exec coverage for ``rollout-research-services.sh --sync`` (issue #610).

``--sync`` fast-forwards the checkout mid-run, but bash has already parsed the
old function bodies from the old file, so the rest of the run keeps using the
OLD script logic. On 2026-09-27 the sidecar fix merged, ``--sync`` advanced the
checkout, and the run still performed the old single-container ``set image``.

This test locks the contract: after ``--sync`` advances the checkout the script
re-executes itself from the newly checked-out file, so the run uses the new
logic. It is exercised end-to-end with a fake ``git`` first on ``PATH`` that
advances ``HEAD`` and rewrites the script on the first ``merge --ff-only``, and
a fake ``kubectl`` injected through the ``KUBECTL`` env var the script honors.

The fake rewrite is an atomic rename, exactly like a real checkout, so the
running bash keeps reading the old inode while the re-exec opens the new file.
The second run's merge is a no-op (``HEAD`` unchanged), so a guarded re-exec
terminates; an unconditional re-exec would loop and hit the subprocess timeout.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ROLLOUT = REPO_ROOT / "scripts/rollout-research-services.sh"
CHECKER = REPO_ROOT / "scripts/check-secret-permissions.sh"

OLD_SHA = "0" * 40
NEW_SHA = "1" * 40
NEW_MARKER = "NEW-SCRIPT-LOGIC"
REEXEC_NOTICE = "re-executing with the newly checked-out script"

# Fake git. ``rev-parse HEAD`` reads the state file; the first
# ``merge --ff-only origin/main`` atomically replaces the script under test with
# the "v2" body and advances HEAD, mimicking a checkout that moved. Later merge
# calls are no-ops, so a correctly guarded re-exec terminates.
FAKE_GIT = r"""#!/usr/bin/env bash
set -euo pipefail
state="${FAKE_GIT_STATE:?}"
new="${FAKE_GIT_NEW_SHA:?}"
script="${FAKE_GIT_SCRIPT:?}"
merge_flag="${FAKE_GIT_MERGE_FLAG:?}"
v2="${FAKE_GIT_V2_BODY:?}"
case "${1:-}" in
  status)
    # Clean tracked tree (the script passes --untracked-files=no).
    exit 0
    ;;
  rev-parse)
    cat "$state"
    exit 0
    ;;
  fetch|checkout)
    exit 0
    ;;
  merge)
    if [[ ! -f "$merge_flag" ]]; then
      : > "$merge_flag"
      tmp="$(mktemp "${script}.XXXXXX")"
      cat "$v2" > "$tmp"
      chmod 0755 "$tmp"
      mv -f "$tmp" "$script"
      printf '%s\n' "$new" > "$state"
    fi
    exit 0
    ;;
esac
exit 0
"""

FAKE_KUBECTL = r"""#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == "apply" ]]; then
  cat >/dev/null
fi
exit 0
"""

# The "newly checked-out" script logic. It carries the same guarded re-exec a
# fixed script would: HEAD does not move on the second run, so it does not
# re-exec and instead prints the marker that proves the new body ran.
FAKE_V2_BODY = r"""#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SYNC=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --sync) SYNC=true; shift ;;
    --service|--tag) shift 2 ;;
    --wait-for-image|--no-wait-for-image|--skip-smoke|--skip-image-prune) shift ;;
    *) shift ;;
  esac
done
if [[ "$SYNC" == true ]]; then
  SYNC_BEFORE="$(git rev-parse HEAD)"
  git fetch origin main
  git checkout main
  git merge --ff-only origin/main
  SYNC_AFTER="$(git rev-parse HEAD)"
  if [[ "$SYNC_BEFORE" != "$SYNC_AFTER" ]]; then
    exec "$ROOT_DIR/scripts/rollout-research-services.sh" "$@"
  fi
fi
printf '%s\n' "NEW-SCRIPT-LOGIC"
"""


class RolloutSyncReexecTests(unittest.TestCase):
    """``--sync`` that advances the checkout must re-execute the new script."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.scripts = self.root / "scripts"
        self.scripts.mkdir()
        for script in (ROLLOUT, CHECKER):
            shutil.copy2(script, self.scripts / script.name)
        self.rollout = self.scripts / ROLLOUT.name

        self.bindir = self.root / "bin"
        self.bindir.mkdir()
        self.git = self.bindir / "git"
        self.git.write_text(FAKE_GIT, encoding="utf-8")
        self.git.chmod(0o755)

        self.kubectl = self.root / "kubectl"
        self.kubectl.write_text(FAKE_KUBECTL, encoding="utf-8")
        self.kubectl.chmod(0o755)

        self.secrets = self.root / "secrets"
        self.secrets.mkdir()

        self.state = self.root / "git-head"
        self.state.write_text(OLD_SHA + "\n", encoding="utf-8")
        self.merge_flag = self.root / "merge.done"
        self.v2 = self.root / "v2-body.sh"
        self.v2.write_text(FAKE_V2_BODY, encoding="utf-8")
        self.v2.chmod(0o755)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run(self) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment["PATH"] = f"{self.bindir}{os.pathsep}{environment['PATH']}"
        environment["KUBECTL"] = str(self.kubectl)
        environment["GLASSLAB_V2_LOCAL_SECRETS_DIR"] = str(self.secrets)
        environment["FAKE_GIT_STATE"] = str(self.state)
        environment["FAKE_GIT_NEW_SHA"] = NEW_SHA
        environment["FAKE_GIT_SCRIPT"] = str(self.rollout)
        environment["FAKE_GIT_MERGE_FLAG"] = str(self.merge_flag)
        environment["FAKE_GIT_V2_BODY"] = str(self.v2)
        return subprocess.run(
            [
                str(self.rollout),
                "--sync",
                "--service", "rabbitmq",
                "--no-wait-for-image",
                "--skip-smoke",
                "--skip-image-prune",
            ],
            cwd=self.root,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            # An un-guarded re-exec loops forever; fail fast instead of hanging.
            timeout=30,
        )

    def test_sync_that_advances_checkout_runs_the_new_script_logic(self):
        # Given: a clean checkout at OLD_SHA, and a fake git whose first
        # `merge --ff-only` advances HEAD to NEW_SHA and checks out a script
        # whose body prints a unique marker.
        # When: the rollout runs with --sync.
        completed = self._run()
        # Then: the run re-executes and uses the newly checked-out logic. The
        # old body would finish without ever printing the marker.
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn(NEW_MARKER, completed.stdout)
        self.assertEqual(
            completed.stdout.count(NEW_MARKER),
            1,
            f"new script must run exactly once, got:\n{completed.stdout}",
        )
        self.assertIn(REEXEC_NOTICE, completed.stderr)
        # The checkout really advanced, so the marker is not a false positive.
        self.assertEqual(self.state.read_text(encoding="utf-8").strip(), NEW_SHA)


if __name__ == "__main__":
    unittest.main()
