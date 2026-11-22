#!/usr/bin/env bash
# Azure edge VM side of the glasslab.org HTTPS relay. Run with sudo ON THE VM.
#
#   edge-vm.sh                 install packages, make the WireGuard key, print
#                              the VM's public key, and stop.
#   edge-vm.sh GATEWAY_PUBKEY  write the WireGuard config, bring the tunnel up,
#                              install haproxy.cfg (from this directory), start
#                              HAProxy.
#
# Idempotent. Never prints a private key.
set -euo pipefail

WG_IF=wg-glasslab
VM_ADDR=100.64.77.2/30
GW_ADDR=100.64.77.1
WG_PORT=${WG_PORT:-51820}
HERE=$(cd "$(dirname "$0")" && pwd)

[ "$(id -u)" -eq 0 ] || { echo "Run with sudo." >&2; exit 1; }

if ! dpkg -s wireguard-tools haproxy >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -q
  apt-get install -y -q wireguard-tools haproxy
fi

install -d -m 700 /etc/wireguard
if [ ! -s /etc/wireguard/edge.key ]; then
  (umask 077; wg genkey > /etc/wireguard/edge.key)
fi
wg pubkey < /etc/wireguard/edge.key > /etc/wireguard/edge.pub
chmod 644 /etc/wireguard/edge.pub

if [ $# -lt 1 ]; then
  echo "VM WireGuard public key: $(cat /etc/wireguard/edge.pub)"
  echo "Packages and key are ready. Next: the gateway step."
  exit 0
fi

GW_PUB=$1
[[ $GW_PUB =~ ^[A-Za-z0-9+/]{43}=$ ]] || { echo "Not a WireGuard public key: $GW_PUB" >&2; exit 1; }

(umask 077; cat > "/etc/wireguard/$WG_IF.conf" <<WG
[Interface]
Address = $VM_ADDR
ListenPort = $WG_PORT
PostUp = wg set %i private-key /etc/wireguard/edge.key

[Peer]
# glasslab.org gateway. It dials out to us, so no Endpoint here.
PublicKey = $GW_PUB
AllowedIPs = $GW_ADDR/32
WG
)
systemctl enable "wg-quick@$WG_IF" >/dev/null
systemctl restart "wg-quick@$WG_IF"

[ -e /etc/haproxy/haproxy.cfg.orig ] || cp -a /etc/haproxy/haproxy.cfg /etc/haproxy/haproxy.cfg.orig
install -m 644 "$HERE/haproxy.cfg" /etc/haproxy/haproxy.cfg.new
haproxy -c -q -f /etc/haproxy/haproxy.cfg.new
mv /etc/haproxy/haproxy.cfg.new /etc/haproxy/haproxy.cfg
systemctl enable haproxy >/dev/null
systemctl restart haproxy

# Host firewall, only if ufw is already on. The Azure NSG is the real gate.
if ufw status 2>/dev/null | grep -q '^Status: active'; then
  ufw allow 80/tcp
  ufw allow 443/tcp
  ufw allow "$WG_PORT/udp"
fi

echo "--- WireGuard"
wg show "$WG_IF"
echo "--- HAProxy"
systemctl --no-pager --lines=0 status haproxy | head -3
echo "Tunnel check: ping -c2 $GW_ADDR"
ping -c2 -W2 "$GW_ADDR" || echo "No reply yet. Is the gateway side up and is UDP $WG_PORT allowed in the NSG?"
