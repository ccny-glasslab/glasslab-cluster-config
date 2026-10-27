#!/bin/bash
# CSCI1910 midterm switch for the glasslab-exam hub (2026-10-27). Run on the provisioner.
set -euo pipefail
cd ~/exam-k8s
K="sudo -n env KUBECONFIG=/home/glasslab/.kube/config kubectl"
H="sudo -n env KUBECONFIG=/home/glasslab/.kube/config $HOME/bin/helm"
NS=glasslab-exam
TAR=csci1910/exam-student-csci1910-amd64.tar
WANT=53177a8b5fad2e29730b6e62413775eea45f8ba7c35dfe845fe7e0eb5448803c
NODES="node01 node03 node04 node05"
step() { echo; echo "=== $(date +%H:%M:%S) $*"; }

step "verify tarball"
(cd csci1910 && sha256sum -c exam-student-csci1910-amd64.tar.sha256)

step "importer pods"
for n in $NODES; do
cat <<EOF | $K apply -f -
apiVersion: v1
kind: Pod
metadata: {name: importer-$n, namespace: $NS}
spec:
  restartPolicy: Never
  hostPID: true
  nodeSelector: {kubernetes.io/hostname: $n}
  automountServiceAccountToken: false
  containers:
  - name: imp
    image: exam-student:release
    imagePullPolicy: Never
    command: [sleep, "3600"]
    securityContext: {privileged: true, runAsUser: 0}
EOF
done
for n in $NODES; do $K -n $NS wait --for=condition=Ready pod/importer-$n --timeout=90s; done

step "import into containerd (parallel)"
for n in $NODES; do
  ( $K -n $NS exec -i importer-$n -- nsenter -t 1 -m -- ctr -n k8s.io images import - < $TAR > csci1910/import-$n.log 2>&1 && echo "$n import done" || echo "$n IMPORT FAILED (see csci1910/import-$n.log)" ) &
done
wait
for n in $NODES; do
  id=$($K -n $NS exec importer-$n -- nsenter -t 1 -m -- crictl inspecti -o go-template --template '{{.status.id}}' docker.io/library/exam-student:csci1910 2>/dev/null || true)
  if [ "$id" = "sha256:$WANT" ]; then echo "$n image OK"; else echo "$n IMAGE MISMATCH: '$id'"; exit 1; fi
done
for n in $NODES; do $K -n $NS delete pod importer-$n --wait=false; done

step "student folders (archive DSEI student01-05, create new)"
users=$(cut -d, -f1 private/csci1910/roster.csv)
for n in node01 node05; do
  dirs=""
  for u in $users; do
    if [ "$n" = node01 ] && [ $((10#${u: -2} % 2)) -eq 1 ]; then dirs="$dirs $u"; fi
    if [ "$n" = node05 ] && [ $((10#${u: -2} % 2)) -eq 0 ]; then dirs="$dirs $u"; fi
  done
  cmd="set -e; A=/host/archive-dsei-20261027; install -d -m 0700 -o 0 -g 0 \$A; for d in student01 student02 student03 student04 student05; do [ -d /host/users/\$d ] && mv /host/users/\$d \$A/ || true; done; for d in $dirs; do install -d -m 0700 -o 1000 -g 1000 /host/users/\$d; done; ls -ln /host/users"
  cat <<EOF | $K apply -f -
apiVersion: v1
kind: Pod
metadata: {name: prep-csci-$n, namespace: $NS}
spec:
  restartPolicy: Never
  nodeSelector: {kubernetes.io/hostname: $n}
  automountServiceAccountToken: false
  securityContext: {runAsUser: 0, runAsGroup: 0}
  containers:
  - name: prep
    image: exam-student:csci1910
    imagePullPolicy: Never
    command: ["sh", "-c", "$cmd"]
    volumeMounts: [{name: host, mountPath: /host}]
  volumes:
  - name: host
    hostPath: {path: /var/lib/glasslab-exam, type: Directory}
EOF
done
for n in node01 node05; do
  $K -n $NS wait --for=jsonpath='{.status.phase}'=Succeeded pod/prep-csci-$n --timeout=90s
  $K -n $NS logs prep-csci-$n | tail -30
  $K -n $NS delete pod prep-csci-$n --wait=false
done

step "stop leftover student servers"
$K -n $NS delete pod -l component=singleuser-server --wait=true || true

step "image keepers -> csci1910"
sed 's/exam-student:release/exam-student:csci1910/' image-keeper.yaml > csci1910/image-keeper.yaml
$K -n $NS delete pod image-keeper-node01 image-keeper-node05 --wait=true
$K apply -f csci1910/image-keeper.yaml

step "helm upgrade with signup open"
$H upgrade exam jupyterhub-4.4.0.tgz -n $NS -f values.yaml --set hub.config.NativeAuthenticator.enable_signup=true
$K -n $NS rollout status deploy/hub --timeout=180s
sleep 5

step "roster sync"
python3 -I roster-sync-admin.py http://192.168.1.50:30800/exam private/csci1910/roster.csv private/csci1910/admin-password || echo "ROSTER SYNC REPORTED PROBLEMS"

step "helm upgrade with signup closed"
$H upgrade exam jupyterhub-4.4.0.tgz -n $NS -f values.yaml
$K -n $NS rollout status deploy/hub --timeout=180s
sleep 5

step "verify logins"
python3 -I verify-logins.py http://192.168.1.50:30800/exam private/csci1910/roster.csv
$K -n $NS get pods -o wide
echo; echo "DONE $(date +%H:%M:%S)"
