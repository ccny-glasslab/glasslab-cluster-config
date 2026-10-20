#!/usr/bin/env bash
# Network-isolation probes for the exam JupyterHub on Kubernetes. Starts a pod that
# carries the student labels (so the chart's singleuser NetworkPolicy applies) and
# a control pod without them, and runs the same connection matrix from both.
# Needs one running student server as the "other student" target.
#   bash netpol-probe.sh <running-student-pod-name>
set -euo pipefail
K="sudo -n env KUBECONFIG=/home/glasslab/.kube/config kubectl"
NS=glasslab-exam
target_pod=${1:?running student pod name, e.g. jupyter-student23}
ip() { $K -n "$1" get "$2" -o jsonpath="$3"; }
peer=$(ip $NS pod/$target_pod '{.status.podIP}')
hub=$(ip $NS svc/hub '{.spec.clusterIP}')
public=$(ip $NS svc/proxy-public '{.spec.clusterIP}')
api=$(ip $NS svc/proxy-api '{.spec.clusterIP}')
pg=$(ip glasslab-v2 svc/glasslab-postgres '{.spec.clusterIP}' 2>/dev/null || echo 10.96.0.250)
probe_py='
import socket, sys
role = sys.argv[1]; failed = 0
for spec in sys.argv[2:]:
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
    if role == "student":
        ok = got == expect; failed += not ok
        print(("PASS  " if ok else "FAIL  ") + f"student -> {name} ({host}:{port}): {got}, expected {expect}")
    else:
        print(f"      control -> {name}: {got}")
sys.exit(1 if failed else 0)
'
targets=(
  "hub API|$hub|8081|open"
  "proxy public port (chart allows)|$public|80|open"
  "proxy admin API|$api|8001|blocked"
  "another student server|$peer|8888|blocked"
  "Kubernetes API|10.96.0.1|443|blocked"
  "cluster DNS: hub|hub.glasslab-exam.svc.cluster.local|dns|blocked"
  "external DNS|example.com|dns|blocked"
  "research Postgres|$pg|5432|blocked"
  "node03 ssh|192.168.1.50|22|blocked"
  "node03 kubelet|192.168.1.50|10250|blocked"
  "provisioner ssh|192.168.1.44|22|blocked"
  "glasslab gateway|192.168.1.4|80|blocked"
  "node02 old hub|192.168.1.11|8000|blocked"
  "internet raw IP|1.1.1.1|443|blocked"
  "link-local metadata|169.254.169.254|80|blocked"
)
run_pod() {  # name labels-json
  $K -n $NS delete pod "$1" --ignore-not-found --wait=true >/dev/null
  $K -n $NS run "$1" --restart=Never --image=exam-student:release --image-pull-policy=Never \
    --labels="$2" --overrides='{"spec":{"automountServiceAccountToken":false,"nodeSelector":{"kubernetes.io/hostname":"node05"},"securityContext":{"runAsUser":1000,"runAsGroup":1000,"runAsNonRoot":true},"containers":[{"name":"'"$1"'","image":"exam-student:release","imagePullPolicy":"Never","command":["sleep","600"],"securityContext":{"allowPrivilegeEscalation":false,"capabilities":{"drop":["ALL"]}}}]}}' >/dev/null
  $K -n $NS wait --for=condition=Ready pod/"$1" --timeout=90s >/dev/null
}
run_pod netpol-probe-student "app=jupyterhub,component=singleuser-server,release=exam,hub.jupyter.org/network-access-hub=true"
run_pod netpol-probe-control "purpose=netpol-control"
echo "== control pod (no student labels): should mostly be open/refused"
$K -n $NS exec netpol-probe-control -- python -c "$probe_py" control "${targets[@]}" || true
echo "== student-labelled pod"
status=0
$K -n $NS exec netpol-probe-student -- python -c "$probe_py" student "${targets[@]}" || status=$?
$K -n $NS delete pod netpol-probe-student netpol-probe-control --wait=false >/dev/null
[[ $status == 0 ]] && echo "Network isolation probes passed" || echo "Network isolation probes FAILED"
exit $status
