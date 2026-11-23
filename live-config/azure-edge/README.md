# Azure Edge Relay

Files for the relay that serves `glasslab.org` to the internet through an
Azure VM, because the CCNY border drops inbound 443 to the gateway. See
[docs/gateway-azure-edge.md](../../docs/gateway-azure-edge.md).

| File | Goes to | Installed by |
| --- | --- | --- |
| `haproxy.cfg` | VM `/etc/haproxy/haproxy.cfg` | `edge-vm.sh` |
| `glasslab-edge.conf` | gateway `/etc/apache2/sites-available/` | `edge-gateway.sh` |
| `exam-glasslab.conf` | gateway `/etc/apache2/sites-available/` | `edge-exam.sh` |
| `glasslab-edge-tunnel.service` | gateway `/etc/systemd/system/` | by hand (see the doc) |
| `edge-vm.sh` | run on the VM with sudo | |
| `edge-gateway.sh` | run on the gateway with sudo | |
| `edge-exam.sh` | run on the gateway with sudo, after the exam DNS records exist | |

Keys (the gateway's tunnel key, WireGuard keys) are generated on each host,
root-only, and never leave it or enter this repo. The live transport is the
reverse SSH tunnel; WireGuard only provides the `100.64.77.1` address.
