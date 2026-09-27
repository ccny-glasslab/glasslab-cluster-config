# Version Control Workflow

`/home/glasslab/cluster-config` is the canonical infrastructure repository.

What is tracked:

- Ansible inventory, variables, playbooks, docs, and helper scripts
- Snapshots of active provisioner PXE/web/service configuration under `live-config/provisioner/`

What is intentionally not tracked:

- Large binary artifacts such as ISOs and kernel/initrd images
- Timestamped `.bak-*` files created for safety during edits

Git remote:

- `origin` is reserved for `git@github-cluster-config:ccny-glasslab/glasslab-cluster-config.git`
- SSH uses the dedicated provisioner key `~/.ssh/id_ed25519_github_cluster_config`

Typical workflow:

1. Edit the live system files or Ansible content.
2. Run `scripts/snapshot-provisioner-config.sh` to copy active provisioner configs into the repo.
3. Review with `git status` and `git diff`.
4. Commit with a meaningful message.
5. Push with `git push` once the GitHub repo and key trust are in place.

Rollback workflow:

1. Use Git to check out the desired commit or file version in `/home/glasslab/cluster-config`.
2. Run `scripts/restore-provisioner-config.sh` to push the tracked snapshot back onto the live provisioner.
3. The restore script writes timestamped `.bak-<stamp>` backups before overwriting live files and restarts `dnsmasq`, `tftpd-hpa`, and `nginx`.

## Provisioner Snapshot And Restore

`scripts/snapshot-provisioner-config.sh` copies the active provisioner config
into `live-config/provisioner/`: `dnsmasq`/`tftpd-hpa` configs, the nginx
`sites-available/default` vhost, iPXE boot scripts, every
`var/www/html/pxe/cloud-init/<profile>/` directory, and the
`var/www/html/c` symlink tree.

`scripts/restore-provisioner-config.sh` applies that snapshot live:

- single files are installed with a `.bak-<stamp>` copy of the previous file;
- cloud-init profile directories are replaced wholesale (timestamped `.bak-*`
  files are preserved) and discovered from the snapshot, so adding a profile
  directory is enough to have it restored;
- `/var/www/html/c` symlinks are recreated from the snapshot;
- `dnsmasq`, `tftpd-hpa`, and `nginx` are restarted at the end.

The tracked PXE profiles deliberately lock the autoinstall identity password
(`password: '!'`) and disable SSH password auth; live files must not drift to
`$6$` password hashes. The tracked nginx vhost denies the direct
`/pxe/cloud-init` path — installers still fetch their data through the `/c/`
alias, which the deny does not affect. After restoring, verify with
`sudo nginx -T | grep -A2 'pxe/cloud-init'` and
`grep -c "password: '!'" live-config/provisioner/var/www/html/pxe/cloud-init/*/user-data`.

The external SOPS secret vault is intentionally excluded from the snapshot and
must be restored separately with `scripts/restore-glasslab-secrets.sh`.
