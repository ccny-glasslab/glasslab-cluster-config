# Restore Glasslab v2 encrypted secrets

This procedure restores the external encrypted SOPS vault. It does not decrypt
documents, apply Kubernetes Secrets, or restart workloads. Those later actions
require the separately enrolled SOPS operator boundary.

Do not perform this procedure until the archive and its adjacent `.sha256` file
are available on the provisioner and an operator has explicitly approved the
target vault replacement.

## 1. Confirm the operating boundary

Use a personal provisioner account and the canonical checkout:

```bash
ssh glasslab-provisioner
cd /home/glasslab/cluster-config
```

Confirm the intended archive names without displaying their contents:

```bash
backup_dir=/home/glasslab/glasslab-secret-backups
find "$backup_dir" -maxdepth 1 -type f \
  \( -name 'glasslab-secrets-*.tar.gz' -o -name 'glasslab-secrets-*.tar.gz.sha256' \) \
  -printf '%f\n' | sort
```

Select the exact archive path. Keep the checksum beside it using the default
`<archive>.sha256` name.

## 2. Restore into the external vault

The helper validates in a randomized private sibling directory before it
honors confirmation. Replace the example timestamp with the reviewed backup:

```bash
archive=/home/glasslab/glasslab-secret-backups/glasslab-secrets-YYYYMMDD-HHMMSS.tar.gz
vault=/home/glasslab/.local/share/glasslab-secrets
./scripts/restore-glasslab-secrets.sh \
  --archive "$archive" \
  --vault-dir "$vault" \
  --yes
```

`--yes` is mandatory. A checksum mismatch, unsafe tar path, traversal, link,
unexpected member, inventory mismatch, invalid SOPS metadata, or tar failure
stops before replacement. The helper never calls `sops -d` and never prints a
secret document.

If the vault already exists, the output reports the dated randomized rollback
directory. Preserve it until the recovery drill and service validation are
complete. In the exceptional case where both rollback preservation and the
automatic exchange-back fail, stop immediately: the error reports a private
staging path that still contains the previous vault. Do not remove that path
until an operator has completed a reviewed manual recovery.

## 3. Check restored metadata without values

Check directory ownership/modes and inventory filenames only:

```bash
stat -c '%a %U:%G %n' "$vault" "$vault/inventory.yaml"
find "$vault" -type f -name '*.sops.yaml' -printf '%P\n' | sort
```

Expected modes are `700` for the vault/directories and `600` for files. Compare
the filename list with the inventory through the approved SOPS operator tooling
once that tooling is enrolled. Do not use `sed`, `cat`, shell tracing, or a
generic YAML dump on live secret documents.

## 4. Complete the SOPS recovery gate

This repository change does not supply a plaintext-decryption fallback. Before
any cluster apply or credential rotation:

1. verify two individual online recipients can pass the approved non-printing
   canary check;
2. verify the offline recovery recipient in an isolated GPG home, then return
   its private material offline;
3. verify every live inventory entry carries every active recipient; and
4. restore the canary to an isolated vault and record only pass/fail metadata.

The planned `glasslab-secret doctor`/`apply` interface owns later decryption and
cluster application. If it is not installed and reviewed, stop here rather
than inventing a plaintext temporary-file procedure.

## 5. Apply and validate only after enrollment

Once the approved SOPS operator CLI exists, use its named inventory operations
to apply the required records. Then restart only the workloads whose Secret
values changed and run their existing service smoke tests. Do not treat a
successful archive restore as proof that Kubernetes objects or workloads were
updated.

Record non-secret recovery evidence:

- archive timestamp and SHA-256 verification result;
- restored inventory record count (not values);
- rollback directory name;
- operator, date, host, fingerprint suffix, and canary pass/fail; and
- affected workload validation results.

## 6. Rollback decision

Keep the prior vault rollback directory unchanged until the drill completes.
If SOPS recipient or canary validation fails, do not apply the restored records
and do not rotate credentials. Escalate for a reviewed rollback or create a new
archive from the preserved directory; avoid ad-hoc directory moves during an
active recovery.

## Deferred status

The live vault migration and recovery drill are intentionally deferred until
SOPS enrollment is complete. Repository tests use isolated disposable vaults
and do not contact the provisioner, read live secrets, decrypt data, or mutate
the cluster.
