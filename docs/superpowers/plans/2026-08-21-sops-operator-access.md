# SOPS Operator Access Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give all three Glasslab operators independent, recoverable SOPS access without coupling it to SSH, sudo, GitHub, or Kubernetes access.

**Architecture:** Public policy, recipient fingerprints, schemas, and tooling live in this repository; encrypted live payloads live in `/home/glasslab/.local/share/glasslab-secrets` and encrypted off-host backups. A shell CLI exposes narrow operations and delegates validation/inventory handling to a small Python module.

**Tech Stack:** SOPS, OpenPGP/GnuPG, Bash, Python 3, PyYAML, kubectl, unittest.

**Spec:** `docs/superpowers/specs/2026-08-21-glasslab-sops-security-remediation-design.md`

## Global Constraints

- SOPS must not gate SSH, sudo, GitHub, or Kubernetes recovery.
- Private keys and encrypted live payloads must not enter the public repository.
- Secret values must not appear in argv, logs, shell tracing, or predictable temporary files.
- Every vault document must include all active operator recipients and the offline recovery recipient.
- Do not rotate a shared credential until two operators and offline recovery pass the canary drill.

---

### Task 0: Apply the merged personal password-lock policy

**Files:**
- Verify: `ansible/group_vars/identity_hosts.yml`
- Apply from: `/home/glasslab/cluster-config` on the provisioner

**Interfaces:**
- Consumes: the merged `password_locked: true` records for `denic` and `tristanc`.
- Produces: Linux `passwd -S` state `L` on the gateway and provisioner while preserving each `authorized_keys` file.

- [ ] **Step 1: Unlock the personal SSH key locally**

Run `ssh-add ~/.ssh/id_ed25519_glasslab`, then prove
`ssh glasslab-provisioner 'sudo -n true'` succeeds. Never use a shared password
as a command-line fallback.

- [ ] **Step 2: Run check mode from canonical `.44`**

Run `./scripts/manage-identities.sh check` with the personal administrator and
review that only the intended password-lock changes occur.

- [ ] **Step 3: Apply and verify**

Run `./scripts/manage-identities.sh apply`, then use `sudo passwd -S denic` and
`sudo passwd -S tristanc` on both Linux hosts. Verify their authorized-key files
remain non-empty and force a public-key-only test for each account.

- [ ] **Step 4: Record non-secret evidence**

Add the apply date and four lock-status results to the migration issue. Do not
record password hashes or public-key bodies.

### Task 1: Public recipient and vault policy

**Files:**
- Create: `.sops.yaml`
- Create: `config/sops-recipients.yaml`
- Create: `config/secret-inventory.example.yaml`
- Create: `tests/security/test_sops_policy.py`
- Modify: `.gitignore`
- Modify: `scripts/check-before-push.sh`

**Interfaces:**
- Produces: recipient records `{username, fingerprint, role, state}` and inventory records `{name, relative_path, target, owner}`.
- Consumes: operator fingerprints collected during enrollment; placeholder fingerprints are forbidden.

- [ ] **Step 1: Write the failing policy tests**

Create unittest cases that require three active named operators, one recovery
recipient, unique 40-hex OpenPGP fingerprints, matching SOPS creation rules,
and ignored local vault/config paths. Tests must fail while files are absent.

- [ ] **Step 2: Verify RED**

Run: `python3 -m unittest tests.security.test_sops_policy -v`

Expected: FAIL because `config/sops-recipients.yaml` and `.sops.yaml` do not exist.

- [ ] **Step 3: Add public policy files**

Use records with `username`, `fingerprint`, and `state` fields after collecting
real public fingerprints. Enforce the value shape in the test rather than
committing illustrative fingerprints:

```python
assert set(operator_names) == {'gr66ss-glasslab', 'denic', 'tristanc'}
assert all(re.fullmatch(r'[0-9A-F]{40}', item['fingerprint']) for item in operators)
assert re.fullmatch(r'[0-9A-F]{40}', recovery['fingerprint'])
assert recovery['storage'] == 'offline'
```

