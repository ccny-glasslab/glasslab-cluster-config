#!/usr/bin/env bash
# Full student session through the public gateway URL, as one roster account
# (default: spare student24). Run as root on node02; the password is read from
# the root-only credentials file and never printed.
#   sudo bash ~/exam/labbox/smoke-public.sh [studentNN]
set -euo pipefail
cd /opt/exam-release
user=${1:-student24}
pw=$(python3 -I -c "import csv,sys; print(next(r['password'] for r in csv.DictReader(open('private/credentials.csv')) if r['username']==sys.argv[1]))" "$user")
python3 -I scripts/student-session.py https://glasslab.org/exam "$user" "$pw"
docker stop "exam-user-$user" >/dev/null 2>&1 || true
