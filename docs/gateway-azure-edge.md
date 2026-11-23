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
| `*.glasslab.org` | the Azure VM | future services; each needs a gateway site and a HAProxy SNI entry |
| `exam.glasslab.org` | `134.74.146.39` (gateway) | exam JupyterHub at `/exam/`, campus only, never relayed |

A DNS record covers every port, so once `glasslab.org` points at the VM,
`ssh glasslab.org` reaches the VM's own sshd (and fails with a host-key
warning). SSH users switch to `ssh.glasslab.org` or the IP.

## How a request flows

1. Visitor connects to the VM on 443. HAProxy (TCP mode, no TLS termination)
   reads only the SNI name, accepts `glasslab.org` / `www.glasslab.org`, and
   drops anything else, including `exam.glasslab.org`.
2. HAProxy connects to `127.0.0.1:10443` on the VM and prepends a PROXY v2
   header with the visitor's address. That port is the VM end of a reverse SSH
   tunnel that the gateway holds open over the VM's port 22
   (`glasslab-edge-tunnel.service` on the gateway, user `edgetunnel` on the
   VM). The gateway end of the tunnel is `100.64.77.1:443`.
3. Apache's `glasslab-edge` site, bound to `100.64.77.1` only, requires that
   header (`RemoteIPProxyProtocol On`), terminates TLS with the gateway's own
   Let's Encrypt certificate, and applies `Require ip` to the real client
   address. The `*:80`/`*:443` sites keep serving direct connections.

Port 80 follows the same path (`10080` -> `100.64.77.1:80`) and redirects to
HTTPS.

`100.64.77.1` is the address of the `wg-glasslab` interface, left from the
first design, which used WireGuard (UDP 51820, never opened in Azure). The
interface must stay up because the edge site binds its address; no WireGuard
peer is expected to answer. The VM's `edge-vm.sh` still installs WireGuard;
`wg-quick@wg-glasslab` there is harmless and can be disabled.

### Tunnel pieces

| Where | What |
| --- | --- |
| gateway `/root/.ssh/glasslab_edge_tunnel` | tunnel key, root-only, never copied |
| gateway `/etc/systemd/system/glasslab-edge-tunnel.service` | [`glasslab-edge-tunnel.service`](../live-config/azure-edge/glasslab-edge-tunnel.service), `Restart=always` |
| VM user `edgetunnel` | shell `nologin`; `authorized_keys` = `restrict,port-forwarding,permitlisten="127.0.0.1:10443",permitlisten="127.0.0.1:10080",command="/usr/sbin/nologin" <gateway tunnel pubkey>` |
| VM `/etc/haproxy/haproxy.cfg` | [`haproxy.cfg`](../live-config/azure-edge/haproxy.cfg) |

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

Today the certificate covers `glasslab.org` + `www`. Plan: reissue it as
`glasslab.org` + `*.glasslab.org`, which covers `www`, `exam` and any future
subdomain. Both names validate through the existing
`_acme-challenge.glasslab.org` CNAME (two TXT values, which is what acme-dns
keeps), so no new DNS records are needed. Until then `edge-exam.sh` issues a
separate `exam.glasslab.org` certificate, which needs an
`_acme-challenge.exam` CNAME. DNS-01 does not depend on where A records
point.

## Operations

- Tunnel status: `systemctl status glasslab-edge-tunnel` on the gateway;
  `ss -tlnp | grep 1044` on the VM should show sshd listening on
  `127.0.0.1:10443` and `:10080`.
- End-to-end from the VM: `curl -s -o /dev/null -w '%{http_code}\n' --resolve glasslab.org:443:127.0.0.1 https://glasslab.org/` (expect 200).
- From outside: `curl -sI https://glasslab.org/` once DNS points at the VM.
- Relay log on the gateway: `/var/log/apache2/edge-access.log` (real client
  addresses). HAProxy logs to the VM's journal.
- If the VM is down, `glasslab.org` is down off campus and on campus too (DNS
  points at the VM). Campus users can still reach the gateway at
  `https://134.74.146.39` (certificate name mismatch) and the exam hub at
  `exam.glasslab.org`. SSH via `ssh.glasslab.org` is unaffected.
- The VM's public IP is dynamic until Curtis makes it static; it may change if
  the VM is deallocated. Then update the `glasslab.org` A record.

## If it breaks

| Symptom | Check | Fix |
| --- | --- | --- |
| Off campus times out | Azure "Effective security rules" for TCP 443 on tylervm | Rule on both NIC and subnet NSG |
| VM answers, 503/connection reset | `systemctl status glasslab-edge-tunnel` on the gateway | `sudo systemctl restart glasslab-edge-tunnel`; check `ssh` from gateway to VM port 22 works |
| Tunnel up, still failing | `ip addr show wg-glasslab` on the gateway | `sudo systemctl restart wg-quick@wg-glasslab` (it only provides 100.64.77.1) |
| VM gone or its IP changed | Azure portal | Point `glasslab.org` back at `134.74.146.39` (rollback step 1), then rebuild |

## Rebuilding on a new VM

Any Ubuntu VM with inbound TCP 22/80/443 works. Run `edge-vm.sh` there (as
root, from a copy of `live-config/azure-edge/`), create the `edgetunnel` user
and `authorized_keys` line above with the gateway's
`/root/.ssh/glasslab_edge_tunnel.pub`, then on the gateway change the VM
address in `glasslab-edge-tunnel.service`, `daemon-reload`, delete
`/root/.ssh/known_hosts_edge`, and restart the service. Point DNS at the new IP.

## Access (bus factor)

| Thing | Who can change it |
| --- | --- |
| Azure firewall, public IP, VM power (subscription `GLASSLAB_PP`) | Curtis Rias; Network Contributor requested for Tyler and Mike |
| VM login (`tylerk`, sudo) | Tyler's key; add a second admin key |
| Gateway | lab admins per the identity ledger |
| DNS (Squarespace) | Mike; a second manager requested |

## Rollback

1. Squarespace: `glasslab.org` A record back to `134.74.146.39`.
2. Gateway: `sudo a2dissite glasslab-edge && sudo systemctl reload apache2 && sudo systemctl disable --now glasslab-edge-tunnel wg-quick@wg-glasslab`
3. VM: `sudo systemctl disable --now haproxy wg-quick@wg-glasslab`

`edge-gateway.sh` and `edge-exam.sh` leave `/root/apache2-bak-<stamp>-*.tgz`
on the gateway; `edge-vm.sh` keeps `/etc/haproxy/haproxy.cfg.orig`.
