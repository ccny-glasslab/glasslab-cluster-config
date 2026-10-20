# DRAFT. Shared rule definitions, sourced by exam-firewall.sh and
# exam-firewall-check.sh so the applied rules and the checked rules are the
# same list. Not executable on its own.
#
# Policy for containers on the exam-students bridge (br-exam):
#   student -> hub:8081 (Hub API)        allow
#   hub     -> student:8888 (servers)    allow
#   replies on those connections         allow
#   anything else on, off or into br-exam  drop (peers, host, LAN, internet)
# And the hub's LAN-published port 8000 accepts new connections only from
# the gateway's reverse proxy.

: "${EXAM_LAN_IP:?Set EXAM_LAN_IP to the lab-LAN (192.168.1.x) address of this machine}"
EXAM_GATEWAY_IP=${EXAM_GATEWAY_IP:-192.168.1.4}
EXAM_BRIDGE=${EXAM_BRIDGE:-br-exam}
EXAM_HUB_IP=${EXAM_HUB_IP:-172.30.99.10}
EXAM_STUDENT_SUBNET=${EXAM_STUDENT_SUBNET:-172.30.99.0/24}

FWD_CHAIN=EXAM-FWD
IN_CHAIN=EXAM-IN

_b=$EXAM_BRIDGE
# One rule per element, word-split when applied.
FWD4_RULES=(
    "-i $_b -o $_b -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT"
    "-s $EXAM_HUB_IP/32 -i $_b -o $_b -p tcp -m tcp --dport 8888 -m conntrack --ctstate NEW -j ACCEPT"
    "-d $EXAM_HUB_IP/32 -i $_b -o $_b -p tcp -m tcp --dport 8081 -m conntrack --ctstate NEW -j ACCEPT"
    "-i $_b -o $_b -j DROP"
    "-i $_b -j DROP"
    "-o $_b -j DROP"
    "! -s $EXAM_GATEWAY_IP/32 -p tcp -m conntrack --ctstate NEW --ctorigdst $EXAM_LAN_IP --ctorigdstport 8000 -j DROP"
)
IN4_RULES=(
    "-i $_b -j DROP"
)
# The hub and students talk IPv4 only; drop all IPv6 on the bridge, including
# link-local peer traffic.
FWD6_RULES=(
    "-i $_b -j DROP"
    "-o $_b -j DROP"
)
IN6_RULES=(
    "-i $_b -j DROP"
)
unset _b

# Docker's FORWARD hook for user rules; fall back to FORWARD if Docker has not
# created it for this family (e.g. ip6tables disabled in daemon.json).
forward_parent() {
    if "$1" -w -n -L DOCKER-USER >/dev/null 2>&1; then echo DOCKER-USER; else echo FORWARD; fi
}

# Only Docker's iptables backend is supported; its nftables backend uses
# different hooks and these rules would not apply.
require_iptables_backend() {
    local driver
    driver=$(docker info --format '{{.FirewallBackend.Driver}}' 2>/dev/null) || driver=
    if [[ -n $driver && $driver != iptables ]]; then
        echo "Docker firewall backend is '$driver'; these rules need the iptables backend" >&2
        return 1
    fi
    iptables -w -n -L DOCKER-USER >/dev/null 2>&1 || {
        echo 'DOCKER-USER chain missing: is Docker running with iptables enabled?' >&2
        return 1
    }
}
