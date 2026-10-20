#!/usr/bin/env bash
# Full student session against any hub URL, as one roster account. Run as root
# on node02; the password is read from the root-only credentials file and never
# printed.
#   sudo bash ~/exam/labbox/smoke-url.sh http://192.168.1.50:30800/exam student23
set -euo pipefail
cd /opt/exam-release
url=$1
user=$2
pw=$(python3 -I -c "import csv,sys; print(next(r['password'] for r in csv.DictReader(open('private/credentials.csv')) if r['username']==sys.argv[1]))" "$user")
python3 -I scripts/student-session.py "$url" "$user" "$pw"
