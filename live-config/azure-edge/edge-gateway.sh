#!/usr/bin/env bash
# Gateway (glasslab.org) side of the HTTPS relay. Run with sudo ON THE GATEWAY.
#
#   edge-gateway.sh VM_PUBKEY   make the gateway's WireGuard key, dial out to
#                               the Azure VM, enable mod_remoteip and the
#                               glasslab-edge site (from this directory).
#
# Does not touch sshd, ufw, the default route, or the existing 80/443 sites.
# Idempotent. Never prints a private key.
set -euo pipefail

WG_IF=wg-glasslab
GW_ADDR=100.64.77.1/30
VM_ADDR=100.64.77.2
VM_ENDPOINT=${VM_ENDPOINT:-20.55.75.236:51820}
HERE=$(cd "$(dirname "$0")" && pwd)

[ "$(id -u)" -eq 0 ] || { echo "Run with sudo." >&2; exit 1; }
[ $# -ge 1 ] || { echo "Usage: $0 VM_WIREGUARD_PUBKEY" >&2; exit 1; }
VM_PUB=$1
[[ $VM_PUB =~ ^[A-Za-z0-9+/]{43}=$ ]] || { echo "Not a WireGuard public key: $VM_PUB" >&2; exit 1; }

if ! dpkg -s wireguard-tools >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -q
  apt-get install -y -q wireguard-tools
fi

install -d -m 700 /etc/wireguard
if [ ! -s /etc/wireguard/edge.key ]; then
  (umask 077; wg genkey > /etc/wireguard/edge.key)
fi
wg pubkey < /etc/wireguard/edge.key > /etc/wireguard/edge.pub
chmod 644 /etc/wireguard/edge.pub

# AllowedIPs is the VM's /32 only, so wg-quick adds one host route and leaves
# the default route (and SSH) alone.
(umask 077; cat > "/etc/wireguard/$WG_IF.conf" <<WG
[Interface]
Address = $GW_ADDR
PostUp = wg set %i private-key /etc/wireguard/edge.key

[Peer]
# Azure edge VM (public 80/443 front for glasslab.org)
PublicKey = $VM_PUB
Endpoint = $VM_ENDPOINT
AllowedIPs = $VM_ADDR/32
PersistentKeepalive = 25
WG
)
systemctl enable "wg-quick@$WG_IF" >/dev/null
systemctl restart "wg-quick@$WG_IF"

# Apache: back up, add the edge site, test, and roll back on failure.
STAMP=$(date +%Y%m%d-%H%M%S)
tar -C /etc -czf "/root/apache2-bak-$STAMP-edge.tgz" apache2
a2enmod -q remoteip
# Serve /robojan/ through the relay only if the direct site already does.
if grep -qE '^[[:space:]]*Include[[:space:]]+/etc/apache2/glasslab/robojan-route.conf' /etc/apache2/sites-available/glasslab-ssl.conf; then
  ROBOJAN='    Include /etc/apache2/glasslab/robojan-route.conf'
else
  ROBOJAN='    # /robojan/ is not enabled on the direct site, so not here either.'
fi
sed "s|^[[:space:]]*#ROBOJAN_INCLUDE#|$ROBOJAN|" "$HERE/glasslab-edge.conf" > /etc/apache2/sites-available/glasslab-edge.conf
chmod 644 /etc/apache2/sites-available/glasslab-edge.conf
a2ensite -q glasslab-edge
if ! apache2ctl configtest; then
  echo "configtest failed; disabling glasslab-edge again." >&2
  a2dissite -q glasslab-edge
  apache2ctl configtest && systemctl reload apache2
  exit 1
fi
systemctl reload apache2

echo "Gateway WireGuard public key: $(cat /etc/wireguard/edge.pub)"
echo "Apache backup: /root/apache2-bak-$STAMP-edge.tgz"
wg show "$WG_IF"