`.sops.yaml` must select `*.sops.yaml` below the external vault layout and list
all four fingerprints in its `pgp` rule. Add `.glasslab-secrets`,
`.glasslab-secrets.env`, and `glasslab-secrets/` to `.gitignore`.

- [ ] **Step 4: Verify GREEN**

Run: `python3 -m unittest tests.security.test_sops_policy -v`

Expected: PASS with four unique recipients and no placeholder fingerprints.

- [ ] **Step 5: Commit**

```bash
git add .sops.yaml .gitignore config tests/security scripts/check-before-push.sh
git commit -m "Define Glasslab SOPS recipient policy"
```

### Task 2: Vault inventory library and CLI safety boundary

**Files:**
- Create: `scripts/glasslab_secret.py`
- Create: `scripts/glasslab-secret`
- Create: `tests/security/test_glasslab_secret.py`

**Interfaces:**
- Produces: `load_inventory(path: Path) -> list[SecretRecord]`, `resolve_secret(name: str) -> Path`, and CLI operations `doctor`, `edit`, `apply`, `get`, `exec-env`, `backup`, `restore`, `updatekeys`.
- Consumes: `GLASSLAB_SECRET_VAULT` or default `/home/glasslab/.local/share/glasslab-secrets`; `SOPS_CONFIG` or repository `.sops.yaml`.

- [ ] **Step 1: Write failing inventory and permission tests**

Cover unknown names, duplicate paths, traversal (`../`), missing `.sops.yaml`
suffix, vault modes broader than `0700`, file modes broader than `0600`, and a
valid inventory. Use `tempfile.TemporaryDirectory`; never touch the real vault.

- [ ] **Step 2: Verify RED**

Run: `python3 -m unittest tests.security.test_glasslab_secret -v`

Expected: FAIL because `scripts/glasslab_secret.py` is absent.

- [ ] **Step 3: Implement the parser and command dispatcher**

Use a frozen `SecretRecord` dataclass. Resolve every path and require
`resolved_path.is_relative_to(vault.resolve())`. Set `umask(0o077)` before any
temporary file. Reject execution when `SHELLOPTS` contains `xtrace`.

The shell entrypoint must contain only:

```bash
#!/usr/bin/env bash
set -euo pipefail
exec python3 "$(dirname "$0")/glasslab_secret.py" "$@"
```

- [ ] **Step 4: Verify GREEN and syntax**

Run: `python3 -m unittest tests.security.test_glasslab_secret -v`

