# Secret Cleanup and Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove tracked and operational plaintext credential paths, migrate all live secrets into the external SOPS vault, and provide encrypted inventory-driven recovery.

**Architecture:** Repository validators reject deployable examples and known credential forms. Deployment helpers fail closed unless an explicitly named encrypted/live secret is available. Backup and restore operate only on already-encrypted SOPS documents plus checksums.

**Tech Stack:** Bash, Python 3, PyYAML, SOPS/OpenPGP, kubectl, unittest.

**Spec:** `docs/superpowers/specs/2026-08-21-glasslab-sops-security-remediation-design.md`

## Global Constraints

- Deprecated WhatsApp files are excluded from remediation.
- Deletion never substitutes for rotation of a credential that may have been valid.
- No secret is accepted in argv or printed by backup/restore.
- Restore must validate into a new private directory before atomic replacement.
- Live values and encrypted live payloads remain outside the public repository.

---

### Task 1: Repository credential regression scanner

**Files:**
- Create: `scripts/check-credential-hygiene.py`
- Create: `tests/security/test_credential_hygiene.py`
- Modify: `scripts/check-before-push.sh`
- Modify: `.github/workflows/ci-configs.yml` or the existing config-validation workflow

**Interfaces:**
- `scan_tree(root: Path) -> list[Finding]`, where `Finding` contains only path, line, and rule ID, never the matched value.
- Excludes `.git`, generated caches, scan artifacts, PDFs/images, and deprecated WhatsApp paths.

- [ ] **Step 1: Write failing fixture tests**

Fixtures must detect Kubernetes Secret `data` values that decode to DSNs,
SHA-512 crypt verifiers, `sshpass -p`, known exposed-value hashes, and deployable
`change-me` Secret manifests. Include safe redacted examples and public SSH keys
as negative controls.

- [ ] **Step 2: Verify RED**

Run: `python3 -m unittest tests.security.test_credential_hygiene -v`

Expected: FAIL because the scanner is absent.

- [ ] **Step 3: Implement non-revealing scanner**

Report path, line, and a symbolic rule ID only. Decode candidate base64 in memory and compare
structure or SHA-256 digests; never print decoded text. Add it to local checks
and CI.

- [ ] **Step 4: Verify GREEN on fixtures and RED on current repository**

Run: `python3 -m unittest tests.security.test_credential_hygiene -v`

Run: `python3 scripts/check-credential-hygiene.py .`

Expected: tests PASS; repository scan FAILS on the GPU DSN and PXE verifiers.

- [ ] **Step 5: Commit scanner**

```bash
git add scripts/check-credential-hygiene.py tests/security scripts/check-before-push.sh .github/workflows
git commit -m "Add credential hygiene regression scanner"
```

### Task 2: GPU and PXE tracked credential removal

**Files:**
- Delete: `kubeadm/glasslab-v2/gpu-runner/40-secret.yaml`
- Modify: `kubeadm/glasslab-v2/gpu-runner/00-all.yaml`
- Create: `kubeadm/glasslab-v2/gpu-runner/40-secret.example.yaml`
- Modify: `scripts/deploy-gpu-runner.sh`
- Modify: `kubeadm/glasslab-v2/gpu-runner/README.md`
- Modify: `live-config/provisioner/var/www/html/pxe/cloud-init/{default,node02,node03,node04,node05,node48,node49}/user-data`
- Modify: `tests/security/test_credential_hygiene.py`

**Interfaces:**
- GPU deploy consumes an explicit `GLASSLAB_GPU_RUNNER_SECRET_FILE` ending in `.local.yaml` or a pre-existing named cluster Secret.
- PXE profiles retain the provisioner public key and set password authentication/interactive authentication false.

- [ ] **Step 1: Add failing deployment-policy tests**

Assert aggregate manifests contain no `kind: Secret`, the deploy script exits
when the live secret is absent, and every PXE user is locked/key-only with a
non-empty authorized key.

- [ ] **Step 2: Verify RED**

Run: `python3 -m unittest tests.security.test_credential_hygiene -v`

Expected: FAIL on current GPU and PXE files.

- [ ] **Step 3: Remove credential material and fail closed**

Make the example a nondeployable schema/document rather than a valid Secret.
Remove the Secret document from `00-all.yaml`. Replace each published verifier
with a locked password marker while retaining `ssh_authorized_keys` and SSH
hardening.

- [ ] **Step 4: Verify GREEN**

Run: `python3 scripts/check-credential-hygiene.py .`

Run: `python3 scripts/validate-configs.py`

Run: `bash -n scripts/deploy-gpu-runner.sh`

Expected: PASS and no credential values printed.

- [ ] **Step 5: Rotate the historical GPU database credential if applicable**

From `.44`, determine whether the referenced endpoint/database exists without
printing the DSN. If it exists or status is uncertain, create a replacement in
SOPS, apply it, revoke the old role/password, and record only the rotation date.

- [ ] **Step 6: Commit**

```bash
git add kubeadm/glasslab-v2/gpu-runner live-config/provisioner/var/www/html/pxe scripts/deploy-gpu-runner.sh tests/security
git commit -m "Remove tracked deployment credentials"
```

### Task 3: Nondeployable examples and fail-closed consumers

**Files:**
- Modify: `kubeadm/agent-stack/12-agent-secrets.example.yaml`
- Modify: `kubeadm/glasslab-v2/minio/10-secret.example.yaml`
- Modify: `kubeadm/glasslab-v2/postgres/10-secret.example.yaml`
- Modify: `kubeadm/glasslab-v2/workflow-api/10-secret.example`
- Modify: `kubeadm/glasslab-v2/research-orchestrator/11-secret.example.yaml`
- Modify: `scripts/deploy-vllm.sh`
- Modify: `scripts/test-vllm.sh`
- Modify: `tests/security/test_credential_hygiene.py`

