# Glasslab Access Topology

Use role names rather than IP-derived nicknames when describing the remote
administration path.

| Canonical name | Address | Hostname | Responsibility |
| --- | --- | --- | --- |
| Glasslab | n/a | n/a | The overall lab and project |
| gateway | `glasslab.org` | `glasslab` | Public SSH entry point only |
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
re-enabling password or root SSH.

The older `glasslab-bastion`, `glasslab-44`, `glasslab-bastion-admin`, and
`glasslab-44-admin` aliases remain compatible. New documentation and scripts
must use the canonical role names.

## Identity Names

`glasslab` is also the legacy shared Unix administrator account on multiple
hosts. It is not a host name in architecture prose. Prefer personal accounts
for normal access and state the shared account explicitly as `the shared
glasslab account` when it is unavoidable.

## Operational Boundaries

- The gateway terminates public SSH access. It is not the canonical repo,
  Ansible controller, PXE host, or Kubernetes workstation.
- The provisioner is the canonical live checkout and cluster administration
  host. It is not publicly reachable without the gateway hop.
- Kubernetes workers are not normal contributor login targets. Research work
  should enter through the orchestrator and bounded job APIs.
- Ansible runs from the provisioner and currently manages the control plane and
  workers. Contributor access on the provisioner is described in
  [contributor-access.md](contributor-access.md).
