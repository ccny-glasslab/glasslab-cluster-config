# Gateway Web Serving

The gateway (`glasslab.org`, hostname `glasslab`) is primarily the public SSH
entry point (see [access-topology.md](access-topology.md)), but it also runs
Apache 2.4 (Ubuntu package) on ports 80 and 443. This page records what that
Apache serves. The tracked config is under
[`live-config/gateway/`](../live-config/gateway/): `000-default.conf` (port 80),
`glasslab-ssl.conf` (port 443), the route fragments under
`/etc/apache2/glasslab/` that those two sites include, and the certbot renewal
files. Firewall rules are in [`live-config/gateway/README.md`](../live-config/gateway/README.md).

Last checked live: 2026-10-19 17:45 EDT. Re-check before describing this as current.

## Port 80

One enabled site, `000-default.conf`, a `*:80` virtual host with
`DocumentRoot /var/www/html`. Apache applies `ProxyPass` rules in the order
they appear and the first match wins, so order matters.

| Path | Behavior | Notes |
| --- | --- | --- |
| `/dsei102/` | Static files from `/var/www/dsei102` | DSEI 10200 offline course reference (~1 GB, ~21k files) for students taking an exam in CCNY computer labs. Added 2026-10-15. `Require ip 134.74.0.0/16 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 127.0.0.1 ::1`: CCNY's public range, the RFC 1918 private ranges, and loopback; everyone else gets 403. The private ranges were added 2026-10-19 because some campus networks reach the gateway without NAT (a laptop on `192.168.4.x` was getting 403). Private source addresses are not routable from the internet, so this is still campus-only. `/dsei102` redirects (301) to `/dsei102/`. `Options None`, `AllowOverride None`, so no directory listings and no `.htaccess`. |
| `/exam`, `/exam/...` | 301 to `https://glasslab.org/exam...` | Added 2026-10-19 17:38 from `/etc/apache2/glasslab/exam-route-http.conf` (`ProxyPass /exam !` plus a `RedirectMatch`). The exam hub is HTTPS-only; see [Exam JupyterHub](#exam-jupyterhub-exam). |
| `/` (everything else) | Static landing page from `/var/www/html`: the animated GLASS Lab title (Lottie) | Since 2026-10-27; see [Landing page](#landing-page). From 2026-10-19 it was an `index.html` with a centered `glasslab_log.gif`. Before that, `/` was reverse-proxied to `http://134.74.126.109/`, which returned a meta-refresh to the CCNY Computer Science site; those `ProxyPass /` lines are now commented out. |
| `/sstfront/` | 404 | Disabled 2026-10-19 (commented out). It was a proxy to Jupyter at `http://134.74.77.69:9999/sstfront/`, always shadowed by the old root proxy, and would have become publicly reachable once that was removed. Re-enable only deliberately. |

If the root proxy is ever re-enabled, the `ProxyPass /dsei102 !` exclusion
must stay **above** `ProxyPass /`. Below it, the root proxy would forward
`/dsei102/` upstream and the static reference would disappear.

A commented-out `ProxyPass / http://192.168.1.251/` remains from the old
internal file server, which no longer answers.

Enabled proxy modules: `proxy`, `proxy_http`. `headers` was enabled
2026-10-19 for the exam route's `RequestHeader` lines. Logs go to
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
- `/exam/` is proxied to the exam JupyterHub; see
  [Exam JupyterHub](#exam-jupyterhub-exam).
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

## Landing Page

`/` on both ports is a static page in `/var/www/html`. Its tracked source is
[`live-config/gateway/var/www/html/`](../live-config/gateway/var/www/html/):

- `index.html`: full-window dark page that plays the GLASS Lab
  (Geometry, Learning, And Statistical Science) title animation in a loop,
  with a Pause/Play button. Visitors whose system asks for reduced motion get
  a still frame of the finished title instead, and can press Play.
- `glasslab.json`: the Lottie animation (1920x1080, 30 fps, 12 s loop). Its
  layers use Lottie expressions, so it needs the full `lottie.min.js` build,
  not the `_light` one.
- `lottie.min.js`: lottie-web 5.12.2, vendored so the page does not depend on
  a CDN being reachable from the lab. Its sha512 matches the cdnjs SRI
  (`sha512-jEnuDt6jfecCjthQAJ+ed0MTVA++5ZKmlUcmDGBv2vUI/REn6FuIdixLNnQT+vKusE2hhTk2is3cFvv5wA+Sgg==`).

Publish with `scripts/deploy-gateway-landing.sh` from a workstation that can
`ssh glasslab-gateway`. It writes only those three files, backs up the ones
it replaces to `/var/backups/glasslab-landing-<stamp>/` on the gateway, and
does not reload Apache. Other content in `/var/www/html` is not tracked here
and is left alone: the per-course exam pages (`*F26mt.html` and their
directories) and the previous `glasslab_log.gif`, which nothing links to any
more. To roll back, copy the backed-up files into `/var/www/html`.

### Off-campus reachability

Checked 2026-10-27 from internet vantage points (check-host.net nodes in
Brazil, Israel, Portugal, Singapore, Canada, Spain and Moldova):

- `http://glasslab.org/` (port 80) returns 200 from everywhere tested. The
  landing page is reachable off campus over plain HTTP.
- `https://glasslab.org/` (port 443) fails: the TCP connection is reset
  before TLS completes, as recorded on 2026-10-19. This is the CCNY border,
  not the gateway (ufw allows 443).
- SSH (22) is open from the internet.

Most browsers try HTTPS first when a user types `glasslab.org` and fall back
to `http://` when that fails, so the page normally still loads, marked "Not
secure". Browsers in HTTPS-only mode, and links written as `https://`, fail
off campus.

Options for HTTPS off campus, in order of preference:

1. **Ask CCNY IT to allow inbound TCP 443 to `134.74.146.39`** at the border,
   the same treatment ports 80 and 22 already get. Nothing else changes: the
   certificate, the vhost and ufw are already in place, and `/dsei102/` and
   `/exam/` keep their campus-only `Require ip` rules. While they are at it,
   ask why Let's Encrypt's HTTP-01 validation to port 80 is reset (see
   above); fixing that is optional since DNS-01 works.
2. **Another port (e.g. 8443)**: not recommended. It needs the same border
   exception from IT, and visitors would have to type the port.
3. **Host the landing page elsewhere** (for example GitHub Pages on
   `www.glasslab.org`, leaving the apex A record, SSH and the campus-only
   routes on the gateway): needs only a DNS change in Squarespace. Use this
   if IT declines.
4. **A tunnel (e.g. Cloudflare Tunnel)**: not recommended. It needs the zone
   moved to Cloudflare, a proxied apex no longer carries SSH, and tunnelled
   requests arrive from loopback, which the `/dsei102/` rule allows, so the
   campus-only routes would have to be reworked first.

## Exam JupyterHub (`/exam/`)

Temporary. A JupyterHub for the DSEI 10200 exam (Wednesday 2026-10-21,
5:00–7:30 PM) runs on `node02` (`192.168.1.11`), which was borrowed from the
Kubernetes cluster for it. The hub's `base_url` is `/exam/`. Added to the
gateway 2026-10-19 17:38.

- **443** (`/etc/apache2/glasslab/exam-route.conf`, included from
  `glasslab-ssl.conf`): `/exam/` is reverse-proxied to
  `http://192.168.1.11:8000/exam/` with `upgrade=websocket` (kernel
  WebSockets, Apache 2.4.47+; the gateway runs 2.4.52) and `timeout=3600` so
  long-lived kernel connections survive the exam. `ProxyPreserveHost On` and
  `RequestHeader set X-Forwarded-Proto https` / `X-Forwarded-Port 443` are
  there because the hub checks the request `Origin` against host and scheme
  for XSRF, and could otherwise reject requests as cross-origin. `/exam` redirects
  (301) to `/exam/`.
- **80** (`/etc/apache2/glasslab/exam-route-http.conf`, included from
  `000-default.conf`): `/exam` and everything under it redirect (301) to
  HTTPS. The hub is never served over plain HTTP, so exam logins never cross
  the network in clear text.
- **Campus-only**: `Require ip 134.74.0.0/16 10.0.0.0/8 172.16.0.0/12
  192.168.0.0/16`, the same ranges as `/dsei102/` minus loopback. The hub holds
  student accounts and exam work and is only meant to be used from the CCNY
  labs during the exam, so it must not be an internet-facing login page. From
  the internet HTTPS is already reset at the CCNY border (see above), but the
  `Require ip` keeps the route closed even if that border behavior changes.
- The fragments still begin with a `# DRAFT.` comment from when they were
  staged; the live files are in use despite that wording.

Checked 2026-10-19 17:45 from a campus address: `https://glasslab.org/exam/`
returns 503 (the gateway route is in place; node02's hub was not started yet),
`http://glasslab.org/exam/` returns 301 to HTTPS, and `/` and `/dsei102/`
still return 200. Re-check before calling the hub reachable.

### Include placement

Each fragment is included from its site **immediately after the `/dsei102/`
`</Directory>`** and before the landing-page / commented-out `ProxyPass /`
lines. `ProxyPass` rules match in the order they appear, first match wins, and
an `Include` is expanded in place. If the root proxy is ever re-enabled below
that point, the `/exam/` rules (and the port-80 `ProxyPass /exam !`) still win
because they come first; an `Include` placed after `ProxyPass /` would be
shadowed and `/exam/` would be forwarded upstream. Any new route fragment
should follow the same rule. The `Include` is a plain `Include`, not
`IncludeOptional`, so a missing fragment makes `apache2ctl configtest` fail.

### Rollback / removal after the exam

On the gateway, either restore the pre-exam sites
(`sites-available/000-default.conf.bak-20261019-173752` and
`glasslab-ssl.conf.bak-20261019-173752`) or delete the two
`Include /etc/apache2/glasslab/exam-route*.conf` lines. Then
`sudo apache2ctl configtest` and `sudo systemctl reload apache2`. The fragment
files can then be removed (or left; nothing includes them), and
`a2dismod headers` is optional since no other route uses it. Re-run
`scripts/snapshot-gateway-config.sh`, delete any fragment you removed from
`live-config/gateway/etc/apache2/glasslab/` (the snapshot only adds files),
and open a PR.

## Changing The Gateway Web Config

1. Back up the live file on the gateway (`sudo cp ... .bak-<stamp>`) or let
   `scripts/restore-gateway-config.sh` do it.
2. Edit, run `sudo apache2ctl configtest`, then `sudo systemctl reload apache2`.
3. From a workstation, run `scripts/snapshot-gateway-config.sh`, review the
   diff, and open a PR into `testing`.

To roll back to a tracked version, check it out and run
`scripts/restore-gateway-config.sh`. It keeps a `.bak-<stamp>` copy, runs
`apache2ctl configtest`, puts the previous file back if the test fails, and
reloads Apache only if it passes. It restores the tracked
`/etc/apache2/glasslab/*.conf` fragments first (the sites include them) and
then both Apache sites. It does not delete a live fragment that is no longer
tracked. It does not run `a2ensite` or `a2enmod`, touch ufw or
`/etc/letsencrypt`, or restore the `/var/www/dsei102` and `/var/www/html` content, none of which it manages.