**Interfaces:**
- Examples describe required keys but cannot be applied with kubectl.
- Deployment commands require an explicit encrypted/live source or existing Secret.

- [ ] **Step 1: Write failing tests for deployable examples and fallback**

Assert examples are not Kubernetes `Secret` objects, no placeholder credential
is accepted, `deploy-vllm.sh` fails when the local secret is absent, and
`test-vllm.sh` fails when `VLLM_API_KEY` is absent.

- [ ] **Step 2: Verify RED**

Run: `python3 -m unittest tests.security.test_credential_hygiene -v`

Expected: FAIL on current examples and fallback.

- [ ] **Step 3: Convert examples and consumers**

Use documentation records such as:

```yaml
required_keys:
  - VLLM_API_KEY
example_values_are_not_deployable: true
```

Remove every `change-me` default. Deliver curl authorization through a private
config file or stdin and delete it on exit.

- [ ] **Step 4: Verify GREEN**

Run: `python3 -m unittest tests.security.test_credential_hygiene -v`

Run: `bash -n scripts/deploy-vllm.sh scripts/test-vllm.sh`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add kubeadm/agent-stack kubeadm/glasslab-v2 scripts/deploy-vllm.sh scripts/test-vllm.sh tests/security
git commit -m "Make secret examples nondeployable"
```

### Task 4: Remove remaining argv exposure

**Files:**
- Modify: `scripts/create-ghcr-pull-secret.sh`
- Modify: `services/workflow-api/scripts/import-json-store-to-postgres.py`
- Modify: deployment/runbook callers of `--dsn`
- Create: `tests/security/test_secret_process_boundaries.py`

**Interfaces:**
- GHCR helper reads token from stdin or `GHCR_TOKEN` and constructs Docker config without a secret-bearing argv.
- Importer reads DSN from `GLASSLAB_WORKFLOW_API_STORE_POSTGRES_DSN` or protected file descriptor; `--dsn` is rejected.

- [ ] **Step 1: Write failing fake-process tests**

Capture child argv for fake kubectl/curl/import processes. Assert token and DSN
sentinels never appear. Assert missing input fails closed.

- [ ] **Step 2: Verify RED**

Run: `python3 -m unittest tests.security.test_secret_process_boundaries -v`

Expected: FAIL because current helpers expose values in argv.

- [ ] **Step 3: Implement safe input paths**

Generate Docker `config.json` in a `0700` temporary directory with `0600` mode
and apply it using `kubectl create secret generic --from-file=.dockerconfigjson=...`.
Read the Postgres DSN from environment/file and scrub it before spawning any
child process.

- [ ] **Step 4: Verify GREEN**

Run: `python3 -m unittest tests.security.test_secret_process_boundaries -v`

Expected: PASS with no sentinel in argv or captured output.

- [ ] **Step 5: Commit**

```bash
git add scripts/create-ghcr-pull-secret.sh services/workflow-api/scripts docs tests/security
git commit -m "Keep operational secrets out of argv"
```

### Task 5: Inventory-driven encrypted backup and safe restore

**Files:**
- Modify: `scripts/backup-glasslab-secrets.sh`
- Modify: `scripts/pull-glasslab-secrets-backup.sh`
- Modify: `scripts/restore-provisioner-config.sh` or replace its secret path with `glasslab-secret restore`
- Modify: `docs/glasslab-v2/secrets-and-dr.md`
- Modify: `docs/glasslab-v2/runbooks/restore-v2-secrets.md`
- Modify: `kubeadm/glasslab-v2/secrets/README.md`
- Create: `tests/security/test_secret_backup_restore.py`

**Interfaces:**
- Backup consumes the vault inventory and copies only `.sops.yaml`, inventory, policy, and SHA-256 checksums.
- Restore validates archive paths/checksums/SOPS metadata in a new `0700` directory, then atomically replaces the vault after confirmation.

- [ ] **Step 1: Write failing archive safety tests**

Cover stale inventory, missing entries, checksum corruption, absolute paths,
`../` traversal, symlinks, predictable temp paths, interrupted restore, and a
successful round trip. Assert stdout never contains a sentinel secret.

- [ ] **Step 2: Verify RED**

Run: `python3 -m unittest tests.security.test_secret_backup_restore -v`

Expected: FAIL against the symmetric plaintext-tar implementation.

- [ ] **Step 3: Implement encrypted-only backup/restore**

Do not run `sops -d` during backup. Build an archive from encrypted files only,
write checksums, then copy off-host. Restore with `tar --no-same-owner` only
after a Python path preflight; reject non-regular files. Rename the validated
staging directory atomically and preserve the old vault as a dated rollback.

- [ ] **Step 4: Verify GREEN and docs**

Run: `python3 -m unittest tests.security.test_secret_backup_restore -v`

Run: `./scripts/check-before-push.sh --docs`

Expected: PASS.

- [ ] **Step 5: Migrate live inventory and perform recovery drill**

On `.44`, enumerate ignored current secret files without printing values,
encrypt each to every active recipient, compare inventory coverage, copy the
encrypted archive off-host, and restore a canary into an isolated vault.

- [ ] **Step 6: Commit**

```bash
git add scripts/backup-glasslab-secrets.sh scripts/pull-glasslab-secrets-backup.sh scripts/restore-provisioner-config.sh docs kubeadm/glasslab-v2/secrets tests/security
git commit -m "Replace plaintext secret backup and restore"
```
