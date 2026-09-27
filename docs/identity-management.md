# Identity Management

Glasslab uses Ansible-managed local accounts. This is deliberately smaller
than LDAP: three current contributors and four identity-bearing machines do
not justify a directory service, its availability dependency, or another
credential store.

```text
personal SSH key
       |
       v
public gateway (glasslab.org)
       |
       +--> provisioner (.44): Git, scoped kubectl, optional Docker
       |
       +--> exo17 / exo18: personal shell, shared exo worktree

Kubernetes workers: no personal shell accounts
Kubernetes API: separate RBAC boundary
GitHub/GHCR: personal GitHub identity and GitHub Actions
```

## Source Of Truth

The account ledger is
`ansible/group_vars/identity_hosts.yml`. It records personal public keys,
target machine classes, and roles. Public keys are not credentials and are
committed so access can be reconstructed. Private keys and passwords are never
stored in the repository.

The inventory defines these identity scopes:

| Scope | Machines | Purpose |
| --- | --- | --- |
| `gateway` | `glasslab.org` | Public SSH entry only |
| `provisioner` | `192.168.1.44` | Repo, Ansible, builds, and cluster operations |
| `exo` | `192.168.1.17`, `192.168.1.18` | Distributed model-serving development |

The Kubernetes nodes are intentionally excluded. Interactive access to them
continues through the provisioner's `clusteradmin` automation identity.

## Roles

| Role | Effect |
| --- | --- |
| `lab_contributor` | Personal gateway login and provisioner `glasslab` group |
| `exo_contributor` | Personal exo login and `glasslab-exo` shared-worktree group |
| `container_builder` | Provisioner `docker` group; this is root-equivalent |
| `kubernetes_observer` | Personal `.44` kubeconfig with read-only workload access in approved namespaces |
| `infrastructure_admin` | `sudo` plus an audited passwordless sudoers entry |

Roles only take effect where they make sense. For example,
`container_builder` does not add a group on the gateway or Macs.

## Kubernetes Contributor Access

The `kubernetes_observer` role creates a personal client certificate and
`~/.kube/config` on the provisioner. It grants `get`, `list`, and `watch` for
pods, logs, events, jobs, workload controllers, services, and persistent volume
claims in `glasslab-v2` and `glasslab-agents`.

It does not grant access to secrets, pod exec or attach, job creation, node
resources, RBAC, or cluster mutation. Contributors submit GPU work through the
research orchestrator, which validates and records the requested job. Direct
worker SSH remains an infrastructure-admin recovery path rather than an
experiment interface.

From the provisioner, an observer can use:

```bash
kubectl get jobs
kubectl get pods
kubectl logs <pod>
kubectl config use-context glasslab-agents
kubectl auth can-i --list
```

The certificates are issued through the Kubernetes CSR API for one year and
renewed by the identity playbook when fewer than 30 days remain. RBAC binds the
certificate's username directly, so disabling or removing the role from a
ledger entry removes that user from the namespace bindings. The playbook also
removes credentials that it previously managed for a revoked observer.

The legacy shared `glasslab` account is not in the personal-account ledger. It
remains a service-owner identity for local and service use while remaining
software is migrated, but it is no longer an SSH break-glass path: key-only
hardening (`ansible/playbooks/harden-ssh-key-only.yml`) publishes `AllowUsers`
from the ledger (personal accounts only) and disables root SSH. Recover a host
through its console or IPMI rather than re-enabling the shared login. It is not
the normal contributor login.

## Apply Changes

Run identity changes from the canonical checkout on the provisioner. Connect
with agent forwarding so Ansible can use the operator's personal key for the
gateway and exo hosts.

```bash
ssh glasslab-provisioner
cd /home/glasslab/cluster-config
./scripts/manage-identities.sh check
./scripts/manage-identities.sh apply
```

The play runs one host at a time. It sets the exact approved SSH keys and role
groups, enforces each account's staged password-lock setting, validates sudoers
and sshd configuration, manages provisioner Kubernetes observer credentials,
and keeps exo shared files group-writable. A second `check` run should report no
changes except where a platform tool cannot report idempotence.

## Add Or Change A Contributor

1. Obtain the contributor's SSH public key through an authenticated channel.
2. Add a unique user record to `glasslab_identity_users` and its username to
   `glasslab_identity_managed_usernames`.
3. Assign only required targets and roles.
4. Run the check command, review the diff, then apply.
5. Initially set `password_locked: false` when adopting an account that
   already uses password authentication.
6. Have the contributor verify each intended SSH alias and run `id`.
7. Have the contributor force a key-only test from every active client:

   ```bash
   ssh -o PreferredAuthentications=publickey \
     -o PasswordAuthentication=no <alias>
   ```

8. After those tests are recorded, change `password_locked` to `true` in a
   separate reviewed change and apply again.

Do not accept a private key, add a shared password, or put a GitHub token in
Ansible variables. Installing a public key is not sufficient evidence that the
contributor's current SSH client possesses and offers the matching private key.

## Revoke Access

Set the user's `state` to `disabled`; do not delete the ledger entry. Apply the
playbook. On Linux this locks the password, changes the shell to `nologin`,
removes supplementary role groups, and removes `authorized_keys`. On macOS it
changes the shell to `/usr/bin/false`, removes supplementary role groups, and
removes `authorized_keys`. Home directories and audit evidence are preserved.

After revocation, separately remove repository access in GitHub. The identity
playbook removes managed kubeconfigs and the user from Kubernetes RoleBindings;
already issued certificates then authenticate as a user with no permissions.
Unix, GitHub, and Kubernetes remain intentional separate trust boundaries.

## Recovery

The `infrastructure_admin` personal account (`gr66ss-glasslab`) is the current
SSH administrative path. The legacy shared `glasslab` account is no longer a
key-only SSH login (see above); when SSH is unavailable, recover through the
host console or out-of-band management (IPMI/iDRAC). Add Mike's personal key as
a second `infrastructure_admin` record before relying on a single account.
Back up the provisioner's local secrets and SSH recovery material through the
planned encrypted off-host backup path; Git only reconstructs public identity
policy.
