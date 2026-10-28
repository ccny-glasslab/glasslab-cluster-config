# Gateway SSH Allowlist

The public gateway (`glasslab.org`, hostname `glasslab`) accepts SSH only from
addresses listed in `/etc/hosts.allow`: sshd is built with TCP wrappers and
`/etc/hosts.deny` is `ALL: ALL`. Campus (`134.74.`), the lab LAN
(`192.168.`), a few residential ISP domains, and individual home addresses are
listed. Accounts are separately limited by `AllowUsers`, from the identity
ledger in `ansible/group_vars/identity_hosts.yml`.

The file is not tracked in this repository, because it lists contributors'
home addresses. Change it on the gateway with the helper below; its backups
and syslog entries are the change record.

## Symptom

A user whose address is not listed is dropped right after the TCP connection,
before key exchange or authentication:

```text
kex_exchange_identification: read: Connection reset
Connection reset by 134.74.146.39 port 22
```

On the gateway, sshd logs the refusal with the source address:

```bash
ssh glasslab-gateway 'sudo journalctl -u ssh --since -1h --no-pager | grep "refused connect"'
# refused connect from host.example.net (203.0.113.7)
```

Confirm the address with the user (for example from whatismyip.com on that
computer) before allowing it. Home addresses are dynamic and can change.

## Allow An Address

Use the helper, never a hand edit:

```bash
ssh glasslab-gateway sudo /usr/local/sbin/glasslab-allow-ssh 203.0.113.7 "jdoe home (example ISP)"
```

[`glasslab-allow-ssh`](../live-config/gateway/usr/local/sbin/glasslab-allow-ssh)
accepts one plain dotted-quad public IPv4 address (no CIDR, partial address,
hostname, or wildcard) and a 1-80 character comment of letters, digits, space,
and `. _ , / ( ) -`. It refuses an address already allowed for sshd, backs up
the file as `/etc/hosts.allow.bak-<timestamp>-glasslab-allow-ssh`, appends a
comment line and `sshd: <address>` through a temporary file and an atomic
rename, and logs to syslog (`journalctl -t glasslab-allow-ssh`). The change
applies to the next connection; nothing restarts.

Put comments on their own `#` line. `hosts_access(5)` has no trailing
comments: in `sshd: 1.2.3.4 # home`, `#` and `home` are read as more client
patterns, and a word such as `ALL` there would allow everyone.

To remove an address, edit `/etc/hosts.allow` by hand with sudo, keeping a
backup.

## Install The Helper

Root and sudoers changes are run by an administrator from a workstation
checkout of `main`:

```bash
scp live-config/gateway/usr/local/sbin/glasslab-allow-ssh \
    live-config/gateway/etc/sudoers.d/91-glasslab-allow-ssh glasslab-gateway:/tmp/
ssh -t glasslab-gateway 'sudo visudo -cf /tmp/91-glasslab-allow-ssh \
  && sudo install -o root -g root -m 0755 /tmp/glasslab-allow-ssh /usr/local/sbin/glasslab-allow-ssh \
  && sudo install -o root -g root -m 0440 /tmp/91-glasslab-allow-ssh /etc/sudoers.d/91-glasslab-allow-ssh \
  && rm /tmp/glasslab-allow-ssh /tmp/91-glasslab-allow-ssh'
```

The sudoers drop-in lets `gr66ss-glasslab` run only this helper without a
password. Today that account also has `NOPASSWD: ALL` from the identity
ledger, so the drop-in only narrows anything once that broader grant is
removed.

## Allowlist-Only Key (For Automation)

Because the gateway account has passwordless root, a rule such as "may run
`ssh glasslab-gateway sudo -n glasslab-allow-ssh ...`" is not narrow: the
remote shell runs anything appended to the command. An agent or script that
should only add addresses gets a dedicated key whose `authorized_keys2` entry
forces the helper, whatever command the client sends:

1. On the workstation, create the key and an SSH alias:

   ```bash
   ssh-keygen -t ed25519 -N '' -C 'glasslab-allowlist-only' -f ~/.ssh/id_ed25519_glasslab_allowlist
   cat >> ~/.ssh/config <<'EOF'

   Host glasslab-gateway-allowlist
     HostName glasslab.org
     User gr66ss-glasslab
     IdentityFile ~/.ssh/id_ed25519_glasslab_allowlist
     IdentitiesOnly yes
   EOF
   ```

2. Install the forced-command entry on the gateway. sshd reads
   `.ssh/authorized_keys2` as well as `.ssh/authorized_keys`, and the identity
   playbook rewrites only the latter, so the entry survives ledger runs:

   ```bash
   ssh glasslab-gateway "umask 077; printf '%s %s\n' \
     'restrict,command=\"sudo -n /usr/local/sbin/glasslab-allow-ssh --ssh-command \\\"\$SSH_ORIGINAL_COMMAND\\\"\"' \
     '$(cat ~/.ssh/id_ed25519_glasslab_allowlist.pub)' >> ~/.ssh/authorized_keys2"
   ```

   `restrict` turns off forwarding, PTYs, and agent forwarding. The client's
   whole command arrives as `SSH_ORIGINAL_COMMAND`, which the helper accepts
   only if all of it is `<ipv4> <comment>`.

3. Use it:

   ```bash
   ssh glasslab-gateway-allowlist '203.0.113.7 jdoe home (example ISP)'
   ```

A Claude Code permission rule can then allow exactly this alias, for example
`"Bash(ssh glasslab-gateway-allowlist:*)"` in `~/.claude/settings.json`. Whatever
is sent through that key can only reach the helper.

Revoke the key by deleting its line from `~/.ssh/authorized_keys2` on the
gateway.
