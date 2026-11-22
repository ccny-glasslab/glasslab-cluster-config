# Gateway Azure Edge Relay

The CCNY border resets inbound TCP 443 to the gateway (`134.74.146.39`), so
`https://glasslab.org` did not work off campus. An Azure VM (Standard_B2s,
Ubuntu 24.04, `20.55.75.236`, owned by Curtis Rias in the `GLASSLAB_PP`
subscription) takes public 80/443 for `glasslab.org` and `www.glasslab.org`
and relays them to the gateway over a WireGuard tunnel that the gateway dials
out. Files: [`live-config/azure-edge/`](../live-config/azure-edge/).

## Names

| Name | Points at | Serves |
| --- | --- | --- |
| `glasslab.org`, `www.glasslab.org` | the Azure VM | the lab site, through the relay |
| `ssh.glasslab.org` | `134.74.146.39` (gateway) | SSH. Use this as `HostName` for `glasslab-gateway` |
| `exam.glasslab.org` | `134.74.146.39` (gateway) | exam JupyterHub at `/exam/`, campus only, never relayed |

A DNS record covers every port, so once `glasslab.org` points at the VM,
`ssh glasslab.org` reaches the VM's own sshd (and fails with a host-key
warning). SSH users switch to `ssh.glasslab.org` or the IP.

## How a request flows

1. Visitor connects to the VM on 443. HAProxy (TCP mode, no TLS termination)
   checks the SNI is `glasslab.org` or `www.glasslab.org` and drops anything
   else, including `exam.glasslab.org`.
2. HAProxy connects to `100.64.77.1:443` over WireGuard (`wg-glasslab`,
   `100.64.77.0/30`, VM listens on UDP 51820, gateway keepalive 25 s) and
   prepends a PROXY v2 header with the visitor's address.
3. Apache's `glasslab-edge` site, bound to `100.64.77.1` only, requires that
   header (`RemoteIPProxyProtocol On`), terminates TLS with the gateway's own
   Let's Encrypt certificate, and applies `Require ip` to the real client
   address. The `*:80`/`*:443` sites keep serving direct connections.

Port 80 on the relay redirects to HTTPS.

## Allowlists

- The tunnel uses `100.64.77.0/30` on purpose: the gateway's allowlists trust
  RFC 1918 and loopback, so a relay that showed visitors as `10.x` or
  `127.0.0.1` would open campus-only routes to the internet.
- `/dsei102/` through the relay: `Require ip 134.74.0.0/16` on the real
  client address. `/robojan/` follows the direct site (included only if the
  direct site includes it), with its own `Require ip`.
- `/exam` through the relay only redirects to
  `https://exam.glasslab.org/exam/`. The exam hub is never proxied from the
  relay site, because exam passwords are weak. `exam.glasslab.org` resolves to
  the gateway, so off campus it hits the border reset, and its site
  (`exam-glasslab.conf`) also has `Require ip` for campus ranges.
- Whoever has root on the VM could forge PROXY headers and so reach
  `/dsei102/` and `/robojan/` (not the exam hub). They cannot decrypt traffic:
  the certificate and key stay on the gateway.
- SSH is not relayed. `/etc/hosts.allow` and fail2ban are unchanged.

## Certificates

`glasslab.org` + `www` keep their certificate. `exam.glasslab.org` has its own
one-name certificate (`--cert-name exam.glasslab.org`), because acme-dns keeps
only two TXT values per subdomain. Its `_acme-challenge.exam` CNAME points at
the same acme-dns subdomain as the others. DNS-01 does not depend on where A
records point.

## Operations

- Tunnel status: `sudo wg show wg-glasslab latest-handshakes` on either side.
- Relay log on the gateway: `/var/log/apache2/edge-access.log` (real client
  addresses). HAProxy logs to the VM's journal.
- If the VM is down, `glasslab.org` is down off campus and on campus too (DNS
  points at the VM). Campus users can still reach the gateway at
  `https://134.74.146.39` (certificate name mismatch) and the exam hub at
  `exam.glasslab.org`. SSH via `ssh.glasslab.org` is unaffected.
- The VM's public IP is dynamic until Curtis makes it static; it may change if
  the VM is deallocated. Then update the `glasslab.org` A record.

## Rollback

1. Squarespace: `glasslab.org` A record back to `134.74.146.39`.
2. Gateway: `sudo a2dissite glasslab-edge && sudo systemctl reload apache2 && sudo systemctl disable --now wg-quick@wg-glasslab`
3. VM: `sudo systemctl disable --now haproxy wg-quick@wg-glasslab`

`edge-gateway.sh` and `edge-exam.sh` leave `/root/apache2-bak-<stamp>-*.tgz`
on the gateway; `edge-vm.sh` keeps `/etc/haproxy/haproxy.cfg.orig`.
