# Secrets and disaster recovery

Glasslab's recovery design keeps live values and encrypted live payloads out of
Git. The external SOPS vault is expected at:

```text
/home/glasslab/.local/share/glasslab-secrets
```

`GLASSLAB_SECRET_VAULT` may select another operator-approved location. The
vault directory must be private (`0700`), and its files must be private
(`0600`). The public repository supplies policy and recovery tooling; the
external vault supplies `inventory.yaml` and the encrypted `*.sops.yaml`
documents.

The SOPS operator CLI, recipient enrollment, public `.sops.yaml` policy, and
live inventory are separate prerequisites and are not present on this branch.
Until they are enrolled on the canonical provisioner, the repository backup
and restore helpers fail closed. They do not fall back to ignored plaintext
Kubernetes manifests.

## Inventory contract

The non-secret `inventory.yaml` has version `1` and one record per ciphertext
document. Each record supplies a unique name and relative path plus operational
ownership metadata:

```yaml
version: 1
secrets:
  - name: workflow-api
    relative_path: kubeadm/glasslab-v2/workflow-api.sops.yaml
    target: glasslab-v2/glasslab-workflow-api
    owner: platform
```

Every `relative_path` must be normalized, remain below the vault, and end in
`.sops.yaml`. Backup fails if a record is missing its file or if the vault has
an unlisted `*.sops.yaml` document. Symbolic links and any traversal, stat, or
read error are rejected; an unreadable subtree cannot be treated as empty.
`target` and `owner` are required, trimmed, bounded, printable single-line
values and are preserved as part of the validated recovery inventory.

Each ciphertext document must use the approved OpenPGP SOPS structure. Every
scalar under Kubernetes Secret `data` or `stringData` must be a complete
`ENC[AES256_GCM,...]` envelope with nonempty base64 fields. Mixed plaintext is
rejected even when another field is encrypted, and duplicate YAML mappings
cannot hide an earlier plaintext payload. Every SOPS OpenPGP record must carry
a 40-hex fingerprint, UTC creation timestamp, and base64-valid armored
encrypted data key; non-PGP recipient lists are not accepted by this boundary.
Every policy creation rule must have a valid `path_regex` and one or more
unique, comma-separated 40-hex OpenPGP fingerprints. For each inventory path,
the first matching creation rule is resolved and its recipient fingerprint set
must equal the document's SOPS OpenPGP fingerprint set. A missing match,
malformed regex, duplicate recipient, or extra/missing document recipient
fails both backup and restore validation.

The live inventory must eventually cover every active secret family, including
workflow-api, research-orchestrator, the v1 agent/model-serving path, Postgres,
MinIO, NATS, registry access, and approved break-glass records. Inventory
records contain metadata only—never secret values.

## Archive format and trust boundary

`scripts/backup-glasslab-secrets.sh` creates:

```text
glasslab-secrets-<timestamp>.tar.gz
glasslab-secrets-<timestamp>.tar.gz.sha256
```

The archive contains exactly:

```text
vault/inventory.yaml
vault/<inventory relative_path>.sops.yaml
policy/.sops.yaml
SHA256SUMS
```

Payload documents are already encrypted by SOPS. The helper never calls
`sops -d`, never creates a plaintext tar, and has no passphrase option. The
archive itself is not a second encryption layer, so filenames and inventory
metadata remain visible; keep the destination private and use encrypted media
when metadata confidentiality is required.

The internal checksum manifest covers the inventory, public policy, and every
encrypted document. The adjacent checksum covers the completed archive for
transport verification. Neither checksum supplies authenticity by itself;
SOPS document metadata and MAC verification remain the payload authenticity
boundary when an enrolled operator later decrypts a canary or applies a
document.

## Backup from the provisioner

After SOPS enrollment and live-vault migration, run on the provisioner:

```bash
cd /home/glasslab/cluster-config
./scripts/backup-glasslab-secrets.sh
```

Useful explicit form:

```bash
./scripts/backup-glasslab-secrets.sh \
  --vault-dir /home/glasslab/.local/share/glasslab-secrets \
  --policy /home/glasslab/cluster-config/.sops.yaml \
  --output-dir /home/glasslab/glasslab-secret-backups
```

The output directory is set to `0700`; published artifacts are `0600` and are
never silently overwritten. Archive and checksum publication is treated as a
pair: an exception or termination signal before both links commit removes only
links owned by the current private staging directory and preserves any
pre-existing no-clobber destination. The backup helper publishes locally only;
use the verified pull helper below for the off-host copy so the archive and its
checksum cannot be split across an unverified two-file publication path.

## Pull an off-host copy

From an enrolled operator laptop on the lab network:

```bash
cd /home/gr66ss/cluster-config
./scripts/pull-glasslab-secrets-backup.sh
```

The pull helper uses the `glasslab-provisioner` personal SSH alias by default.
It runs the encrypted-only backup remotely without a TTY, downloads the archive
and checksum into randomized `0700` local staging, verifies the checksum, and
publishes both into `$HOME/glasslab-secret-backups` without overwriting an
existing backup. Its signal cleanup applies the same paired-publication rule,
so an interrupted first link does not reserve the timestamp and a safe retry
can proceed. It needs no inbound connection to the laptop and accepts no secret
or passphrase argument.

## Restore safety

`scripts/restore-glasslab-secrets.sh` performs these operations before changing
the active vault:

1. copies the archive into a randomized `0700` directory beside the target;
2. verifies the adjacent archive checksum;
3. preflights every tar member in Python and rejects absolute paths, traversal,
   duplicates, links, devices, directories, and unexpected names;
4. extracts only after preflight with fixed, root-owned `/usr/bin/tar`,
   `--no-same-owner`, and restrictive ownership and mode behavior; ambient
   `TAR_BIN` is ignored outside the explicit repository test mode, while
   `TAR_OPTIONS`, `TAR_RSH`, `RSH`, and `TAPE` are removed from the tar child;
5. compares extracted paths to the preflight result and inventory;
6. verifies internal SHA-256 coverage, policy structure, and SOPS metadata for
   every ciphertext document; and
7. requires `--yes`, then exchanges the validated staged vault atomically with
   the active vault and preserves the previous vault as
   `<vault>.rollback-<UTC timestamp>-<random>`.

SIGINT, SIGTERM, validation failure, or extraction failure before commit
removes staging and does not create a rollback. Signals are blocked only during
the atomic exchange and rollback rename, so an interruption cannot leave a
half-swapped pair. A signal delivered after the exchange and rollback commit is
reported as deferred, and the helper exits successfully with explicit restored
vault and rollback status rather than falsely reporting an unchanged vault. If
both the rollback rename and automatic exchange-back fail, the helper leaves
the private staging directory in place and reports the old vault's recovery
path instead of deleting the last preserved copy.

The tracked provisioner snapshot does not include this external vault.
`scripts/restore-provisioner-config.sh` deliberately directs operators to the
separate secret restore runbook.

## Deferred live acceptance

No live inventory migration, off-host backup, decryption, cluster apply, or
recovery drill was performed while implementing this boundary. Those steps are
blocked on verified SOPS enrollment for at least two administrators plus the
offline recovery recipient. Once that gate exists, complete the live procedure
in [Restore Glasslab v2 secrets](runbooks/restore-v2-secrets.md) and record only
operator, date, host, fingerprint suffix, inventory coverage, and pass/fail.
Never record decrypted values or private-key locations.

## Rotation expectations

A restored old value is not automatically safe. Rotate an affected credential
after suspected disclosure or when rebuilding the provisioner from an
untrusted state. Deleting an old file or Git history does not substitute for
revocation or service-side rotation.
