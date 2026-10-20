#!/usr/bin/env bash
# DRAFT. 20-student load test against the live exam hub on node02: Mike's
# tests/load-local.py, adapted by tests/make-load-exam.py. Run as root BEFORE the
# exam with nobody using the hub. It spawns student01-20, runs a synchronized and
# a staggered pass of Mike's synthetic exam-shaped workload, then deletes its
# notebooks and stops those servers.
#   sudo bash ~/exam/labbox/load-test.sh
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
RELEASE=/opt/exam-release
EXAM_ROOT=/srv/exam
OUT=/opt/exam-loadtest
LIB=$OUT/pylib
NAMES=$(printf 'student%02d ' $(seq 1 20))

[[ $EUID == 0 ]] || { echo 'Run with sudo' >&2; exit 1; }

active=$(docker ps --filter name=exam-user- --format '{{.Names}}')
[[ -z $active ]] || { echo "Student servers are running: $active. Not starting." >&2; exit 1; }
for n in $NAMES; do
    extra=$(find "$EXAM_ROOT/users/$n" -mindepth 1 -maxdepth 1 ! -name .ipynb_checkpoints | head -1)
    [[ -z $extra ]] || { echo "$n already has files ($extra); not running over real work" >&2; exit 1; }
done

# websocket-client (the harness's only non-stdlib dependency) copied from the
# hash-locked student image: same 1.9.2 Mike's harness pins, pure Python.
install -d -m 0700 "$OUT"
if [[ ! -d $LIB/websocket ]]; then
    install -d -m 0700 "$LIB"
    src=$(docker run --rm exam-student:release python -c 'import websocket, os; print(os.path.dirname(websocket.__file__))')
    cid=$(docker create exam-student:release)
    docker cp -q "$cid:$src" "$LIB/websocket"
    docker rm "$cid" >/dev/null
fi

python3 -I "$here/../tests/make-load-exam.py" "$RELEASE" "$EXAM_ROOT" "$OUT"
cd "$RELEASE"
status=0
PYTHONPATH=$LIB python3 -s -P tests/load-exam.py || status=$?

# Remove anything the test left in the student folders.
for n in $NAMES; do
    d=$EXAM_ROOT/users/$n
    rm -f "$d/load-synchronized.ipynb" "$d/load-staggered.ipynb" "$d"/.ipynb_checkpoints/load-*.ipynb
    rmdir "$d/.ipynb_checkpoints" 2>/dev/null || true
done
left=$(docker ps --filter name=exam-user- --format '{{.Names}}')
[[ -z $left ]] || echo "Still running after cleanup: $left"

python3 -I - "$OUT/load-results.json" <<'PY'
import json, sys
r = json.load(open(sys.argv[1]))
users = r.get('users', {})
def worst(key):
    vals = [u[key] for u in users.values() if key in u]
    return f'{max(vals):.2f}s (median {sorted(vals)[len(vals)//2]:.2f}s, n={len(vals)})' if vals else 'n/a'
print('\n=== Load test summary ===')
print('status:                 ', r.get('status'), r.get('error', ''))
print('all 20 servers ready:   ', r.get('all_servers_ready_seconds'), 's (target <= 120)')
print('slowest spawn:          ', worst('spawn_seconds_including_queue'))
print('synchronized pass:      ', worst('synchronized_seconds'), '(target <= 60)')
print('staggered pass:         ', worst('staggered_seconds'))
print('min memory headroom:    ', r.get('minimum_memory_headroom_fraction'), '(target >= 0.20)')
print('min disk headroom:      ', r.get('minimum_disk_headroom_fraction'), '(target >= 0.20)')
print('max swap used (KiB):    ', r.get('swap_used_kib_max'))
print('OOM-killed containers:  ', r.get('oom_killed_containers'))
PY
echo "Details: $OUT/load-results.json and $OUT/load-samples.json. Exit status $status."
exit $status
