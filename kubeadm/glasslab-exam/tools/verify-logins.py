"""Verify roster logins only (no signup). Usage: verify-logins.py BASE ROSTER_CSV [REMOVED_USER PASSWORD_FILE]"""
import csv, http.cookiejar, re, sys, urllib.error, urllib.parse, urllib.request
base = sys.argv[1].rstrip('/')
def login(u, p):
    o = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    with o.open(base + '/hub/login', timeout=30) as r: html = r.read().decode()
    x = re.search(r'name="_xsrf" value="([^"]+)"', html).group(1)
    req = urllib.request.Request(base + '/hub/login?next=%2Fexam%2Fhub%2Fhome',
        urllib.parse.urlencode({'_xsrf': x, 'username': u, 'password': p}).encode(), headers={'Referer': base + '/hub/login'})
    try:
        with o.open(req, timeout=60) as r: return '/hub/login' not in r.url
    except urllib.error.HTTPError: return False
rows = [(r[0].strip(), r[1]) for r in csv.reader(open(sys.argv[2])) if r]
bad = [u for u, p in rows if not login(u, p)]
print(f'{len(rows) - len(bad)} of {len(rows)} roster logins OK' + (f'; FAILED: {bad}' if bad else ''))
if len(sys.argv) > 4:
    ok = login(sys.argv[3], open(sys.argv[4]).read().rstrip('\n'))
    print(f'removed account {sys.argv[3]} with its old password: ' + ('STILL LOGS IN (bad)' if ok else 'rejected (good)'))
    bad += [sys.argv[3]] if ok else []
sys.exit(1 if bad else 0)
