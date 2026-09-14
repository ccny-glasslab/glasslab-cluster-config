"""Preflight and rollback-guidance coverage for rollout-research-services.sh.

Issue #245: the rollout script can deploy an image tag GHCR has not built yet,
and a mid-bundle failure leaves a mixed-version cluster with no rollback
guidance. These tests lock the two safety behaviors:

1. Before any ``kubectl set image``, the script polls the GHCR manifest for
   the target tag and refuses to proceed while it is missing.
2. When a component fails partway through the bundle, the script prints
   explicit rollback commands carrying the previously deployed image refs.

The script is exercised end-to-end with a fake ``kubectl`` and a fake GHCR
manifest probe injected through the ``KUBECTL`` / ``IMAGE_PROBE`` env vars the
script already honors.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts/rollout-research-services.sh"

TAG = "97d0caa"
ORCHESTRATOR_IMAGE = f"ghcr.io/ccny-glasslab/glasslab-research-orchestrator:{TAG}"
WORKFLOW_API_IMAGE = f"ghcr.io/ccny-glasslab/glasslab-workflow-api:{TAG}"
PRIOR_ORCHESTRATOR = "ghcr.io/ccny-glasslab/glasslab-research-orchestrator:prior-sha"
PRIOR_WORKFLOW_API = "ghcr.io/ccny-glasslab/glasslab-workflow-api:prior-sha"

FAKE_KUBECTL = """#!/usr/bin/env bash
set -euo pipefail
# Fake kubectl for rollout-research-services.sh tests. Behavior is driven by
# env vars:
#   KUBECTL_LOG         append every invocation line to this file
#   KUBECTL_FAIL_ROLLOUT  deployment name whose rollout status should fail
#   KUBECTL_APPLY_FAIL  when 1, every kubectl apply fails
#   KUBECTL_PRIOR_IMAGE_<NAME>  image returned by jsonpath get deployment
if [[ -n "${KUBECTL_LOG:-}" ]]; then
  printf '%s\\n' "$*" >> "$KUBECTL_LOG"
fi
sub=""
sub2=""
prev=""
for arg in "$@"; do
  if [[ "$prev" == "-n" || "$prev" == "-o" ]]; then
    prev="$arg"
    continue
  fi
  if [[ "$arg" == "-n" || "$arg" == "-o" ]]; then
    prev="$arg"
    continue
  fi
  if [[ -z "$sub" ]]; then
    sub="$arg"
  elif [[ -z "$sub2" ]]; then
    sub2="$arg"
  fi
  prev="$arg"
done
if [[ "$sub" == "apply" ]]; then
  # Drain stdin so the upstream `set image ... | apply -f -` pipe never
  # sees SIGPIPE.
  cat >/dev/null
  if [[ "${KUBECTL_APPLY_FAIL:-}" == "1" ]]; then
    printf 'fake kubectl: apply failed\\n' >&2
    exit 1
  fi
  exit 0
fi
if [[ "$sub" == "set" && "$sub2" == "image" ]]; then
  cat <<'YAML'
apiVersion: apps/v1
kind: Deployment
metadata:
  name: fake-deployment
spec:
  template:
    spec:
      containers:
        - name: fake
          image: fake
YAML
  exit 0
