# Glasslab SOPS and Security Remediation Design

Date: 2026-08-21

Status: proposed for implementation

## Purpose

Remove plaintext credentials and unsafe secret handling from Glasslab without
making SOPS, one person's key, or a shared password the root of operational
access. Denise, Tristan, and Tyler must be able to perform normal lab operations
through personal, attributable identities. SOPS protects secret values; it does
not grant SSH, sudo, GitHub, or Kubernetes access.

This design also addresses the non-WhatsApp findings from the 2026-08-21
security review. WhatsApp is deprecated and explicitly outside this project.

## Security Outcomes

The project is complete when:

1. No plaintext password or live service credential is tracked, documented in
   operator handoff files, passed through process arguments, or printed during
   restore.
2. Every current operator uses a personal SSH identity and has sufficient
   personal sudo and Kubernetes access for the work expected of them.
3. Each secret administrator has an individual SOPS recipient. Removing one
   recipient does not require changing every other operator's key.
4. Loss of SOPS access cannot prevent SSH login, sudo, Kubernetes recovery,
   repository access, or re-enrollment by another administrator.
5. At least two tested administrators plus one offline recovery identity can
   decrypt the secret vault before shared passwords are rotated.
6. State-changing workflow requests are authenticated and authorized, runtime
   agents receive only the credentials they need, and workflow-api no longer
   has namespace-wide Secret read access.
7. Source-document ingestion cannot reach arbitrary network targets or consume
   unbounded responses.

## Scope

### Included

- SOPS recipient management, encrypted secret storage, safe operator commands,
  backup, restore, and recovery.
- Removal and rotation of tracked GPU-runner credentials and historical PXE
  password verifiers.
- Removal of passwords from `/home/gr66ss/AGENTS.md` and the exo handoff note,
  with references to safe commands instead.
- Personal operator access and a staged collaborator migration.
- Authentication and authorization for workflow-api mutations.
- Removal of arbitrary runner image and entrypoint selection from untrusted
  requests; approved images are digest-pinned in policy.
- SSRF and response-size controls for source-document ingestion.
- Secret minimization for agent subprocesses and Kubernetes service accounts.
- Replacement of fail-open example credentials and command-line secret
  exposure.
- Safe backup/restore behavior and coverage of all current local secret files.

### Excluded

- Deprecated WhatsApp services and their findings.
- A general identity provider, LDAP deployment, or external secrets operator.
- Redesigning the product workflow beyond the controls required above.
- Treating public Git history rewriting as credential rotation. Exposed values
  are rotated first; history cleanup is a separately approved destructive task.

## Architecture

### Independent access planes

Glasslab retains separate access planes:

```text
personal SSH key ---> gateway/provisioner/exo login ---> personal sudo
personal GitHub ID -----------------------------------> repository/GHCR workflow
personal kubeconfig ---------------------------------> Kubernetes authorization
personal SOPS key ---> decrypt approved secret files -> secret operations
offline recovery key -------------------------------> SOPS re-enrollment only
```

None of the first three arrows depends on SOPS. SOPS recipient possession also
does not implicitly grant a Unix account, sudo, GitHub membership, or Kubernetes
RBAC.

Denise, Tristan, and Tyler are enrolled as full operators using individual
identities. Their committed role assignments must provide the operational
capabilities they actually need, including infrastructure administration and
an administrator kubeconfig where required. The implementation must not copy
or share Tyler's private SSH key or kubeconfig.

### SOPS key model

Use SOPS with individual OpenPGP recipients for the initial deployment because
GnuPG is already available on the laptop and provisioner. Each operator creates
a passphrase-protected key locally and contributes only the public fingerprint.
The repository records fingerprints, roles, enrollment state, and instructions.
Private keys never enter Git, Ansible variables, or another operator's home.

Create a distinct offline recovery recipient. Its private key is exported to
encrypted removable media or an institutionally controlled offline location,
tested, and removed from online hosts. It is not used for routine operations.

SOPS policy must require all active operator recipients and the recovery
recipient for every current Glasslab secret file. Recipient changes use
`sops updatekeys`, are reviewed, and are verified by every remaining operator.

### Vault location

The public repository contains no encrypted live payloads. It contains:

- public recipient fingerprints and `.sops.yaml` creation rules;
- schemas and redacted example manifests;
- enrollment, rotation, backup, and recovery documentation;
- scripts that locate the private vault through a configured path.

The encrypted SOPS vault lives outside the public checkout on the provisioner
and in an encrypted off-host backup. Default location:
`/home/glasslab/.local/share/glasslab-secrets`. A local configuration file may
override this path without being tracked. Keeping ciphertext out of the public
repository limits permanent disclosure and metadata leakage while allowing the
public repo to remain the complete operating manual.

