#!/usr/bin/env bash
# DRAFT. Temporary personal admin account on node02 for the exam week, so the
# remaining exam steps don't need clusteradmin's password each time.
# Run once on node02:  sudo bash ~/exam/labbox/add-exam-admin.sh
# Remove after node02 rejoins the cluster (runbook Phase 3):
#   sudo rm -f /etc/sudoers.d/90-gr66ss-glasslab-exam && sudo userdel -r gr66ss-glasslab
set -euo pipefail
[[ $EUID == 0 && $(hostname) == node02 ]] || { echo 'Run with sudo on node02' >&2; exit 1; }
user=gr66ss-glasslab
key='ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIPEoJqi9YEfIF/DG7YIIKGazr2SBAAORdNP7n0x7hDcQ gr66ss-glasslab replacement 2026-08-22'
id "$user" >/dev/null 2>&1 || useradd -m -s /bin/bash -c 'temporary exam admin' "$user"
install -d -m 0700 -o "$user" -g "$user" "/home/$user/.ssh"
printf '%s\n' "$key" > "/home/$user/.ssh/authorized_keys"
chown "$user:$user" "/home/$user/.ssh/authorized_keys"
chmod 0600 "/home/$user/.ssh/authorized_keys"
f=/etc/sudoers.d/90-gr66ss-glasslab-exam
printf '# Temporary: exam week 2026-10-20..23. Remove when node02 rejoins the cluster.\n%s ALL=(ALL) NOPASSWD: ALL\n' "$user" > "$f.tmp"
chmod 0440 "$f.tmp"
visudo -cf "$f.tmp" && mv "$f.tmp" "$f"
echo "Account $user ready on node02:"
sudo -l -U "$user" | tail -1
