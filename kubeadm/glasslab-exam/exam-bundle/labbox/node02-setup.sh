#!/usr/bin/env bash
# DRAFT. One-time setup of node02 as the exam host, in stages. Run as root from
# the staged copy in clusteradmin's home:
#
#   sudo bash ~/exam/labbox/node02-setup.sh stage1    # disable kubelet, reboot
#   sudo bash ~/exam/labbox/node02-setup.sh stage2    # Docker, disk, firewall, release, images
#   sudo bash ~/exam/labbox/node02-setup.sh accounts  # credentials, register roster, close signup
#   sudo bash ~/exam/labbox/node02-setup.sh start     # (re)start the hub, then live firewall probes
#
# Students reach the hub at https://glasslab.org/exam/ through the gateway.
#
# Expects ~/exam/release (images tar, source tarball, manifest, checksums) and
# ~/exam/labbox (these drafts). Cordon and drain happen first, from the provisioner.
set -euo pipefail

EXAM_LAN_IP=192.168.1.11
EXAM_ROOT=/srv/exam
EXAM_DISK_IMG=/var/lib/exam-data.img
EXAM_DISK_SIZE=40G
RELEASE_DIR=/opt/exam-release
FW_DIR=/opt/exam-firewall
ENV_DIR=/etc/exam

here=$(cd "$(dirname "$0")" && pwd)
stage_dir=$(dirname "$here")

[[ $EUID == 0 ]] || { echo 'Run with sudo' >&2; exit 1; }
[[ $(hostname) == node02 ]] || { echo 'This script is for node02 only' >&2; exit 1; }

step() { echo; echo "==> $*"; }

stage1() {
    step 'Disabling kubelet (the node must already be cordoned and drained)'
    read -r -p 'Has node02 been cordoned and drained from the provisioner? [yes/N] ' ok
    [[ $ok == yes ]] || { echo 'Stopped.'; exit 1; }
    systemctl disable --now kubelet
    step 'Rebooting so the Calico and kube-proxy containers and rules are gone. Then run stage2.'
    /usr/local/sbin/glasslab-reboot-node
}

stage2() {
    step 'Checking that Kubernetes networking is gone'
    [[ $(systemctl is-enabled kubelet 2>/dev/null) == disabled ]] || { echo 'kubelet is not disabled: run stage1' >&2; exit 1; }
    local leftover
    leftover=$(iptables-save | grep -cE 'cali-|KUBE-' || true)
    [[ $leftover == 0 ]] || { echo "$leftover Calico/kube-proxy iptables lines remain: reboot first" >&2; exit 1; }

    step 'Installing Ubuntu docker.io (keeps the existing containerd)'
    apt-get install -y docker.io
    ensure_compose
    systemctl enable --now docker
    [[ $(docker info --format '{{.FirewallBackend.Driver}}') == iptables ]] || { echo 'Docker is not on the iptables backend' >&2; exit 1; }

    step "Creating the $EXAM_DISK_SIZE exam filesystem at $EXAM_ROOT"
    if ! mountpoint -q "$EXAM_ROOT"; then
        [[ -e $EXAM_DISK_IMG ]] || { fallocate -l "$EXAM_DISK_SIZE" "$EXAM_DISK_IMG"; mkfs.ext4 -q -L examdata "$EXAM_DISK_IMG"; }
        mkdir -p "$EXAM_ROOT"
        grep -q "^$EXAM_DISK_IMG " /etc/fstab || echo "$EXAM_DISK_IMG $EXAM_ROOT ext4 loop,nofail 0 2" >> /etc/fstab
        mount "$EXAM_ROOT"
    fi
    local uuid
    uuid=$(findmnt -n -o UUID --target "$EXAM_ROOT")

    step 'Installing the exam firewall'
    install -d -m 0755 "$FW_DIR" "$ENV_DIR"
    install -m 0755 "$here"/exam-firewall.sh "$here"/exam-firewall-check.sh "$FW_DIR"/
    install -m 0644 "$here"/exam-firewall-rules.sh "$FW_DIR"/
    install -m 0644 "$here"/exam-firewall.service /etc/systemd/system/exam-firewall.service
    printf 'EXAM_LAN_IP=%s\n' "$EXAM_LAN_IP" > "$ENV_DIR/firewall.env"
    chmod 0600 "$ENV_DIR/firewall.env"
    systemctl daemon-reload
    systemctl enable exam-firewall
    systemctl restart exam-firewall   # ExecStartPost runs the static check

    step "Unpacking the release into $RELEASE_DIR"
    (cd "$stage_dir/release" && sha256sum -c exam-images-amd64.tar.sha256 source.tar.gz.sha256)
    install -d -m 0755 "$RELEASE_DIR"
    tar -xzf "$stage_dir/release/source.tar.gz" -C "$RELEASE_DIR"
    (cd "$RELEASE_DIR" && sha256sum --quiet -c release/checksums.sha256)
    install -m 0644 "$stage_dir/release/source.tar.gz" "$RELEASE_DIR/release/source.tar.gz"
    install -m 0644 "$here/compose.lan.yaml" "$RELEASE_DIR/compose.lan.yaml"
    install -m 0755 "$here/start-lab.sh" "$RELEASE_DIR/scripts/start-lab.sh"
    # Mike's account script with every /hub/ path under the /exam/ base URL.
    sed -e "s#'/hub/#'/exam/hub/#g" -e "s#%2Fhub%2Fhome#%2Fexam%2Fhub%2Fhome#" \
        "$RELEASE_DIR/scripts/provision-accounts.py" > "$RELEASE_DIR/scripts/provision-accounts-exam.py"
    install -m 0755 "$here/../tests/student-session.py" "$RELEASE_DIR/scripts/student-session.py"
    cat > "$RELEASE_DIR/.env" <<EOF
HUB_IMAGE=exam-hub:release
STUDENT_IMAGE=exam-student:release
STUDENT_MEM_LIMIT=1G
ALLOW_ACCOUNT_SETUP=0
EXAM_ROOT=$EXAM_ROOT
EXAM_SUBDOMAIN_HOST=
EXAM_LAN_IP=$EXAM_LAN_IP
EOF
    chmod 0600 "$RELEASE_DIR/.env"
    cat > "$ENV_DIR/exam.env" <<EOF
EXAM_ROOT=$EXAM_ROOT
EXAM_LAN_IP=$EXAM_LAN_IP
EXAM_DISK_UUID=$uuid
EXAM_FIREWALL_CHECK=$FW_DIR/exam-firewall-check.sh
EOF
    chmod 0600 "$ENV_DIR/exam.env"

    step 'Loading images and checking them against the release manifest'
    docker image load -i "$stage_dir/release/exam-images-amd64.tar"
    # Docker's containerd image store reports manifest digests, while Mike's
    # manifest records config IDs. The checksummed tarball links the two:
    # its manifest.json maps tag -> config blob, its index.json tag -> manifest.
    python3 -I - "$stage_dir/release/manifest.json" "$stage_dir/release/exam-images-amd64.tar" <<'PY'
import json, subprocess, sys, tarfile
release = json.load(open(sys.argv[1]))
with tarfile.open(sys.argv[2]) as t:
    tar_manifest = json.load(t.extractfile('manifest.json'))
    index = json.load(t.extractfile('index.json'))
config_of = {tag: 'sha256:' + m['Config'].rsplit('/', 1)[-1] for m in tar_manifest for tag in m['RepoTags']}
digest_of = {}
for m in index['manifests']:
    name = m['annotations']['io.containerd.image.name'].removeprefix('docker.io/').removeprefix('library/')
    digest_of[name] = m['digest']
bad = 0
for img in release['images']:
    tag = img['tag']
    got = subprocess.run(['docker', 'image', 'inspect', '-f', '{{.Id}} {{.Os}}/{{.Architecture}}', tag],
                         capture_output=True, text=True).stdout.split()
    ok = (config_of.get(tag) == img['id'] and len(got) == 2 and got[1] == img['platform']
          and got[0] in (img['id'], digest_of.get(tag)))
    bad += not ok
    print(('OK   ' if ok else 'BAD  ') + f"{tag}: {' '.join(got) or 'missing'} (release id {img['id'][:19]})")
sys.exit(1 if bad else 0)
PY

    step 'Provisioning per-student storage'
    (cd "$RELEASE_DIR" && EXAM_ROOT=$EXAM_ROOT bash scripts/provision-storage.sh)

    step "stage2 done. Next: sudo bash $0 accounts"
}

