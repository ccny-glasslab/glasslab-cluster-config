# Glasslab Access Topology

Use role names rather than IP-derived nicknames when describing the remote
administration path.

| Canonical name | Address | Hostname | Responsibility |
| --- | --- | --- | --- |
| Glasslab | n/a | n/a | The overall lab and project |
| gateway | `glasslab.org` | `glasslab` | Public SSH entry point; small Apache site on port 80 ([gateway-web.md](gateway-web.md)) |
| provisioner | `192.168.1.44` | `glasslab-PXE-01` | PXE, Ansible, canonical repo, image builds, and `kubectl` |
| control plane | `192.168.1.49` | `cp01` | Kubernetes API and control plane |
| workers | lab LAN addresses | `node01` through `node05` | Kubernetes workloads |

The gateway and provisioner are separate machines:

```text
contributor workstation
        |
        | ssh glasslab-gateway
        v
public gateway at glasslab.org
        |
        | ProxyJump
        v
internal provisioner at 192.168.1.44
        |
        +--> Kubernetes API on cp01
        +--> Ansible management of cluster nodes
```

## SSH Names

Canonical personal aliases:

```bash
ssh glasslab-gateway
ssh glasslab-provisioner
```

The shared-administrator aliases target the legacy shared `glasslab` account:

```bash
ssh glasslab-gateway-admin
ssh glasslab-provisioner-admin
```

Key-only hardening (`ansible/playbooks/harden-ssh-key-only.yml`) publishes
`AllowUsers` from the identity ledger, which lists only the personal accounts
`gr66ss-glasslab`, `denic`, and `tristanc`. The shared `glasslab` `*-admin`
logins and `root` are consequently removed from sshd and are no longer
reachable over SSH; the aliases remain only for historical reference. Recover a
host through its console or out-of-band management (IPMI/iDRAC) rather than
re-enabling password or root SSH; see
[SSH Lockout Recovery](#ssh-lockout-recovery).

The older `glasslab-bastion`, `glasslab-44`, `glasslab-bastion-admin`, and
`glasslab-44-admin` aliases remain compatible. New documentation and scripts
must use the canonical role names.

## Identity Names

`glasslab` is also the legacy shared Unix administrator account on multiple
hosts. It is not a host name in architecture prose. Prefer personal accounts
for normal access and state the shared account explicitly as `the shared
glasslab account` when it is unavoidable.

## Operational Boundaries

- The gateway terminates public SSH access. It also serves a small Apache
  site on port 80, described in [gateway-web.md](gateway-web.md). It is not
  the canonical repo, Ansible controller, PXE host, or Kubernetes workstation.
- The provisioner is the canonical live checkout and cluster administration
  host. It is not publicly reachable without the gateway hop.
- Kubernetes workers are not normal contributor login targets. Research work
  should enter through the orchestrator and bounded job APIs.
- Ansible runs from the provisioner and currently manages the control plane and
  workers. Contributor access on the provisioner is described in
  [contributor-access.md](contributor-access.md).

## SSH Lockout Recovery

### Symptom: sshd Closes Before The Banner

If `ssh glasslab-provisioner` gets through the gateway hop and then fails with:

```text
kex_exchange_identification: Connection closed by remote host
```

then the provisioner's sshd is accepting TCP connections, but the
per-connection sshd child exits before it sends its `SSH-2.0-...` banner. The
client never gets as far as sending a username, so this locks out **every**
account at once: the personal accounts, the shared `glasslab` account, and
root. `AllowUsers`, `Match` blocks, and authorized keys are never evaluated, so
a break-glass or shared-admin SSH login does not help. Ansible cannot repair it
either, because Ansible runs from the provisioner. Other services on the
provisioner (for example nginx on port 80) and other hosts can stay healthy
throughout. This happened on 2026-10-19.

To confirm from the gateway that no banner is sent:

```bash
ssh glasslab-gateway \
  "timeout 3 bash -c 'exec 3<>/dev/tcp/192.168.1.44/22; head -1 <&3'"
```

A healthy sshd prints an `SSH-2.0-` line. A pre-banner failure prints nothing.
Compare against `cp01` (`192.168.1.49`) to rule out the gateway.

### Console And Out-Of-Band Access

The provisioner (`glasslab-PXE-01`) is a Dell server. Repair needs its local
console or its iDRAC session, and console and physical access to it are owned
by CCNY lab/IT staff, not by Glasslab contributors. Contact them to get someone
at the console. The iDRAC address and any console or iDRAC credentials are
deliberately not recorded in this repository. Request them from CCNY IT through
an authenticated channel, and never put them in Git, issues, PRs, screenshots,
or handoff notes.

### Read-Only Triage At The Console

Log in locally with an account that has sudo, then collect evidence before
changing anything:

```bash
sudo journalctl -u ssh -u ssh.socket --since -3d --no-pager | tail -80
systemctl status ssh ssh.socket --no-pager
ls -ld /run/sshd
sudo sshd -t
grep -i openssh /var/log/apt/history.log /var/log/dpkg.log
zgrep -i openssh /var/log/apt/history.log.*.gz
ls -l /etc/ssh/ssh_host_*
cat /etc/hosts.allow /etc/hosts.deny
```

What to look for:

- `Missing privilege separation directory: /run/sshd`, or `ls` reports that
  `/run/sshd` does not exist. This is the most likely cause. On Ubuntu 24.04
  with `ssh.socket` activation, stopping or restarting `ssh.service` (for
  example through an unattended openssh upgrade under
  `ansible/playbooks/maintain-packages.yml`, or the sshd restart in
  `ansible/playbooks/harden-ssh-key-only.yml`) can leave the listener running
  without its runtime directory.
- `no hostkeys available` or `Could not load host key`: host keys are missing or
  unreadable.
- `sshd -t` prints an error: the configuration is invalid and names the file
  and line.
- An entry in `/etc/hosts.deny` matching the gateway or the LAN: if this sshd
  build still honours TCP wrappers, it rejects the connection after accepting
  it.

### Recovery

Apply only the fix that matches the evidence:

- Missing `/run/sshd`, or `ssh.service` failed or inactive:

  ```bash
  sudo systemctl restart ssh
  ```

  Restarting the service recreates `/run/sshd`. If
  `systemctl is-active ssh.socket` reports `active`, restart both:

  ```bash
  sudo systemctl restart ssh.socket ssh
  ```

- Configuration error: fix the file that `sshd -t` names, rerun `sudo sshd -t`
  until it is silent, then restart as above.
- `hosts.deny` entry: remove the entry. No restart is needed.
- Host keys unreadable: restore ownership and permissions (`root:root`, mode
  `0600` for the private keys), then restart.
- Host keys actually missing: restore them from the SSH recovery backup if one
  exists. Run `sudo ssh-keygen -A` only as a last resort.

Do not run `ssh-keygen -A` as a generic fix. It generates new keys for every
missing key type, which changes the host fingerprint, so every client's
`known_hosts` entry for the provisioner has to be updated and the new
fingerprint verified out of band. Do not re-enable password or root SSH either.
It cannot help a failure that happens before authentication.

Verify from a contributor workstation:

```bash
ssh glasslab-provisioner true
```

Then record the cause and the fix (without secrets) in the tracking issue or
handoff.
