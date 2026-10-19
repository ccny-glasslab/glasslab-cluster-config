# Gateway Live Config Snapshot

Tracked copy of the public gateway's (`glasslab.org`, hostname `glasslab`)
web-serving config. What it serves and why is described in
[docs/gateway-web.md](../../docs/gateway-web.md).

Tracked files:

- `etc/apache2/sites-available/000-default.conf` — the `*:80` site.
- `etc/apache2/sites-available/glasslab-ssl.conf` — the `*:443` site, added
  2026-10-19. Both are enabled through symlinks in `sites-enabled/`.
- `etc/apache2/glasslab/*.conf` — route fragments that the two sites
  `Include` by absolute path, added 2026-10-19 17:38 for the exam JupyterHub:
  `exam-route.conf` (included from `glasslab-ssl.conf`) and
  `exam-route-http.conf` (included from `000-default.conf`). The snapshot
  script copies every `*.conf` it finds in `/etc/apache2/glasslab/`.
- `etc/letsencrypt/acme-dns-auth.py` — certbot's DNS-01 auth hook (root,
  0700 on the gateway).
- `etc/letsencrypt/renewal/glasslab.org.conf` — certbot's renewal settings for
  the `glasslab.org` certificate. It is written by certbot and kept for
  reference; the restore script does not push it.

Never tracked: `/etc/letsencrypt/acmedns.json` (acme-dns credentials) and the
certificate and keys under `/etc/letsencrypt/live` and `archive`.

Refresh with `scripts/snapshot-gateway-config.sh` and push back with
`scripts/restore-gateway-config.sh`, both from a contributor workstation that
can `ssh glasslab-gateway`. See [docs/version-control.md](../../docs/version-control.md).

## Firewall (ufw)

ufw is not snapshotted by script. Rules as checked on 2026-10-19 17:45
(`sudo ufw status verbose` / `sudo ufw show added`):

- Default: deny incoming, allow outgoing, routed disabled.
- `ufw allow Apache` — 80/tcp, IPv4 and IPv6.
- `ufw allow OpenSSH` and `ufw allow 22/tcp` — 22/tcp, IPv4 and IPv6 (the
  second rule duplicates the first).
- `ufw allow 443/tcp comment "HTTPS (Apache glasslab-ssl, Lets Encrypt)"` —
  IPv4 and IPv6, since 2026-10-19 16:28 when the HTTPS site went live. Earlier
  that day it was briefly `ufw reject 443/tcp` (so HTTPS-first browsers failed
  fast and fell back to `http://`), and before that the default policy dropped
  443. ufw replaced the reject rule in place. Pre-change rule files are in
  `/root/ufw-bak-20261019-162839-https/` on the gateway.
