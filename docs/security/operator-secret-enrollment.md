# Operator secret enrollment

How the Glasslab SOPS vault is created, used, and recovered. Values never
appear in this document, in Git, in argv, or in shell history.

## Who

Three operators have individual keys: `gr66ss-glasslab`, `denic`, `tristanc`.
A fourth, offline recovery key exists on removable media and is never used for
routine work. Every vault document is encrypted to all four.

## 1. Install the pinned tool

`sops` 3.13.3 is installed on the provisioner at `/usr/local/bin/sops`.
Reinstall and verify with:

```bash
curl -fsSL -o /tmp/sops \
  https://github.com/getsops/sops/releases/download/v3.13.3/sops-v3.13.3.linux.amd64
printf '%s  %s\n' \
  e5bec3346a873ae91d871550f3e698c1aad962aff462a080e40f25fde17fef6b /tmp/sops \
  | sha256sum -c -
sudo install -m 0755 /tmp/sops /usr/local/bin/sops
sops --version
```

Do not install a different version without updating the constant in
`scripts/glasslab_secret.py`; `glasslab-secret doctor` rejects a mismatch.

## 2. Create your key

Run in your own account on the provisioner, so the private key never leaves
your home directory:

```bash
./scripts/new-sops-recipient.sh "Your Name <you@example>"
```

It prints one 40-hex fingerprint. Send **only that fingerprint** to the other
operators through an authenticated channel and compare the last eight
characters out loud. Never send the private key, a key export, or a passphrase.

## 3. Commit the public policy

The policy owner adds every fingerprint to `config/sops-recipients.yaml` (shape
in `sops-recipients.example.yaml`) and writes `.sops.yaml`:

```yaml
creation_rules:
  - path_regex: '.*\.sops\.yaml$'
    pgp:
      - <gr66ss-glasslab fingerprint>
      - <denic fingerprint>
      - <tristanc fingerprint>
      - <offline recovery fingerprint>
```

Open a PR. `tests/security/test_sops_policy.py` enforces three active
operators, one offline recovery recipient, unique real fingerprints, and a
rule that matches every vault document.

## 4. Create the vault

On the provisioner, in the `glasslab` account that owns the service state:

```bash
install -d -m 0700 /home/glasslab/.local/share/glasslab-secrets
```

Write `inventory.yaml` (mode `0600`) with one record per document; the shape is
in `docs/glasslab-v2/secrets-and-dr.md`:

```yaml
version: 1
secrets:
  - name: robojan-bob
    relative_path: kubeadm/robojan/robojan-bob.sops.yaml
    target: robojan/robojan-bob
    owner: platform
```

Import every operator's **public** key into the keyring used for encryption:

```bash
gpg --import < operator-public-key.asc   # one per operator, public halves only
```

Allow each operator to read (not modify) the encrypted documents without
changing their mode bits: `setfacl -R -m u:<user>:rX` on the vault directory
and files. The backup and restore helpers check mode bits only, so ACLs do not
weaken the archive boundary.

## 5. Encrypt a document

`glasslab-secret create` reads a local Secret manifest and writes ciphertext
into the vault; it refuses output that is not encrypted and writes mode `0600`.

```bash
sudo -u glasslab ./scripts/glasslab-secret create robojan-bob \
  --from /path/to/local/robojan-bob.yaml
```

The local manifest is the one described in `kubeadm/robojan/README.md` step 2
(`kubeadm/glasslab-v2/secrets/50-robojan-bob.local.yaml`, mode `0600`,
gitignored). Delete it once the vault holds the encrypted copy and the canary
passes; until then it is the only second copy.

`glasslab-secret edit NAME` opens `sops` for an existing document.
`glasslab-secret apply NAME` decrypts into a private `0700` directory, checks
that the document is a Secret, and pipes it to `kubectl apply -f -`; no value
enters argv.

## 6. Canary and recovery gate

Create a canary document with a random non-secret marker, encrypt it to all
four recipients, and have each online operator run:

```bash
GLASSLAB_SECRET_VAULT=/path/to/vault ./scripts/glasslab-secret get canary MARKER
```

Record operator, date, host, fingerprint suffix, and pass/fail only. Then move
the recovery key to offline media, delete it from every online keyring, and
prove it in an isolated GPG home. Do not rotate a shared credential until at
least two operators and the recovery key pass.

## 7. Routine operations

```bash
./scripts/glasslab-secret doctor       # sops, gpg, vault modes, inventory, policy
./scripts/glasslab-secret list
./scripts/glasslab-secret apply NAME
./scripts/glasslab-secret updatekeys   # after adding or removing a recipient
./scripts/glasslab-secret backup --output-dir /home/glasslab/glasslab-secret-backups
./scripts/glasslab-secret restore --archive <archive>
```

`glasslab-secret` refuses to run with `xtrace` enabled, refuses paths outside
the vault, rejects group- or world-readable vault material, and never prints a
value except for an explicit `get`.

## 8. ROBOJAN's four secrets

`kubeadm/robojan/10-secret.example.yaml` lists them. Encrypt each under the
vault as `robojan-bob`, `robojan-bob-postgres`, `robojan-bob-minio`, and the
GHCR pull secret. The GHCR pull secret is a copy of `glasslab-v2`'s
`glasslab-ghcr-pull`, so it can be encrypted from a `kubectl get secret`
export held only in a private file.

Until this completes, the four secrets exist only in the cluster and in the
local `0600` manifest on the provisioner. That is the gap this procedure
closes.
