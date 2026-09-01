# Final review fix report

## Status

All Critical, Important, and Minor findings in `final-review-findings.md` were
implemented with regression coverage. The required repository verification is
green. No live credential rotation, SOPS enrollment, vault migration,
decryption, off-host transfer, SSH session, Kubernetes read/apply, or cluster
mutation was performed. No dependency was installed.

Repository commit: `Harden final secret cleanup boundaries` (the implementation
commit containing this report).

## Finding resolution

### Critical: exposed shared password

- Removed the known shared password from the tracked provisioning cleanup
  runbook and from both authorized external handoffs.
- Replaced password/`sshpass` operational examples in the handoffs with the
  personal key-only aliases `glasslab-provisioner`, `glasslab-21`, and
  `glasslab-19` plus a future named `scripts/glasslab-secret` SOPS break-glass
  reference.
- Added a non-revealing SHA-256 fingerprint and fixed-length sliding-window
  rule so punctuation or a `user:value` example cannot hide the known value.
  Findings contain only path, line, and symbolic rule identifier.
- Digest-only verification found zero matching windows in the current tracked
  tree, `/home/gr66ss/AGENTS.md`,
  `/home/gr66ss/exo_handoff_2026-04-13.md`, and the non-Git worktree.

Live rotation is intentionally deferred. The value must still be treated as
compromised because removing current files does not revoke it or rewrite Git
history.

### Important 1: fail-closed credential scanner

- YAML parser errors, file read failures, file stat failures, and traversal
  failures now produce symbolic `scan-error-*` findings and nonzero CLI exit.
- Structural YAML node validation rejects duplicate keys, including duplicate
  `kind`, before interpreting Kubernetes objects.
- Privileged-safe tests inject deterministic failures instead of depending on
  Unix permission bits that root could bypass.
- Kubernetes `Secret.stringData` now detects DSNs, known exposed values, and
  non-redacted credential-like fields. The regression includes an ignored
  local Secret path and verifies the scanner itself still examines it.

### Important 2: secret process boundaries

- vLLM now receives `VLLM_API_KEY` from the container environment supported by
  vLLM; the pod command no longer expands the key into Python argv.
- `upload-cifar100.sh` has no credential defaults or secret CLI options. It
  reads the two explicit environment inputs, removes their inherited names,
  URL-encodes via stdin, and scopes the resulting `MC_HOST_glasslab` value to
  each `mc` child. `mc alias set` and secret-bearing argv are gone.
- The research SQLite importer accepts its Postgres DSN only from
  `GLASSLAB_ORCHESTRATOR_STORE_POSTGRES_DSN` or an already-open descriptor via
  `--dsn-fd`. It scrubs the environment, rejects `--postgres-dsn` generically,
  and reduces dependency failures to a non-secret error.
- Direct usage documentation now shows the descriptor/environment boundaries.

### Important 3: inherited xtrace

- `deploy-vllm.sh` disables inherited xtrace before any Secret path or value is
  expanded and restores it only after secret state is gone.
- `test-vllm.sh` keeps tracing disabled until the API key is unset and both
  private curl configs have been removed, then restores the caller's state.
- Sentinel regressions execute the helpers under `bash -x` and through a
  sourced wrapper, proving both non-disclosure and safe restoration.

### Important 4: GPU runner Secret contract

- The local Secret validator decodes strict base64 for `data`, rejects a value
  present in both `data` and `stringData`, and requires a trimmed, printable,
  non-placeholder PostgreSQL DSN with hostname, username, password, and
  database path.
- The existing-live-Secret path decodes and enforces the same contract through
  a pipe without capturing or printing the DSN.
- `kubeadm/glasslab-v2/gpu-runner/40-secret.local.yaml` is explicitly ignored,
  documented as the natural local path, and proven untracked.

### Important 5: backup policy and inventory validation

- `InventoryRecord` now preserves `target` and `owner`; both must be trimmed,
  printable, bounded single-line values.
- Every creation rule requires a compilable `path_regex` and a unique set of
  valid OpenPGP fingerprints.
- The first policy rule matching each inventory path is resolved during backup
  and restore. The document's normalized recipient fingerprint set must equal
  that rule's set; missing matches, duplicates, extras, and omissions fail
  closed without decryption.

### Important 6: trusted tar child environment

- Restore passes a dedicated child environment with `TAR_OPTIONS`, `TAR_RSH`,
  `RSH`, and `TAPE` removed.
- A harmless inherited GNU tar checkpoint action is proven not to execute.

### Important 7: copy destination removal

- `backup-glasslab-secrets.sh --copy-dest` and both direct two-file copy paths
  were removed.
- The helper now publishes the local archive/checksum pair only. Documentation
  directs off-host transfer through the existing staged, checksum-verifying,
  no-clobber pull helper.

### Important 8: contrastive deployment documentation

- Replaced the removed `40-secret.yaml` reference with the non-deployable
  `40-secret.example.yaml` and ignored `40-secret.local.yaml` workflow.
- Direct `kubectl apply -f .../00-all.yaml` was replaced by the fail-closed GPU
  deployment helper.
- CIFAR-100 upload examples use the future `glasslab-secret exec-env` process;
  the document explicitly states that enrollment is pending and that the only
  current allowed fallback is an already-approved private environment.

### Minor: routine gates

- The default pre-push gate runs
  `tests.security.test_secret_process_boundaries` and
  `tests.security.test_secret_backup_restore`.
