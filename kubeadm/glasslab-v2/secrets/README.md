# Secret manifest examples

This tracked directory contains documentation contracts and non-secret
examples only. Do not store live plaintext manifests or encrypted live SOPS
payloads in the public checkout.

The planned external vault lives at:

```text
/home/glasslab/.local/share/glasslab-secrets
```

It contains a non-secret `inventory.yaml` and inventory-named `*.sops.yaml`
documents. `scripts/backup-glasslab-secrets.sh` archives only those encrypted
documents plus inventory, public SOPS policy, and checksums. The tracked
provisioner snapshot does not capture the vault.

See:

- [`../../../docs/glasslab-v2/secrets-and-dr.md`](../../../docs/glasslab-v2/secrets-and-dr.md)
- [`../../../docs/glasslab-v2/runbooks/restore-v2-secrets.md`](../../../docs/glasslab-v2/runbooks/restore-v2-secrets.md)

Live migration and the recovery drill remain deferred until at least two
administrators and the offline recovery recipient have passed SOPS enrollment.
