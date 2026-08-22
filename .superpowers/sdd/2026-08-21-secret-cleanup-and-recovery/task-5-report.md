# Task 5 Report: Inventory-driven encrypted backup and safe restore

## Status

Implemented and repository-verified. The live vault migration, off-host copy,
SOPS canary decryption, and recovery drill were intentionally not performed.

## Dependency ruling

The planned `glasslab-secret` CLI, public `.sops.yaml` policy, recipient
enrollment, and live external inventory are not implemented on this branch.
Rather than inventing a plaintext decryption path, this task adds a focused
standalone archive boundary in `scripts/secret_backup_restore.py` with narrow
backup, restore, and transport-checksum verification operations.

The helpers fail closed until an operator supplies:

- a private external vault (default
  `/home/glasslab/.local/share/glasslab-secrets`);
- a version-1 `inventory.yaml` inside that vault;
- exactly one regular `*.sops.yaml` file for every inventory record and no
  unlisted SOPS documents; and
- a public SOPS policy with recipient-bearing creation rules.

No command in this implementation calls `sops -d`, accepts a passphrase, or
falls back to the ignored plaintext Kubernetes manifests.

## Changes

- Replaced the hard-coded plaintext-manifest/GPG backup with an
  inventory-driven archive of only:
  - `vault/inventory.yaml`;
  - inventory-listed encrypted `vault/**/*.sops.yaml` documents;
  - `policy/.sops.yaml`; and
  - `SHA256SUMS`.
- Added an adjacent SHA-256 sidecar for transport verification. Backup output
  is assembled in randomized private staging and published without replacing
  an existing artifact.
- Added `scripts/restore-glasslab-secrets.sh` and the Python restore boundary.
  Restore:
  - copies the selected archive through a no-follow regular-file descriptor;
  - verifies the adjacent checksum;
  - preflights every tar path and member type in Python before extraction;
  - rejects absolute paths, traversal, duplicate members, symlinks, hardlinks,
    devices, directories, sparse entries, unexpected files, excessive size,
    inventory mismatch, checksum mismatch, invalid policy, and missing or
    malformed SOPS metadata;
  - extracts with `tar --no-same-owner` plus restrictive metadata flags into a
    randomized `0700` sibling directory;
  - normalizes restored directories to `0700` and files to `0600`;
  - requires explicit `--yes` only after all staged validation completes; and
  - uses Linux `renameat2(RENAME_EXCHANGE)` for atomic replacement, then moves
    the old vault to a dated randomized rollback directory.
- Blocks SIGINT/SIGTERM during only the final exchange/rollback critical
  section. Earlier interruptions remove staging and leave the active vault
  unchanged.
- Hardened the laptop pull path to use the personal
  `glasslab-provisioner` alias, escaped remote argv, no remote TTY, randomized
  local staging, archive-checksum verification, and no-clobber local
  publication of both artifacts.
- Kept provisioner configuration restore separate from the external secret
  vault and added an explicit operator notice.
- Rewrote the DR and restore documentation to remove predictable `/tmp`, GPG
  passphrase, plaintext extraction, `sed`/`cat`, and direct `kubectl apply`
  procedures. The docs now state the enrollment dependency and deferred live
  acceptance gate.

## TDD evidence

Initial RED:

```bash
python3 -m unittest tests.security.test_secret_backup_restore -v
```

Against the old symmetric plaintext-tar implementation, the 12-test suite
reported six failures and three errors: the old helper rejected the new vault
interface, still expected plaintext source paths, and had no safe restore
entrypoint. Three fail-closed controls passed only because the new interface
was absent; later green and mutation-oriented coverage proved those branches
against the implemented boundary.

The off-host pull tests were then added before its implementation and failed
2/2 because the old helper lacked vault/policy options and passed an
option-shaped host to SSH.

During self-review, a further RED regression reproduced a rare rollback bug:
after a successful exchange, if both the rollback rename and exchange-back
failed, temporary-directory cleanup erased the staged old vault. The focused
test failed because the recovery workspace no longer existed. The fix now
retains that private workspace and reports the previous vault path for manual
recovery.

Final focused GREEN:

```text
15 tests ran; 15 passed
```

Coverage includes stale inventory, missing inventory coverage, symlinked
source files, absolute paths, parent traversal, archive symlinks, corrupted
internal checksums, missing archive entries, malformed SOPS metadata, explicit
confirmation, randomized `0700` staging, SIGTERM cleanup, atomic round trip,
rollback preservation, catastrophic rollback preservation, safe off-host
pull, transport verification, and no sentinel value in captured output.

## Security self-review

The Codex Security working-tree diff workflow reviewed all five changed runtime
source files with no delegation. Its final canonical scan reported zero
surviving findings after the rollback double-failure fix. The scan covered:

- plaintext/non-output invariants;
- inventory and regular-file scope;
- SSH/scp command construction;
- hostile archive path, type, and extraction controls;
- checksum and SOPS metadata gates; and
- atomic exchange, interruption, rollback, and manual-recovery behavior.

TAC status could not be checked because the access connector was unavailable;
that advisory did not gate the local review.

## Verification

Fresh final verification before commit produced:

- `python3 -m unittest tests.security.test_secret_backup_restore -v`:
  15/15 passed;
- `python3 -m unittest tests.security.test_secret_process_boundaries
  tests.security.test_credential_hygiene -v`: 55/55 passed;
- shell syntax for all four affected shell helpers: passed;
- Python compilation for the implementation and focused tests: passed;
- `python3 scripts/check-credential-hygiene.py .`: passed with no findings;
- `python3 scripts/validate-configs.py`: passed;
- `./scripts/check-before-push.sh --docs`: passed;
- `git diff --check`: passed;
- `services/research-workspace-runner`: 6/6 passed; and
- `services/research-orchestrator`: 224 passed, 7 skipped.

The default `./scripts/check-before-push.sh` passed its configuration,
credential, documentation, shell, and Python checks, then reached 163/165
passing `workflow-api` tests. Two unrelated run-artifact tests could not import
the environment's absent `kubernetes` Python package. No dependency was
installed, as required by this task; the failure was preserved as an explicit
environmental limitation rather than described as a passing full gate.

## Deferred concerns

- No live host, vault, recipient key, Secret, workload, or cluster was read or
  changed.
- Live inventory migration and the recovery drill remain blocked until at
  least two online administrators plus offline recovery pass SOPS enrollment.
- SOPS metadata validation here is deliberately structural. Cryptographic MAC
  verification requires the separately approved enrolled SOPS operation; this
  archive boundary does not decrypt.
- The archive contains ciphertext rather than a second archive-level
  encryption layer. Payload values remain SOPS-encrypted, but filenames and
  non-secret inventory metadata are visible. Use encrypted media where that
  metadata also requires confidentiality.
- Atomic replacement requires Linux `renameat2(RENAME_EXCHANGE)`. Unsupported
  hosts fail closed before replacing an existing vault.
