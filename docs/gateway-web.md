# Gateway Web Serving

The gateway (`glasslab.org`, hostname `glasslab`) is primarily the public SSH
entry point (see [access-topology.md](access-topology.md)), but it also runs
Apache 2.4 (Ubuntu package) on ports 80 and 443. This page records what that
Apache serves. The tracked config is under
[`live-config/gateway/`](../live-config/gateway/): `000-default.conf` (port 80),
`glasslab-ssl.conf` (port 443) and the certbot renewal files. Firewall rules
are in [`live-config/gateway/README.md`](../live-config/gateway/README.md).

Last checked live: 2026-10-19. Re-check before describing this as current.

## Port 80

One enabled site, `000-default.conf`, a `*:80` virtual host with
`DocumentRoot /var/www/html`. Apache applies `ProxyPass` rules in the order
they appear and the first match wins, so order matters.

| Path | Behavior | Notes |
| --- | --- | --- |
| `/dsei102/` | Static files from `/var/www/dsei102` | DSEI 10200 offline course reference (~1 GB, ~21k files) for students taking an exam in CCNY computer labs. Added 2026-10-15. `Require ip 134.74.0.0/16 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 127.0.0.1 ::1`: CCNY's public range, the RFC 1918 private ranges, and loopback; everyone else gets 403. The private ranges were added 2026-10-19 because some campus networks reach the gateway without NAT (a laptop on `192.168.4.x` was getting 403). Private source addresses are not routable from the internet, so this is still campus-only. `/dsei102` redirects (301) to `/dsei102/`. `Options None`, `AllowOverride None`, so no directory listings and no `.htaccess`. |
| `/` (everything else) | Static landing page from `/var/www/html` (`index.html` with a centered `glasslab_log.gif`) | Since 2026-10-19. Before that, `/` was reverse-proxied to `http://134.74.126.109/`, which returned a meta-refresh to the CCNY Computer Science site; those `ProxyPass /` lines are now commented out. The landing-page files are not tracked here. |
| `/sstfront/` | 404 | Disabled 2026-10-19 (commented out). It was a proxy to Jupyter at `http://134.74.77.69:9999/sstfront/`, always shadowed by the old root proxy, and would have become publicly reachable once that was removed. Re-enable only deliberately. |

If the root proxy is ever re-enabled, the `ProxyPass /dsei102 !` exclusion
must stay **above** `ProxyPass /`. Below it, the root proxy would forward
`/dsei102/` upstream and the static reference would disappear.

A commented-out `ProxyPass / http://192.168.1.251/` remains from the old
internal file server, which no longer answers.

Enabled proxy modules: `proxy`, `proxy_http`. Logs go to
`/var/log/apache2/access.log` and `error.log`.

## Port 443 (HTTPS)

Enabled 2026-10-19 so that browsers which try HTTPS first (students typing
`glasslab.org/dsei102`) get the page. Before that, 443 was dropped and later
rejected by ufw, and those browsers failed unless the user typed `http://`.

- Site: `glasslab-ssl.conf`, a `*:443` virtual host for `glasslab.org` and
  `www.glasslab.org`. Its routing mirrors port 80: the same `/dsei102/` block
  (same `Require ip` list) and the landing page from `/var/www/html`.
  **Keep the two files in sync**; an access-rule change made in only one of
  them leaves the other port open or closed.
- Certificate: Let's Encrypt, `CN=glasslab.org` + `www.glasslab.org`, in
  `/etc/letsencrypt/live/glasslab.org/`. The first one expires 2027-01-17.
- No HTTP→HTTPS redirect and no HSTS, on purpose. `http://` keeps working as a
  fallback, and off-campus clients can only use port 80 (below).
- `default-ssl.conf` (the Ubuntu sample) is still present and not enabled.

Reachability, checked 2026-10-19:

- From campus (a `134.74.x` source, the lab LAN, and a private `192.168.x`
  source): `https://glasslab.org/dsei102/` returns 200 with a valid
  certificate.
- From the internet, HTTPS does not get through: the CCNY border resets the
  connection, and no packets reach the gateway. Port 80 works from the
  internet, and `/dsei102/` returns 403 there as intended.

### Certificate issuance and renewal (DNS-01 via acme-dns)

The usual HTTP-01 check does not work here. The CCNY border resets Let's
Encrypt's (and letsdebug.net's) port-80 validation requests before they
reach the gateway, while ordinary internet clients get through. The
`glasslab.org` zone is hosted at Squarespace (inherited from Google Domains,
nameservers `ns-cloud-c*.googledomains.com`), which has no DNS API. So
validation is delegated:

- Squarespace has two permanent CNAMEs, `_acme-challenge.glasslab.org` and
  `_acme-challenge.www.glasslab.org`, both pointing to an acme-dns subdomain
  at `auth.acme-dns.io` (registered 2026-10-19).
- certbot runs with `--manual --preferred-challenges dns` and
  `/etc/letsencrypt/acme-dns-auth.py` as the auth hook. The hook publishes
  each challenge value through the acme-dns update API. acme-dns keeps the
  two newest TXT values, which covers both names in one certificate.
- The acme-dns credentials are in `/etc/letsencrypt/acmedns.json` (root,
  0600). They are not tracked here. Anyone holding them can pass DNS
  validation for these two names, and so can the acme-dns operator.
- Renewal: `certbot.timer` runs `certbot -q renew` twice a day. The renewal
  config (`/etc/letsencrypt/renewal/glasslab.org.conf`, tracked) replays the
  hook and runs `systemctl reload apache2` after a new certificate.
  `sudo certbot renew --dry-run` passed on 2026-10-19.

If the acme-dns registration is lost, register a new one
(`curl -s -X POST https://auth.acme-dns.io/register` into `acmedns.json`, root
0600), then point both CNAMEs in Squarespace at the new `fulldomain`.

## Changing The Gateway Web Config

1. Back up the live file on the gateway (`sudo cp ... .bak-<stamp>`) or let
   `scripts/restore-gateway-config.sh` do it.
2. Edit, run `sudo apache2ctl configtest`, then `sudo systemctl reload apache2`.
3. From a workstation, run `scripts/snapshot-gateway-config.sh`, review the
   diff, and open a PR into `testing`.

To roll back to a tracked version, check it out and run
`scripts/restore-gateway-config.sh`. It keeps a `.bak-<stamp>` copy, runs
`apache2ctl configtest`, puts the previous file back if the test fails, and
reloads Apache only if it passes. It restores both Apache sites. It does not
run `a2ensite`, touch ufw or `/etc/letsencrypt`, or restore the
`/var/www/dsei102` and `/var/www/html` content, none of which it manages.
