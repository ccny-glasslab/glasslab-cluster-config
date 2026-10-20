#!/usr/bin/env bash
# DRAFT. 20-student load test against the Kubernetes exam hub (glasslab-exam),
# run from node02, where the root-only credentials live. Mike's workload and
# timing targets; nodes are sampled and OOM kills checked separately with kubectl.
# Run before the exam with nobody using the Kubernetes hub.
#   sudo bash ~/exam/labbox/load-test-k8s.sh
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
RELEASE=/opt/exam-release
HUB=${EXAM_K8S_HUB:-http://192.168.1.50:30800}
OUT=/opt/exam-loadtest-k8s
LIB=/opt/exam-loadtest/pylib   # websocket-client copied from the student image by load-test.sh

[[ $EUID == 0 ]] || { echo 'Run with sudo' >&2; exit 1; }
[[ -d $LIB/websocket ]] || { echo "Missing $LIB/websocket: run load-test.sh once first" >&2; exit 1; }
curl -sf -m 5 "$HUB/exam/hub/health" >/dev/null || { echo "Kubernetes hub not reachable at $HUB" >&2; exit 1; }
install -d -m 0700 "$OUT"
python3 -I "$here/../tests/make-load-k8s.py" "$RELEASE" "$HUB" "$OUT"
cd "$RELEASE"
status=0
PYTHONPATH=$LIB python3 -s -P tests/load-k8s.py || status=$?

python3 -I - "$OUT/load-results.json" <<'PY'
import json, sys
r = json.load(open(sys.argv[1]))
users = r.get('users', {})
def worst(key):
    vals = [u[key] for u in users.values() if key in u]
    return f'{max(vals):.2f}s (median {sorted(vals)[len(vals)//2]:.2f}s, n={len(vals)})' if vals else 'n/a'
print('\n=== Kubernetes load test summary ===')
print('status:                 ', r.get('status'), r.get('error', ''))
print('all 20 servers ready:   ', r.get('all_servers_ready_seconds'), 's (target <= 120)')
print('slowest spawn:          ', worst('spawn_seconds_including_queue'))
print('synchronized pass:      ', worst('synchronized_seconds'), '(target <= 60)')
print('staggered pass:         ', worst('staggered_seconds'))
PY
echo "Details: $OUT/load-results.json. Exit status $status."
exit $status
