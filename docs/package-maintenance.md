# Package Maintenance Notes

Current policy:

- automatic unattended upgrades are enabled for Ubuntu security updates on cluster nodes
- manual full-package maintenance is run from Ansible during a maintenance window
- managed hosts are configured to include phased Ubuntu updates so the lab can converge on the current candidate package versions instead of waiting for rollout percentages
- Kubernetes packages remain held by the bootstrap playbooks, so routine apt upgrades do not silently move the cluster version

Current scope:

- live package maintenance is configured on `k8s_nodes`
- the provisioner host is intentionally not included yet because local sudo for `glasslab` has not been wired into Ansible

Manual maintenance run:

```bash
cd /home/glasslab/cluster-config/ansible
ansible-playbook playbooks/maintain-packages.yml --limit k8s_nodes -K
```

Wrapper:

```bash
/home/glasslab/cluster-config/scripts/maintain-packages.sh --limit k8s_nodes -K
```

What the playbook does:

1. installs `unattended-upgrades` and `update-notifier-common`
2. enables `apt-daily.timer` and `apt-daily-upgrade.timer`
3. configures `20auto-upgrades`
4. configures `51glasslab-phased-updates`
5. configures `52glasslab-unattended-upgrades` for security updates
6. runs a full apt upgrade serially across the target hosts
7. reports whether `/var/run/reboot-required` exists afterward

Operational note:

- the playbook uses `serial: 1` so nodes are updated one at a time
- it does not reboot hosts itself; kured handles worker reboots (below)
- unattended upgrades do not autoremove packages, and `pigz` is kept installed:
  an autoremove of `pigz` on 2026-10-27 left the running containerd unable to
  unpack any image (`fork/exec /usr/bin/unpigz`) until it restarted

## Automatic worker reboots (kured)

`kubeadm/kured.yaml` runs kured on the worker nodes (not `cp01`). When
`/var/run/reboot-required` exists, kured drains the node, reboots it, waits
for it to come back, and uncordons it.

- window: 08:00-09:30 UTC daily (04:00-05:30 EDT / 03:00-04:30 EST)
- one node at a time; the lock never expires, so a node that fails to come
  back stops all further reboots until someone clears it
- no reboot without a successful drain (`--force-reboot=false`)
- any running JupyterHub student server (`component=singleuser-server`)
  blocks all reboots
- exam image-keeper pods are skipped by the drain and restart in place
- pinned stateful pods (postgres and minio on node01, nats on node05, exam
  hub on node03) are down for a few minutes while their node reboots

Apply from the canonical checkout:

```bash
kubectl apply -f /home/glasslab/cluster-config/kubeadm/kured.yaml
```

Pause all reboots (for example before an exam that starts before 09:30 UTC)
and resume:

```bash
kubectl -n kube-system annotate ds kured weave.works/kured-node-lock='{"nodeID":"manual"}'
kubectl -n kube-system annotate ds kured weave.works/kured-node-lock-
```

Not covered by kured, so reboot these by hand after a kernel update: `cp01`,
the provisioner, and the gateway (which serves `glasslab.org` and `/exam`).

## Agent tooling and dependency updates

Agent CLIs must not drift behind security fixes.

Lab Macs (`exo_macs`): `ansible/playbooks/maintain-darwin-agent-tools.yml`
moves OpenCode to the Homebrew formula (removing hand-installed npm-global
copies), installs the `com.glasslab.agent-tools-upgrade` LaunchDaemon that runs
`brew upgrade` for the managed formulae daily at 04:30, and asserts the
security version floors. Add a Mac to `exo_macs` to cover it.

```bash
scripts/maintain-darwin-agent-tools.sh --check --diff
scripts/maintain-darwin-agent-tools.sh
```

Upgrade log: `~<brew owner>/Library/Logs/glasslab-agent-tools-upgrade.log`.

Cluster images: `.github/dependabot.yml` opens update PRs into `testing` for
the OpenCode lockfile (`services/research-orchestrator/agent-tools/`), service
Python requirements, digest-pinned base images, and GitHub Actions, with a
short cooldown against freshly published releases.
`.github/workflows/dependency-audit.yml` audits the pinned set against
published advisories daily. Updated images still reach the cluster only through
the normal promotion to `main` and rollout runbook.