- `ci-configs.yml` runs the same modules without adding an install step.
- Behavioral tests execute the gate with instrumented commands and parse the
  CI workflow to prevent either module from being silently removed.

## TDD evidence

Tests were written and executed against the prior implementation before each
production fix.

### RED

- Credential/repository suite: `Ran 62 tests`; the first run reported 16
  failures and one error across known-value embedding, scanner errors,
  duplicate YAML keys, `stringData`, ignored-path safety, GPU DSN structure,
  and inherited xtrace. The stat-injection harness was corrected so the one
  harness error became the intended failing assertion; all new behaviors
  remained RED.
- Process-boundary suite: `Ran 19 tests`; nine failures demonstrated vLLM argv,
  MinIO defaults/argv/alias behavior, research importer DSN argv/error echo,
  and missing routine gate wiring.
- Backup/restore suite: `Ran 40 tests`; ten failures and one harness error
  demonstrated dropped inventory metadata, policy/path recipient mismatch,
  unsafe tar environment, checkpoint action execution, and the surviving
  copy-destination path. The inventory assertion was corrected to expose the
  intended RED behavior rather than raising an attribute error.

The RED commands were the corresponding `python3 -m unittest ... -v` module or
focused test invocations. No production implementation was edited until the
behavioral failures had been observed.

### GREEN

- Scanner-focused regression class: 24/24 passed.
- GPU contract focused group: 7/7 passed.
- Secret argv/xtrace focused group: 9/9 passed.
- Backup policy/tar focused group: 10/10 passed.
- Routine-gate class: 2/2 passed.
- Final required security discovery: 123/123 passed.

## Verification

Fresh final results:

- `python3 -m unittest discover -s tests/security -p 'test_*.py' -v`:
  **123 passed**.
- `python3 scripts/check-credential-hygiene.py .`: passed with no findings.
- `python3 scripts/validate-configs.py`: passed.
- `python3 scripts/check-doc-links.py`: passed.
- `bash -n` for all six changed shell helpers: passed.
- `python3 -m py_compile` for all three changed Python implementations and all
  three changed security test modules: passed.
- `git diff --check`: passed.
- Digest-only exact-value scan: current tracked tree `0`, `AGENTS.md` `0`, exo
  handoff `0`, non-Git worktree `0`.
- Safe-mode assertions: personal key aliases, future SOPS break-glass
  reference, MinIO environment boundary, and research DSN env/fd boundary all
  present.

The broader `./scripts/check-before-push.sh --default` was also attempted. Its
configuration, scanner, 61 newly routine secret-boundary tests, docs, shell,
and Python checks passed. The workflow-api group reached 163/165 passing tests;
two unrelated run-artifact tests could not import a usable local `kubernetes`
package. An existing old environment was checked but its generated package was
incomplete. No package was installed, per task constraints. This environment
limitation does not affect the explicitly required verification above.

## Repository files

- Gate and ignore policy:
  - `.github/workflows/ci-configs.yml`
  - `.gitignore`
  - `scripts/check-before-push.sh`
- Scanner and credential tests:
  - `scripts/check-credential-hygiene.py`
  - `tests/security/test_credential_hygiene.py`
- Runtime/process boundaries:
  - `kubeadm/agent-stack/11-vllm-deployment.yaml`
  - `scripts/deploy-vllm.sh`
  - `scripts/test-vllm.sh`
  - `scripts/upload-cifar100.sh`
  - `services/research-orchestrator/scripts/import-sqlite-store-to-postgres.py`
  - `tests/security/test_secret_process_boundaries.py`
- GPU boundary:
  - `scripts/deploy-gpu-runner.sh`
  - `kubeadm/glasslab-v2/gpu-runner/README.md`
- Recovery boundary:
  - `scripts/backup-glasslab-secrets.sh`
  - `scripts/secret_backup_restore.py`
  - `tests/security/test_secret_backup_restore.py`
- Documentation:
  - `docs/glasslab-v2/runbooks/purge-temporary-provisioning-debug.md`
  - `docs/glasslab-v2/contrastive-learning-deployment-summary.md`
  - `docs/glasslab-v2/secrets-and-dr.md`
  - `services/research-orchestrator/README.md`
- Report:
  - `.superpowers/sdd/2026-08-21-secret-cleanup-and-recovery/final-fix-report.md`

## External edits

- `/home/gr66ss/AGENTS.md`: removed 11 digest-matched occurrences and replaced
  password fallback instructions with key-only aliases and the future SOPS
  break-glass boundary.
- `/home/gr66ss/exo_handoff_2026-04-13.md`: removed two digest-matched
  occurrences and replaced password-bearing Mac commands with key-only aliases
  and the same future break-glass boundary.

Both edits used exact marker ranges plus digest-count assertions, preserved
file modes, changed no unrelated sections, and were separately verified at
zero matching windows. They are outside Git and therefore not part of the
repository commit.

## Remaining concerns

- The exposed shared credential has not been rotated or revoked. Perform live
  rotation only after the approved SOPS enrollment and recovery gates exist.
- Git history was not rewritten. Treat the historical value as permanently
  exposed even though the current tracked tree and handoffs are clean.
- The planned `scripts/glasslab-secret`, public recipient policy, enrolled
  recipients, external live inventory, cryptographic SOPS canary, off-host
  transfer, and recovery drill remain future work.
- Backup validation is structural and recipient-policy-bound but deliberately
  does not decrypt or cryptographically verify the SOPS MAC.
- The local default gate needs a complete `kubernetes` Python dependency before
  its two pre-existing workflow-api live-status tests can run on this machine.
