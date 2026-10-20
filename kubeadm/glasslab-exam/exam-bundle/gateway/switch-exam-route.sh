#!/usr/bin/env bash
# Point the gateway's /exam/ route at the Kubernetes hub or back at node02.
#   sudo bash ~/switch-exam-route.sh k8s      # glasslab-exam proxy-public NodePort on node03
#   sudo bash ~/switch-exam-route.sh node02   # fallback: Docker hub on node02
# Backs up the route file, runs apache2ctl configtest, reloads only if it passes
# (restores the backup otherwise), then checks the login page through the gateway.
set -euo pipefail
F=/etc/apache2/glasslab/exam-route.conf
case ${1:-} in
    k8s)    backend=http://192.168.1.50:30800/exam/; label='Kubernetes glasslab-exam proxy-public NodePort on node03 (192.168.1.50:30800)' ;;
    node02) backend=http://192.168.1.11:8000/exam/;  label='node02 Docker hub (192.168.1.11:8000)' ;;
    *) echo "Usage: $0 k8s|node02" >&2; exit 2 ;;
esac
[[ $EUID == 0 ]] || { echo 'Run with sudo' >&2; exit 1; }
curl -sf -m 5 -o /dev/null "${backend}hub/login" || { echo "Backend $backend is not answering; not switching" >&2; exit 1; }
backup=$F.bak-$(date +%Y%m%d-%H%M%S)
cp -a "$F" "$backup"
sed -i -E \
    -e "s#^\# Backend: .*#\# Backend: $label.#" \
    -e "s#http://[0-9.]+:[0-9]+/exam/#$backend#g" "$F"
grep -n -E '^# Backend|ProxyPass' "$F"
if apache2ctl configtest 2>&1 | grep -q 'Syntax OK'; then
    systemctl reload apache2
    echo "Switched /exam/ to $label (backup: $backup)"
else
    cp -a "$backup" "$F"
    echo 'configtest failed: restored the previous route, nothing reloaded' >&2
    exit 1
fi
sleep 2
for u in https://glasslab.org/ https://glasslab.org/dsei102/ https://glasslab.org/exam/hub/login; do
    printf '%-36s %s\n' "$u" "$(curl -s -m 10 --resolve glasslab.org:443:192.168.1.4 -o /dev/null -w '%{http_code}' "$u")"
done
