# Gateway Live Config Snapshot

Tracked copy of the public gateway's (`glasslab.org`, hostname `glasslab`)
web-serving config. What it serves and why is described in
[docs/gateway-web.md](../../docs/gateway-web.md).

Tracked files:

- `etc/apache2/sites-available/000-default.conf` — the only enabled Apache
  site (`sites-enabled/000-default.conf` is a symlink to it).

Refresh with `scripts/snapshot-gateway-config.sh` and push back with
`scripts/restore-gateway-config.sh`, both from a contributor workstation that
can `ssh glasslab-gateway`. See [docs/version-control.md](../../docs/version-control.md).

## Firewall (ufw)

ufw is not snapshotted by script. Rules as checked on 2026-10-19
(`sudo ufw status verbose` / `sudo ufw show added`):

- Default: deny incoming, allow outgoing, routed disabled.
- `ufw allow Apache` — 80/tcp, IPv4 and IPv6.
- `ufw allow OpenSSH` and `ufw allow 22/tcp` — 22/tcp, IPv4 and IPv6 (the
  second rule duplicates the first).

- `ufw reject 443/tcp comment "fail fast so HTTPS-first browsers fall back to
  http"` — IPv4 and IPv6, added 2026-10-19. Apache listens on 443 because
  `mod_ssl` is enabled (`ports.conf`), but no 443 site is enabled. Before this
  rule the default policy dropped 443, so browsers that try HTTPS first hung
  instead of falling back to port 80.
