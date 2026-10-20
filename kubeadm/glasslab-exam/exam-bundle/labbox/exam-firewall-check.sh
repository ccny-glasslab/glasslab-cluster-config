#!/usr/bin/env bash
# DRAFT. Verify the exam student-container firewall.
#
#   sudo -E exam-firewall-check.sh          static checks; start-lab.sh runs this
#                                           before the hub starts
#   sudo -E exam-firewall-check.sh --probe  live traffic matrix from a throwaway
#                                           container; run after the hub is up
#
# Exits non-zero if anything fails. PROBE_IMAGE defaults to the student image.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=exam-firewall-rules.sh
. "$here/exam-firewall-rules.sh"

[[ $EUID == 0 ]] || { echo 'Run as root' >&2; exit 1; }
PROBE_IMAGE=${PROBE_IMAGE:-exam-student:release}
NETWORK=${EXAM_NETWORK:-exam-students}
HUB_CONTAINER=${EXAM_HUB_CONTAINER:-exam-hub}
failures=0

pass() { echo "PASS  $*"; }
fail() { echo "FAIL  $*"; failures=$((failures + 1)); }

# check_chain CMD CHAIN PARENT RULE... : CHAIN holds exactly RULEs, in order,
# and PARENT jumps to it first.
check_chain() {
    local cmd=$1 chain=$2 parent=$3 rule expected actual first
    shift 3
    expected=$(for rule in "$@"; do echo "-A $chain $rule"; done)
    actual=$("$cmd" -w -S "$chain" 2>/dev/null | grep '^-A ') || actual=
    if [[ $actual == "$expected" ]]; then
        pass "$cmd $chain has the expected ${#} rules"
    else
        fail "$cmd $chain differs from the expected rules"
        diff <(echo "$expected") <(echo "$actual") | sed 's/^/      /' || true
    fi
    first=$("$cmd" -w -S "$parent" 2>/dev/null | grep '^-A ' | head -1) || first=
    if [[ $first == "-A $parent -j $chain" ]]; then
        pass "$cmd $parent jumps to $chain first"
    else
        fail "$cmd $parent does not jump to $chain first (first rule: ${first:-none})"
    fi
}

static_checks() {
    if require_iptables_backend; then pass 'Docker uses the iptables firewall backend'; else fail 'Docker firewall backend'; fi
    local key
    for key in bridge-nf-call-iptables bridge-nf-call-ip6tables; do
        if [[ $(cat /proc/sys/net/bridge/$key 2>/dev/null) == 1 ]]; then
            pass "net.bridge.$key=1"
        else
            fail "net.bridge.$key is not 1 (br_netfilter not loaded?): peer traffic would bypass the rules"
        fi
    done
    check_chain iptables "$FWD_CHAIN" DOCKER-USER "${FWD4_RULES[@]}"
    check_chain iptables "$IN_CHAIN" INPUT "${IN4_RULES[@]}"
    check_chain ip6tables "$FWD_CHAIN" "$(forward_parent ip6tables)" "${FWD6_RULES[@]}"
    check_chain ip6tables "$IN_CHAIN" INPUT "${IN6_RULES[@]}"
}

# Runs in the probe container. Each target is name|host|port|expect, where
# port "dns" means resolve host. A refused connection counts as reachable:
# the packet got through and something answered.
PROBE_PY='
import socket, sys
failed = 0
for spec in sys.argv[1:]:
    name, host, port, expect = spec.split("|")
    try:
        if port == "dns":
            socket.getaddrinfo(host, 443)
        else:
            socket.create_connection((host, int(port)), timeout=3).close()
        got = "open"
    except ConnectionRefusedError:
        got = "refused"
    except OSError:
        got = "blocked"
    ok = got == expect
    failed += not ok
    print(("PASS  " if ok else "FAIL  ") + f"student -> {name} ({host}:{port}): {got}, expected {expect}")
sys.exit(1 if failed else 0)
'

probe_checks() {
    local info internal bridge subnet hub_ip bridge_gw listener_ip
    info=$(docker network inspect "$NETWORK" \
        -f '{{.Internal}} {{index .Options "com.docker.network.bridge.name"}} {{(index .IPAM.Config 0).Subnet}} {{(index .IPAM.Config 0).Gateway}}' 2>/dev/null) \
        || { fail "network $NETWORK not found: start the hub first"; return; }
    read -r internal bridge subnet bridge_gw <<<"$info"
    if [[ $internal == true && $bridge == "$EXAM_BRIDGE" && $subnet == "$EXAM_STUDENT_SUBNET" ]]; then
        pass "$NETWORK is internal on $EXAM_BRIDGE with subnet $subnet"
    else
        fail "$NETWORK is internal=$internal bridge=$bridge subnet=$subnet; expected true $EXAM_BRIDGE $EXAM_STUDENT_SUBNET"
    fi
    hub_ip=$(docker inspect "$HUB_CONTAINER" -f "{{(index .NetworkSettings.Networks \"$NETWORK\").IPAddress}}" 2>/dev/null) || hub_ip=
    if [[ $hub_ip == "$EXAM_HUB_IP" ]]; then
        pass "$HUB_CONTAINER is at $hub_ip on $NETWORK"
    else
        fail "$HUB_CONTAINER is at '${hub_ip:-not running}' on $NETWORK; expected $EXAM_HUB_IP"
        return
    fi

    # A stand-in "other student": a server on 8888 like a real student server.
    listener=exam-fw-probe-listener-$$
    trap 'docker rm -f "$listener" >/dev/null 2>&1 || true' EXIT
    docker run -d --rm --name "$listener" --network "$NETWORK" \
        --user 1000:1000 --cap-drop ALL --security-opt no-new-privileges:true \
        --memory 256m --pids-limit 64 \
        "$PROBE_IMAGE" python -m http.server 8888 --bind 0.0.0.0 >/dev/null
    listener_ip=$(docker inspect "$listener" -f "{{(index .NetworkSettings.Networks \"$NETWORK\").IPAddress}}")
    sleep 2

    # Allowed path: the hub must still reach student servers.
    if docker exec "$HUB_CONTAINER" python3 -c \
        "import socket; socket.create_connection(('$listener_ip', 8888), timeout=3).close()" 2>/dev/null; then
        pass "hub -> student server ($listener_ip:8888): open"
    else
        fail "hub -> student server ($listener_ip:8888): not reachable; spawns would fail"
    fi

    docker run --rm --network "$NETWORK" \
        --user 1000:1000 --cap-drop ALL --security-opt no-new-privileges:true \
        --memory 256m --pids-limit 64 \
        "$PROBE_IMAGE" python -c "$PROBE_PY" \
        "hub API|$EXAM_HUB_IP|8081|open" \
        "hub public port|$EXAM_HUB_IP|8000|blocked" \
        "another student|$listener_ip|8888|blocked" \
        "bridge gateway (host) ssh|$bridge_gw|22|blocked" \
        "this machine's LAN address|$EXAM_LAN_IP|8000|blocked" \
        "glasslab gateway|$EXAM_GATEWAY_IP|80|blocked" \
        "provisioner|192.168.1.44|22|blocked" \
        "internet raw IP|1.1.1.1|443|blocked" \
        "link-local metadata|169.254.169.254|80|blocked" \
        "external DNS|example.com|dns|blocked" \
        || failures=$((failures + 1))
}

static_checks
[[ ${1:-} == --probe ]] && probe_checks

if ((failures)); then
    echo "Exam firewall check FAILED ($failures)" >&2
    exit 1
fi
echo 'Exam firewall check passed'