fi
if [[ "$sub" == "rollout" && "$sub2" == "status" ]]; then
  name=""
  for arg in "$@"; do
    case "$arg" in
      deployment/*) name="${arg#deployment/}" ;;
    esac
  done
  if [[ -n "$name" && "$name" == "${KUBECTL_FAIL_ROLLOUT:-}" ]]; then
    printf 'fake kubectl: rollout status failed for %s\\n' "$name" >&2
    exit 1
  fi
  exit 0
fi
if [[ "$sub" == "get" ]]; then
  kind=""
  name=""
  output=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      -n) shift 2 ;;
      -o) output="$2"; shift 2 ;;
      *)
        if [[ -z "$kind" ]]; then
          kind="$1"
        else
          name="$1"
        fi
        shift
        ;;
    esac
  done
  if [[ "$output" == jsonpath=* && -n "$name" ]]; then
    var="KUBECTL_PRIOR_IMAGE_$(printf '%s' "$name" | tr '[:lower:]-' '[:upper:]_')"
    printf '%s\\n' "${!var:-}"
  fi
  exit 0
fi
exit 0
"""

FAKE_PROBE = """#!/usr/bin/env bash
set -euo pipefail
# Fake GHCR manifest probe: succeeds only for images listed in PROBE_EXISTING.
if [[ "${1:-}" != "manifest" ]]; then
  exit 2
fi
grep -qxF "${2:-}" "${PROBE_EXISTING:?}"
"""


class RolloutImagePreflightTests(unittest.TestCase):
    """End-to-end coverage of the rollout preflight and rollback guidance."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.kubectl = self.root / "kubectl"
        self.kubectl.write_text(FAKE_KUBECTL)
        self.kubectl.chmod(0o755)
        self.probe = self.root / "probe"
        self.probe.write_text(FAKE_PROBE)
        self.probe.chmod(0o755)
        self.existing = self.root / "existing-images"
        self.existing.write_text("")
        self.log = self.root / "kubectl.log"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run(self, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(SCRIPT), *args],
            cwd=REPO_ROOT,
            env={
                **os.environ,
                "KUBECTL": str(self.kubectl),
                "IMAGE_PROBE": str(self.probe),
                "PROBE_EXISTING": str(self.existing),
                "KUBECTL_LOG": str(self.log),
                "IMAGE_POLL_ATTEMPTS": "2",
                "IMAGE_POLL_INTERVAL": "0",
                **env,
            },
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def _invocations(self) -> list[str]:
        if not self.log.exists():
            return []
        return [line for line in self.log.read_text().splitlines() if line.strip()]

    def test_blocks_when_image_tag_missing_from_ghcr(self) -> None:
        # Given: GHCR has not built the target tag yet (probe finds nothing).
        self.existing.write_text(
            "ghcr.io/ccny-glasslab/glasslab-research-orchestrator:other-sha\n"
        )
        # When: the rollout runs with --wait-for-image (the default).
        completed = self._run(
            "--service", "research-orchestrator",
            "--tag", TAG,
            "--skip-smoke",
            "--skip-image-prune",
        )
        # Then: the script refuses to deploy and explains CI is still building.
        self.assertNotEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("not found", completed.stderr)
        self.assertIn("CI", completed.stderr)
        self.assertNotIn("set image", self._invocations())

    def test_proceeds_when_image_tag_exists(self) -> None:
        # Given: GHCR already contains both bundle images for the tag.
        self.existing.write_text(f"{ORCHESTRATOR_IMAGE}\n{WORKFLOW_API_IMAGE}\n")
        # When: the full bundle is rolled out.
        completed = self._run(
            "--service", "all",
            "--tag", TAG,
            "--skip-smoke",
            "--skip-image-prune",
        )
        # Then: the rollout proceeds and sets the image.
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue(
            any("set image" in line for line in self._invocations()),
            self._invocations(),
        )

    def test_no_wait_for_image_skips_preflight(self) -> None:
        # Given: the tag is missing from GHCR but the operator opts out.
        self.existing.write_text("")
        # When: --no-wait-for-image is passed.
        completed = self._run(
            "--service", "research-orchestrator",
            "--tag", TAG,
            "--no-wait-for-image",
            "--skip-smoke",
            "--skip-image-prune",
        )
        # Then: the rollout proceeds without the preflight gate.
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue(
            any("set image" in line for line in self._invocations()),
            self._invocations(),
        )

    def test_mid_bundle_failure_prints_rollback_guidance_with_prior_image(self) -> None:
        # Given: both images exist, the orchestrator rolls first, and the
        # workflow-api rollout fails; the cluster previously ran prior-sha.
        self.existing.write_text(f"{ORCHESTRATOR_IMAGE}\n{WORKFLOW_API_IMAGE}\n")
        # When: the bundle rollout fails partway through.
        completed = self._run(
            "--service", "all",
            "--tag", TAG,
            "--skip-smoke",
            "--skip-image-prune",
            KUBECTL_FAIL_ROLLOUT="glasslab-workflow-api",
            KUBECTL_PRIOR_IMAGE_GLASSLAB_RESEARCH_ORCHESTRATOR=PRIOR_ORCHESTRATOR,
            KUBECTL_PRIOR_IMAGE_GLASSLAB_WORKFLOW_API=PRIOR_WORKFLOW_API,
        )
        # Then: explicit rollback guidance names the prior images.
        self.assertNotEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("rollback", completed.stderr.lower())
        self.assertIn(PRIOR_ORCHESTRATOR, completed.stderr)
        self.assertIn(PRIOR_WORKFLOW_API, completed.stderr)
        self.assertIn("set image", completed.stderr)


if __name__ == "__main__":
    unittest.main()