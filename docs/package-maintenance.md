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
- it does not reboot hosts automatically
- if a kernel or core library update sets `/var/run/reboot-required`, schedule a controlled reboot afterward

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