Run: `bash -n scripts/glasslab-secret`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/glasslab-secret scripts/glasslab_secret.py tests/security/test_glasslab_secret.py
git commit -m "Add safe Glasslab secret CLI boundary"
```

### Task 3: Decrypt, apply, and scoped execution

**Files:**
- Modify: `scripts/glasslab_secret.py`
- Modify: `tests/security/test_glasslab_secret.py`

**Interfaces:**
- `decrypt_to_private_file(record, temp_dir) -> Path`
- `apply_secret(record, kubectl_bin='kubectl') -> None`
- `exec_with_secret_env(record, fields: list[str], argv: list[str]) -> int`

- [ ] **Step 1: Write failing process-boundary tests**

Use fake `sops` and `kubectl` executables. Assert decrypted files are `0600`,
their directory is `0700`, cleanup happens after success, failure, and SIGTERM,
kubectl receives a file/stdin but no value in argv, and only requested keys are
present in the child environment.

- [ ] **Step 2: Verify RED**

Run: `python3 -m unittest tests.security.test_glasslab_secret.GlasslabSecretProcessTests -v`

Expected: FAIL because process operations are not implemented.

- [ ] **Step 3: Implement operations**

Call subprocesses with argv arrays and `shell=False`. Use
`tempfile.TemporaryDirectory(prefix='glasslab-secret-', dir=os.environ.get('XDG_RUNTIME_DIR'))`.
Register cleanup through the context manager and signal handlers. `get` warns on
stderr when stdout is a TTY; it emits only the selected scalar and a trailing
newline. `apply` validates Kubernetes kind `Secret` and rejects example paths.

- [ ] **Step 4: Verify GREEN**

Run: `python3 -m unittest tests.security.test_glasslab_secret -v`

Expected: PASS, including `/proc`-style fake argv capture.

- [ ] **Step 5: Commit**

```bash
git add scripts/glasslab_secret.py tests/security/test_glasslab_secret.py
git commit -m "Implement scoped SOPS secret operations"
```

### Task 4: Independent access check and enrollment guide

**Files:**
- Create: `scripts/check-my-access.sh`
- Create: `tests/security/test_check_my_access.py`
- Create: `docs/security/operator-secret-enrollment.md`
- Modify: `docs/contributor-access.md`
- Modify: `docs/identity-management.md`

**Interfaces:**
- Produces independent result lines with fields `boundary`, `status`, and a safe `detail` message.
- Consumes SSH aliases, `sudo -n`, GitHub CLI status, kubectl authorization, and `glasslab-secret doctor`.

- [ ] **Step 1: Write failing fake-command tests**

Assert one failed SOPS check does not mark SSH/sudo/kubectl failed; force SSH
with `BatchMode=yes`, `PreferredAuthentications=publickey`, and
`PasswordAuthentication=no`; assert output contains no environment values.

- [ ] **Step 2: Verify RED**

Run: `python3 -m unittest tests.security.test_check_my_access -v`

Expected: FAIL because the checker is absent.

- [ ] **Step 3: Implement checker and enrollment documentation**

Check gateway, provisioner, exo17, and exo18 separately. Document local
passphrase-protected key generation, exporting only the fingerprint/public key,
recipient review, canary decryption, revocation, and offline recovery. State
prominently that SOPS failure never justifies sharing another person's private
key or kubeconfig.

- [ ] **Step 4: Verify GREEN and documentation**

Run: `python3 -m unittest tests.security.test_check_my_access -v`

Run: `./scripts/check-before-push.sh --docs`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/check-my-access.sh tests/security/test_check_my_access.py docs/security docs/contributor-access.md docs/identity-management.md
git commit -m "Document and verify independent operator access"
```

### Task 5: Enrollment and live canary gate

**Files:**
- Modify: `config/sops-recipients.yaml`
- Modify: `.sops.yaml`
- Create outside Git: `/home/glasslab/.local/share/glasslab-secrets/inventory.yaml`
- Create outside Git: `/home/glasslab/.local/share/glasslab-secrets/canary.sops.yaml`

**Interfaces:**
- Consumes four verified public fingerprints.
- Produces a canary encrypted to all recipients and a signed-off recovery record containing no secret.

- [ ] **Step 1: Install pinned SOPS on `.44` and verify its checksum**

Record the exact version, download URL, and SHA-256 in the enrollment guide.
Do not install Python dependencies.

- [ ] **Step 2: Enroll each public fingerprint**

Import only public keys, compare the full 40-hex fingerprints through an
authenticated channel, and update policy using a reviewed commit.

- [ ] **Step 3: Create and test the canary**

Run `glasslab-secret doctor` and a non-printing decrypt test independently as
Tyler, Denise, and Tristan. Test the recovery key from an isolated GPG home,
then return it offline.

- [ ] **Step 4: Record non-secret evidence**

Record operator, fingerprint suffix, date, host, and pass/fail only. Do not
record the canary value or private-key location.

- [ ] **Step 5: Commit public enrollment evidence**

```bash
git add .sops.yaml config/sops-recipients.yaml docs/security/operator-secret-enrollment.md
git commit -m "Enroll Glasslab SOPS operators"
```
