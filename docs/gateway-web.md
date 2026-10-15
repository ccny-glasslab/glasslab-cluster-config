# Gateway Web Serving

The gateway (`glasslab.org`, hostname `glasslab`) is primarily the public SSH
entry point (see [access-topology.md](access-topology.md)), but it also runs
Apache 2.4 (Ubuntu package) on port 80. This page records what that Apache
serves. The tracked config is
[`live-config/gateway/etc/apache2/sites-available/000-default.conf`](../live-config/gateway/etc/apache2/sites-available/000-default.conf);
firewall rules are in [`live-config/gateway/README.md`](../live-config/gateway/README.md).

Last checked live: 2026-10-15. Re-check before describing this as current.

## Port 80

One enabled site, `000-default.conf`, a `*:80` virtual host with
`DocumentRoot /var/www/html`. Apache applies `ProxyPass` rules in the order
they appear and the first match wins, so order matters.

| Path | Behavior | Notes |
| --- | --- | --- |
| `/dsei102/` | Static files from `/var/www/dsei102` | DSEI 10200 offline course reference (~1 GB, ~21k files) for students taking an exam in CCNY computer labs. Added 2026-10-15. `Require ip 134.74.0.0/16 127.0.0.1 ::1`: CCNY campus addresses plus local loopback; everyone else gets 403. `/dsei102` redirects (301) to `/dsei102/`. `Options None`, `AllowOverride None`, so no directory listings and no `.htaccess`. |
| `/` (everything else) | Reverse proxy to `http://134.74.126.109/` | The upstream returns a meta-refresh page to the CCNY Computer Science site. |
| `/sstfront/` | Intended proxy to `http://134.74.77.69:9999/sstfront/` (plus a `wss://` rule for `/sstfront/api/kernels`) | Dead. It is listed after `ProxyPass /`, so the root proxy catches `/sstfront/` first and the upstream returns 404. The `wss://` rule would also need `mod_proxy_wstunnel`, which is not enabled. Cleanup candidate; left in place. |

The `ProxyPass /dsei102 !` exclusion must stay **above** `ProxyPass /`.
Moved below it, the root proxy would forward `/dsei102/` to 134.74.126.109
and the static reference would disappear.

A commented-out `ProxyPass / http://192.168.1.251/` remains from the old
internal file server, which no longer answers.

Enabled proxy modules: `proxy`, `proxy_http`. Logs go to
`/var/log/apache2/access.log` and `error.log`.

## Port 443

Apache listens on 443 because `mod_ssl` is enabled, but no HTTPS site is
enabled (`default-ssl.conf` exists in `sites-available/` but is not linked),
and ufw has no rule allowing 443. HTTPS to `glasslab.org` therefore does not
connect.

## Changing The Gateway Web Config

1. Back up the live file on the gateway (`sudo cp ... .bak-<stamp>`) or let
   `scripts/restore-gateway-config.sh` do it.
2. Edit, run `sudo apache2ctl configtest`, then `sudo systemctl reload apache2`.
3. From a workstation, run `scripts/snapshot-gateway-config.sh`, review the
   diff, and open a PR into `testing`.

To roll back to a tracked version, check it out and run
`scripts/restore-gateway-config.sh`. It keeps a `.bak-<stamp>` copy, runs
`apache2ctl configtest`, puts the previous file back if the test fails, and
reloads Apache only if it passes. It does not touch ufw or the
`/var/www/dsei102` content, which is not tracked here.