The vault mirrors logical deployment paths and includes an inventory manifest
with secret name, namespace or host target, owner, rotation date, and source
file. It covers current workflow-api, research-orchestrator, v1 agent, Postgres,
MinIO, NATS, model-serving, registry, and break-glass credentials. The inventory
contains no secret values.

### Operator interface

Provide `scripts/glasslab-secret` with these stable operations:

- `doctor`: check SOPS/GPG availability, recipient enrollment, vault permissions,
  expected inventory, and decrypt capability without printing values.
- `edit <name>`: open a named SOPS document in the user's editor.
- `apply <name|all>`: decrypt to a private temporary directory, validate the
  manifest, apply it, and reliably remove plaintext on exit.
- `get <name> <field>`: write a single value to standard output only after an
  explicit command; warn when stdout is a terminal.
- `exec-env <name> -- <command>`: provide selected values through the child
  environment, never argv, and restore the parent environment afterward.
- `backup <destination>` and `restore <archive>`: preserve encrypted SOPS files
  only and verify checksums and inventory before replacement.
- `updatekeys`: update recipients and report which documents changed.

The helper rejects world-readable vault/config files, missing encrypted files,
unknown secret names, and committed example manifests. It uses a `0700`
temporary directory under the invoking user's runtime directory, creates files
with `0600`, installs signal/exit cleanup traps, and never enables shell tracing.

No helper accepts a password or token as a command-line option. Tools such as
Docker receive tokens via stdin; Kubernetes values are applied from stdin or a
private temporary file; curl uses protected config/stdin where appropriate.

## Collaborator Migration

### Public communication

Before rotating anything, merge a repository notice and GitHub tracking issue
that state:

- why credential handling is changing;
- which personal access paths each collaborator must verify;
- installation and SOPS enrollment commands;
- the migration deadline and recovery contact;
- that old passwords remain temporarily available until verification;
- which shared credentials will stop working and when.

Add `scripts/check-my-access.sh`. It checks key-only SSH to the gateway,
provisioner, exo17, and exo18, then checks personal sudo, repository access,
Docker access where assigned, Kubernetes administrator capability, and SOPS
`doctor`. It reports each boundary independently, so lack of SOPS never appears
as loss of machine access.

### Staged sequence

1. Correct the public identity ledger so all three named operators have the
   approved operational roles, using personal accounts and least privilege.
2. Each collaborator verifies key-only access from every active client and
   records confirmation on the migration issue.
3. Each collaborator generates a personal SOPS key and verifies `doctor` and a
   non-printing decrypt test.
4. Create and test the offline recovery recipient.
5. Import every current local secret into the encrypted vault and verify that
   each of the three operators can perform a controlled apply.
6. Back up the encrypted vault off-host and perform a recovery drill.
7. Lock collaborator passwords in separate reviewed changes only after their
   key-only tests pass.
8. Remove plaintext handoff references, rotate the shared provisioner and exo
   passwords, and store the replacements only as encrypted break-glass values.
9. Remove the legacy shared account from normal workflows after confirming that
   two personal administrators can recover every access plane.

A failed check pauses only the affected person's retirement step. It does not
block other safe remediation work and never triggers bulk password locking.

## Application and Cluster Hardening

### Workflow authorization

All workflow-api mutation routes require an authenticated workload identity and
an operation-specific authorization decision. Initial implementation uses a
dedicated Kubernetes Secret token per approved caller, mounted only into that
caller and workflow-api. Tokens are compared safely, omitted from logs, and
rotatable without changing application images. Read-only health endpoints may
remain unauthenticated inside the cluster; sensitive reads require identity.

The registry, not request input, selects runner image, digest, entrypoint,
command shape, resource bounds, and service account. Requests may select only
approved parameters. NetworkPolicy restricts workflow-api ingress to named
caller pods and restricts runner egress to required services.

### Source-document ingestion

Remote document fetches accept only HTTPS, reject embedded credentials, resolve
DNS before connection, and deny loopback, link-local, private, multicast, and
cluster/service address ranges for every resolved address and redirect. Set
connect/read timeouts, a redirect ceiling, a response-byte ceiling, and an
allowlist where workflows can name known research sources. Stream responses;
do not call an unbounded `read()`.

### Least privilege and secret isolation

- Remove workflow-api `get/list/watch` access to Kubernetes Secrets. Secret
  deployment is an operator-side action, not an API runtime capability.