load_env() { set -a; . "$ENV_DIR/exam.env"; set +a; }

# Ubuntu's docker.io ships without the compose plugin, which Mike's scripts use.
ensure_compose() {
    docker compose version >/dev/null 2>&1 || apt-get install -y docker-compose-v2
}

wait_healthy() {
    local i
    for i in $(seq 60); do
        curl -sf -m 2 http://127.0.0.1:8000/exam/hub/health >/dev/null && return 0
        sleep 2
    done
    echo 'Hub did not become healthy: docker logs exam-hub' >&2
    return 1
}

accounts() {
    ensure_compose
    load_env
    cd "$RELEASE_DIR"
    step 'Generating credentials (refuses to overwrite an existing file)'
    [[ -e private/credentials.csv ]] || python3 -I scripts/generate-credentials.py
    step 'Starting the hub with roster signup open'
    ALLOW_ACCOUNT_SETUP=1 bash scripts/start-lab.sh
    wait_healthy
    python3 -I scripts/provision-accounts-exam.py
    step 'Restarting with signup closed'
    ALLOW_ACCOUNT_SETUP=0 bash scripts/start-lab.sh
    wait_healthy
    local code
    code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/exam/hub/signup)
    [[ $code == 404 ]] || { echo "Signup still answers $code; expected 404" >&2; exit 1; }
    echo 'Signup closed (404).'
    step 'Smoke-testing a spare account end to end (student25)'
    smoke student25
    step "Passwords are in $RELEASE_DIR/private/credentials.csv (root-only). Hand them out one row at a time."
    "$FW_DIR/exam-firewall-check.sh" --probe
}

# smoke USER: log in, spawn, run a kernel over WebSocket, save a notebook, via
# the local hub port. Stops the user's server afterwards.
smoke() {
    local pw
    pw=$(python3 -I -c "import csv,sys; print(next(r['password'] for r in csv.DictReader(open('private/credentials.csv')) if r['username']==sys.argv[1]))" "$1")
    python3 -I scripts/student-session.py http://127.0.0.1:8000/exam "$1" "$pw"
    docker stop "exam-user-$1" >/dev/null 2>&1 || true
}

start() {
    ensure_compose
    load_env
    cd "$RELEASE_DIR"
    ALLOW_ACCOUNT_SETUP=0 bash scripts/start-lab.sh
    wait_healthy
    "$FW_DIR/exam-firewall-check.sh" --probe
}

case ${1:-} in
    stage1) stage1 ;;
    stage2) stage2 ;;
    accounts) accounts ;;
    start) start ;;
    *) echo "Usage: sudo bash $0 stage1|stage2|accounts|start" >&2; exit 2 ;;
esac
