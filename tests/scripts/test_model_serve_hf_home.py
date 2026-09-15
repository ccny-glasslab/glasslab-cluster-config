"""Coverage for the standalone mlx model-server HF cache hardening.

The model servers previously used an ephemeral ``HF_HOME=/private/tmp/hf-cache``.
macOS cleared ``/private/tmp``, leaving a 0-byte snapshot whose ``config.json``
was a dangling symlink; ``mlx_lm.server`` then hung forever on the first
completion instead of failing. These tests lock the fix:

1. Static: the installer keeps weights in a durable cache under the service
   user's home, never ``/private/tmp``, and does not hardcode
   ``HF_HUB_OFFLINE=1`` (offline is opt-in).
2. Executable: the completeness predicate ``snapshot_is_complete`` rejects a
   missing ``config.json``, a missing ``model.safetensors.index.json``, a
   dangling weight-shard symlink, and a 0-byte blob; it accepts a fully
   materialized snapshot.
3. Arg parsing preserves ``--replace`` and makes ``--offline`` opt-in.

The installer is sourced for the executable cases; the ``main`` guard keeps
sourcing free of side effects.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts/model-serve/install-model-serve.sh"


class ModelServeHfHomeTests(unittest.TestCase):
    """Static and sourced-script coverage of install-model-serve.sh."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # -- helpers -----------------------------------------------------------

    def _run_sourced(
        self, body: str, *args: str
    ) -> subprocess.CompletedProcess[str]:
        """Source the installer, then run ``body`` with ``args`` as $1..$n."""
        return subprocess.run(
            ["bash", "-c", 'source "$1" >/dev/null 2>&1; shift; ' + body, "_",
             str(SCRIPT), *args],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def _snapshot(
        self,
        *,
        config: bool = True,
        index: bool = True,
        shards: tuple[str, ...] = ("model-00001-of-00001.safetensors",),
        blob_bytes: int = 1,
        dangling: bool = False,
    ) -> Path:
        """Build a synthetic Hugging Face snapshot tree under self.root."""
        repo = self.root / "hub" / "models--org--name"
        snap = repo / "snapshots" / "rev"
        blobs = repo / "blobs"
        snap.mkdir(parents=True)
        blobs.mkdir(parents=True)
        if config:
            (snap / "config.json").write_text("{}")
        if index:
            (snap / "model.safetensors.index.json").write_text("{}")
        for name in shards:
            blob = blobs / f"{name}.blob"
            if dangling:
                target = blobs / f"missing-{name}"
            else:
                target = blob
                blob.write_bytes(b"\x00" * blob_bytes)
            (snap / name).symlink_to(os.path.relpath(target, snap))
        return snap

    def _complete(self, snapshot: Path) -> bool:
        completed = self._run_sourced(
            'if snapshot_is_complete "$1"; then echo COMPLETE; '
            'else echo INCOMPLETE; fi',
            str(snapshot),
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return completed.stdout.strip() == "COMPLETE"

    # -- static assertions -------------------------------------------------

    def test_installer_never_uses_ephemeral_tmp_cache(self) -> None:
        # Given/When: the installer source.
        text = SCRIPT.read_text()
        # Then: the ephemeral macOS cache path is gone entirely.
        self.assertNotIn("/private/tmp", text)

    def test_default_cache_is_durable_and_under_service_home(self) -> None:
        text = SCRIPT.read_text()
        # Then: HF_HOME defaults under the service user's home, and the hub
        # tree plus the --model snapshot path derive from that one variable.
        self.assertIn("/Users/$SERVICE_USER/.cache/huggingface", text)
        self.assertIn('HF_HOME="${GLASSLAB_MODEL_HF_HOME:-', text)
        self.assertIn('HF_HUB="$HF_HOME/hub"', text)
        self.assertIn('SNAPSHOT="$HF_HUB/models--', text)
        self.assertIn("--model</string><string>$MODEL</string>", text)

    def test_offline_mode_is_opt_in_not_hardcoded(self) -> None:
        text = SCRIPT.read_text()
        # Then: no unconditional offline pin, and offline flows through the
        # OFFLINE variable plus an env/flag opt-in (defaults to online).
        self.assertNotIn("<key>HF_HUB_OFFLINE</key><string>1</string>", text)
        self.assertIn("<key>HF_HUB_OFFLINE</key><string>$OFFLINE</string>", text)
        self.assertIn("OFFLINE=0", text)
        self.assertIn("--offline) OFFLINE=1", text)
        self.assertIn('OFFLINE="${GLASSLAB_MODEL_HF_OFFLINE:-$OFFLINE}"', text)

    def test_completeness_preflight_is_present(self) -> None:
        text = SCRIPT.read_text()
        # Then: the preflight checks every artifact whose absence hung the
        # server, and it points at the exact repair command.
        self.assertIn("snapshot_is_complete()", text)
        self.assertIn("require_complete_snapshot", text)
        self.assertIn("config.json is missing", text)
        self.assertIn("model.safetensors.index.json is missing", text)
        self.assertIn("model-*.safetensors", text)
        self.assertIn("$hf download $REPO --revision $REVISION", text)

    # -- executable predicate ---------------------------------------------

    def test_accepts_complete_snapshot(self) -> None:
        # Given: config, index, and one shard symlinked to a non-empty blob.
        snapshot = self._snapshot()
        # When/Then: the predicate reports complete.
        self.assertTrue(self._complete(snapshot))

    def test_rejects_missing_config(self) -> None:
        snapshot = self._snapshot(config=False)
        self.assertFalse(self._complete(snapshot))

    def test_rejects_missing_shard_index(self) -> None:
        snapshot = self._snapshot(index=False)
        self.assertFalse(self._complete(snapshot))

    def test_rejects_dangling_shard_symlink(self) -> None:
        # Given: the snapshot the ephemeral cache left behind — the config
        # exists but a weight shard points at a blob that is not there.
        snapshot = self._snapshot(dangling=True)
        self.assertFalse(self._complete(snapshot))

    def test_rejects_zero_byte_blob(self) -> None:
        # Given: an incomplete download — the shard symlink resolves to a
        # 0-byte blob.
        snapshot = self._snapshot(blob_bytes=0)
        self.assertFalse(self._complete(snapshot))

    def test_rejects_snapshot_without_shards(self) -> None:
        snapshot = self._snapshot(shards=())
        self.assertFalse(self._complete(snapshot))

    # -- arg parsing -------------------------------------------------------

    def test_replace_flag_preserved_and_offline_is_opt_in(self) -> None:
        # Given/When: role with the legacy --replace flag.
        completed = self._run_sourced(
            'ROLE=""; REPLACE=0; OFFLINE=0; parse_args "$@"; '
            'echo "$ROLE|$REPLACE|$OFFLINE"',
            "coder", "--replace",
        )
        # Then: role and replace are unchanged, offline stays off.
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), "coder|1|0")

        # When: --offline is requested (order-independent).
        completed = self._run_sourced(
            'ROLE=""; REPLACE=0; OFFLINE=0; parse_args "$@"; '
            'echo "$ROLE|$REPLACE|$OFFLINE"',
            "--offline", "thinking",
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), "thinking|0|1")

        # Then: the default (no flag) is online.
        completed = self._run_sourced(
            'ROLE=""; REPLACE=0; OFFLINE=0; parse_args "$@"; '
            'echo "$ROLE|$REPLACE|$OFFLINE"',
            "coder",
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), "coder|0|0")


if __name__ == "__main__":
    unittest.main()