- Give each workload a dedicated service account with only required verbs and
  namespaced resources.
- Construct an explicit minimal environment for Hermes/OpenCode subprocesses;
  do not inherit the orchestrator process environment. Pass only scoped,
  short-lived values required for that task.
- Deployment helpers fail closed when a live secret is absent. Example values
  are never deployable defaults.
- Replace every secret-bearing argv use, including registry, vLLM, MinIO, and
  dataset helpers, with stdin, protected files, or scoped environment delivery.

### Tracked credential cleanup

Remove the GPU-runner Secret from both the component and aggregate manifests,
replace it with a redacted example, and rotate the referenced PostgreSQL
credential if the endpoint ever existed. Do not assume the currently different
live hash proves the old credential was never valid.

Replace tracked PXE password verifiers with locked-password/key-only templates.
Before altering a live profile, verify that the provisioner key is injected and
that console recovery exists. Historical values are considered compromised even
if password SSH is disabled.

## Backup and Restore

Replace the symmetric, stale-scope backup path with an inventory-driven backup
of encrypted SOPS files, public policy, and checksums. Backups never contain a
second plaintext archive. Store at least one encrypted copy off the provisioner.

Restore extracts only encrypted files into a new `0700` staging directory,
verifies paths, inventory, SOPS metadata, and checksums, then atomically swaps
the vault after confirmation. It never writes predictable `/tmp` filenames,
prints Kubernetes Secret manifests, or overwrites the active vault before
validation. A quarterly recovery drill decrypts one canary, validates access,
and records only success metadata.

## Failure and Recovery Rules

- Lost operator SOPS key: another enrolled operator verifies the person's
  identity, adds their new public recipient, runs `updatekeys`, and removes the
  lost recipient. SSH/sudo/Kubernetes remain usable throughout.
- Last online SOPS key unavailable: use the offline recovery identity to add at
  least two new individual recipients, then return the recovery key offline.
- SOPS unavailable on the provisioner: operators can still administer hosts and
  Kubernetes. They install the pinned tool from the documented package/checksum
  path or apply already-present cluster Secrets without revealing them.
- Suspected plaintext exposure: rotate the affected credential first, then
  remove artifacts and assess history. Deletion is not treated as rotation.
- Partial apply: the helper reports the exact failed object, preserves encrypted
  source, removes plaintext staging, and does not continue to unrelated secrets
  unless explicitly requested.

## Verification

Automated checks must cover:

- repository and history scans for known credential patterns and the specific
  exposed values/verifiers;
- shell tests proving secrets never appear in argv, logs, predictable temp
  paths, or example-manifest fallbacks;
- SOPS helper tests with disposable GPG homes, multiple recipients, revocation,
  missing vault files, cleanup on signals, backup, and restore corruption;
- access-check output for independent pass/fail states;
- workflow-api tests for missing, invalid, unauthorized, and valid caller
  identity on every mutation route;
- policy tests proving request data cannot select arbitrary images,
  entrypoints, commands, or service accounts;
- SSRF tests for IP literals, DNS rebinding-resistant resolution, redirects,
  private networks, excessive size, and timeouts;
- RBAC assertions that workflow-api cannot read Secrets;
- subprocess tests proving sensitive orchestrator variables are absent.

Live acceptance from the canonical provisioner requires:

1. Each of the three operators passes key-only gateway, provisioner, and exo
   login and can use their assigned personal administrative paths.
2. Each passes SOPS `doctor`, decrypts a canary without printing it, and performs
   one controlled secret apply.
3. Offline recovery restores the canary into an isolated staging vault.
4. The encrypted off-host backup covers every inventory entry.
5. Workflow smoke tests pass with valid identity and fail without it.
6. Kubernetes authorization confirms workflow-api cannot read Secrets.
7. A final repository scan finds no plaintext passwords, deployable example
   secrets, published password verifiers, or secret-bearing command examples.

No live password is rotated until acceptance items 1 through 4 pass for at
least two administrators. Denise's and Tristan's individual password-lock
changes remain separate and require their own recorded confirmations.

## Delivery Order

Implementation is divided into reviewable phases:

1. Public migration notice, identity-role corrections, and access checks.
2. SOPS policy, operator helper, individual enrollment, and offline recovery.
3. Vault migration, encrypted backup/restore, handoff cleanup, and credential
   rotations.
4. Workflow authentication, approved runner policy, RBAC, environment isolation,
   and SSRF controls.
5. Final live validation, password retirement, and security rescan.

Each phase must be independently reversible until credential rotation. Rotation
records identify the credential and date but never its value.
