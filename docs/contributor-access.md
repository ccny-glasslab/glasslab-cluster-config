# Contributor Access

Glasslab uses separate identities at each trust boundary. Logging into the
gateway does not automatically grant Docker, repository, Kubernetes, or GitHub
package permissions.

| Boundary | Account or role | Granted capability |
| --- | --- | --- |
| Public gateway | personal Unix account | enter the lab network over SSH |
| Provisioner | matching personal Unix account | work in the canonical checkout |
| Canonical checkout | `glasslab` group | inspect and operate the live checkout when authorized |
| Local Docker daemon | `docker` group, explicitly approved | build and test images on the provisioner |
| GitHub repository | personal GitHub collaborator | push branches and trigger workflows |
| GHCR publication | GitHub Actions `GITHUB_TOKEN` | publish approved service images |
| Kubernetes administration | administrator workflow | not implied by contributor access |

The `docker` group is root-equivalent on the provisioner. It is appropriate
only for the currently trusted contributors and must not become the default
for future untrusted accounts.

## Normal Connection Path

Each contributor should define local SSH aliases equivalent to:

```sshconfig
Host glasslab-gateway
  HostName glasslab.org
  User <personal-user>
  IdentityFile ~/.ssh/<personal-key>

Host glasslab-provisioner
  HostName 192.168.1.44
  User <same-personal-user>
  IdentityFile ~/.ssh/<personal-key>
  ProxyJump glasslab-gateway
  # Signed research links open at http://127.0.0.1:18080 while this session is
  # open (the provisioner keeps the persistent forward; see below).
  LocalForward 18080 127.0.0.1:18080

Host glasslab-exo17
  HostName 192.168.1.17
  User <same-personal-user>
  IdentityFile ~/.ssh/<personal-key>
  ProxyJump glasslab-gateway

Host glasslab-exo18
  HostName 192.168.1.18
  User <same-personal-user>
  IdentityFile ~/.ssh/<personal-key>
  ProxyJump glasslab-gateway
```

Public keys should be installed on both hosts. Password reuse between the
gateway and provisioner is not the account synchronization mechanism.

Password retirement is staged. Existing passwords remain available until the
contributor has demonstrated key-only login from every computer they actively
use. An administrator then changes the account's committed
`password_locked` setting and reapplies the identity playbook.

## Inspecting Signed Research Links

Research reports and artifacts are exported to Discord as signed links. The
provisioner runs the port-forward persistently, so no contributor starts one by
hand:

- A systemd unit on the provisioner (`glasslab-orchestrator-forward.service`)
  keeps `127.0.0.1:18080` forwarded to the research orchestrator service.
- The `LocalForward 18080 127.0.0.1:18080` line in the `glasslab-provisioner`
  block above maps that port to the same local port, so a signed link opens at
  `http://127.0.0.1:18080/links/...` while your SSH session is open.

The link host is `127.0.0.1`, so a link only resolves on a machine that holds an
SSH session; access stays bounded by the SSH whitelist and the signed,
expiring token rather than by network exposure.

## Inspecting The Corpus And Reports (Read-Only UI)

The orchestrator serves a read-only, no-JavaScript page at `GET /ui/`. It shows
the runs, corpus sources, and context packets, a digest-verified text preview
of a linkable artifact, and a citation inspector. The route sits behind the
same operator token as the rest of the API, so a browser can't open it
directly. A small local proxy injects the token for you.

Prerequisites:

- The `glasslab-provisioner` block above already forwards
  `127.0.0.1:18080` while your SSH session is open.
- Your operator token is exported in the environment as
  `GLASSLAB_ORCHESTRATOR_OPERATOR_API_TOKEN`. Read it from the deployment
  secret. Never paste the value into a file, shell history, issue, or chat.

Start the proxy on the workstation. It binds `127.0.0.1:19090` and forwards to
`127.0.0.1:18080`:

```bash
python3 scripts/glasslab-orchestrator-ui-proxy.py
```

Then open:

```text
http://127.0.0.1:19090/ui/
```

The proxy reads the token from the environment and adds
`X-Glasslab-Operator-Token` to every upstream request. The token stays in the
proxy process and never enters the browser: it is not accepted as a
command-line argument, not written to disk, and not logged. The proxy is
loopback-only and read-only by default. It refuses to start when the token
variable is unset or empty, only forwards `GET` and `HEAD` (any other method
gets `405`), and rejects a request whose `Host` header doesn't name the
loopback listener (a DNS-rebinding name gets `421`).

See the [Corpus UI runbook](glasslab-v2/runbooks/orchestrator-corpus-ui.md) for
the proxy flags, the pane layout, the citation badges, and troubleshooting.

## Development Checkouts

Contributors must develop in separate clones under their own home directories.
Do not use `/home/glasslab/cluster-config` as a shared development worktree;
simultaneous branches, builds, and generated files would interfere with each
other and with live rollouts.

After authenticating the GitHub CLI, create a personal checkout:

```bash
cd ~
gh repo clone ccny-glasslab/glasslab-cluster-config cluster-config
cd ~/cluster-config
git switch -c <personal-feature-branch>
```

The checkout under `/home/glasslab/cluster-config` remains the clean canonical
apply and validation tree.

## Building And Publishing Images

After an administrator grants `docker` membership, start a new SSH login before
testing it:

```bash
id
docker info
```

Local Docker access is for builds and checks. The normal GHCR publication path
is GitHub Actions, so contributors do not need a shared GHCR token on `.44`.

Authenticate the GitHub CLI as your own GitHub account:

```bash
gh auth login --web
```

Then request a repository-owned build:

```bash
./scripts/request-service-image.sh workflow-api
./scripts/request-service-image.sh research-orchestrator
```

The caller must be a repository collaborator. The workflow records who
requested the build, builds from committed `main`, and publishes with its
short-lived `GITHUB_TOKEN`.

## Provisioning Accounts

Gateway, provisioner, and exo personal accounts are managed from one committed
identity ledger. See [Identity Management](identity-management.md) for the
role model, revocation procedure, and recovery boundary.

Run from the canonical checkout on the provisioner:

```bash
cd /home/glasslab/cluster-config
./scripts/manage-identities.sh check
./scripts/manage-identities.sh apply
```

Public keys and role assignments live in
`ansible/group_vars/identity_hosts.yml`. Private keys, passwords, and access
tokens must not be added there.
