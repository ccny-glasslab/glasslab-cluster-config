"""Bring the exam hub's accounts in line with a roster CSV, then verify every login.

Usage: python3 -I roster-sync.py BASE_URL ROSTER_CSV ADMIN_PASSWORD_FILE
  BASE_URL             e.g. http://192.168.1.50:30800/exam
  ROSTER_CSV           username,password per line (no header)
  ADMIN_PASSWORD_FILE  examadmin's password on one line

Signup must be open while this runs. For each roster user it signs up; if the
account already exists it sets the roster password through NativeAuthenticator's
admin change-password page as examadmin. It then authorizes any pending roster
signups and logs in as every user. Prints usernames only, never passwords.
Stdlib only.
"""
import csv
import http.cookiejar
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

base = sys.argv[1].rstrip('/')
with open(sys.argv[2], newline='') as f:
    roster = [(r[0].strip(), r[1]) for r in csv.reader(f) if r and r[0].strip()]
admin_password = open(sys.argv[3]).read().rstrip('\n')


def session():
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    return opener


def get(opener, path):
    with opener.open(base + path, timeout=30) as r:
        return r.url, r.read().decode()


def form(opener, path, values):
    _, html = get(opener, path)
    m = re.search(r'name="_xsrf" value="([^"]+)"', html)
    if not m:
        raise RuntimeError(f'no XSRF form at {path}')
    data = urllib.parse.urlencode({'_xsrf': m.group(1), **values}).encode()
    req = urllib.request.Request(base + path, data, headers={'Referer': base + path})
    try:
        with opener.open(req, timeout=60) as r:
            return r.url, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.url, e.read().decode()


def login(username, password):
    opener = session()
    url, _ = form(opener, '/hub/login?next=%2Fexam%2Fhub%2Fhome', {'username': username, 'password': password})
    return opener if '/hub/login' not in url else None


admin = login("michael.grossberg77", admin_password)
if not admin:
    raise SystemExit('admin login failed')

failures = 0
for username, password in roster:
    _, html = form(session(), '/hub/signup', {'username': username, 'signup_password': password,
                                              'signup_password_confirmation': password})
    if 'already in use' in html:
        _, page = form(admin, f'/hub/change-password/{urllib.parse.quote(username)}',
                       {'new_password': password, 'new_password_confirmation': password})
        print(f'existing account, password set by admin: {username}')
    elif 'alert-success' in html or 'alert-info' in html:
        print(f'registered: {username}')
    else:
        failures += 1
        print(f'SIGNUP PROBLEM: {username}')

_, page = get(admin, '/hub/authorize')
pending = re.findall(r'href="([^"]+/authorize/[^"]+)"[^>]*>Authorize</a>', page)
names = {u for u, _ in roster}
for path in pending:
    name = urllib.parse.unquote(path.rsplit('/', 1)[-1])
    if name in names:
        get(admin, path[len('/exam'):] if path.startswith('/exam/') else path)
        print(f'authorized: {name}')

for username, password in roster:
    if login(username, password):
        print(f'LOGIN OK    {username}')
    else:
        failures += 1
        print(f'LOGIN FAIL  {username}')
print(f'all {len(roster)} roster logins verified' if not failures else f'{failures} problem(s)')
sys.exit(1 if failures else 0)
