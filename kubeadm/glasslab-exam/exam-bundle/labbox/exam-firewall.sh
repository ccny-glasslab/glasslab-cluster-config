#!/usr/bin/env bash
# DRAFT. Apply or remove the exam student-container firewall.
# Usage: sudo -E exam-firewall.sh apply|remove
# Rules are defined in exam-firewall-rules.sh next to this script.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=exam-firewall-rules.sh
. "$here/exam-firewall-rules.sh"

[[ $EUID == 0 ]] || { echo 'Run as root' >&2; exit 1; }

# install_chain CMD CHAIN PARENT RULE... : (re)fill CHAIN and make it the
# first jump in PARENT.
install_chain() {
    local cmd=$1 chain=$2 parent=$3 rule
    shift 3
    "$cmd" -w -N "$chain" 2>/dev/null || true
    "$cmd" -w -F "$chain"
    for rule in "$@"; do
        # shellcheck disable=SC2086
        "$cmd" -w -A "$chain" $rule
    done
    while "$cmd" -w -D "$parent" -j "$chain" 2>/dev/null; do :; done
    "$cmd" -w -I "$parent" 1 -j "$chain"
}

remove_chain() {
    local cmd=$1 chain=$2 parent
    for parent in DOCKER-USER FORWARD INPUT; do
        while "$cmd" -w -D "$parent" -j "$chain" 2>/dev/null; do :; done
    done
    "$cmd" -w -F "$chain" 2>/dev/null || true
    "$cmd" -w -X "$chain" 2>/dev/null || true
}

case ${1:-} in
apply)
    require_iptables_backend
    # Same-bridge (peer) traffic only reaches iptables with br_netfilter on.
    modprobe br_netfilter
    sysctl -q -w net.bridge.bridge-nf-call-iptables=1 net.bridge.bridge-nf-call-ip6tables=1
    install_chain iptables "$FWD_CHAIN" DOCKER-USER "${FWD4_RULES[@]}"
    install_chain iptables "$IN_CHAIN" INPUT "${IN4_RULES[@]}"
    install_chain ip6tables "$FWD_CHAIN" "$(forward_parent ip6tables)" "${FWD6_RULES[@]}"
    install_chain ip6tables "$IN_CHAIN" INPUT "${IN6_RULES[@]}"
    echo "Exam firewall applied for $EXAM_BRIDGE (hub $EXAM_HUB_IP, LAN $EXAM_LAN_IP, gateway $EXAM_GATEWAY_IP)"
    ;;
remove)
    for cmd in iptables ip6tables; do
        remove_chain "$cmd" "$FWD_CHAIN"
        remove_chain "$cmd" "$IN_CHAIN"
    done
    echo 'Exam firewall removed'
    ;;
*)
    echo "Usage: $0 apply|remove" >&2
    exit 2
    ;;
esac
