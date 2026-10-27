"""Smoke test one student session end to end through any URL prefix.

Usage: python3 -I student-session.py BASE_URL USERNAME PASSWORD
  e.g. http://127.0.0.1:8000/exam  or  https://glasslab.org/exam
Logs in, starts the student's server, opens JupyterLab, starts a kernel, runs
code over the kernel WebSocket, saves a notebook, and deletes the kernel.
Stdlib only. Prints PASS/FAIL lines; exits non-zero on failure.
"""
import base64, http.cookiejar, json, os, re, socket, ssl, sys, time, uuid
import urllib.parse, urllib.request

base, user, password = sys.argv[1].rstrip('/'), sys.argv[2], sys.argv[3]
u = urllib.parse.urlsplit(base)
prefix = u.path
jar = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar),
                                     urllib.request.HTTPSHandler(context=ssl.create_default_context()))

def ok(msg): print('PASS  ' + msg)
def die(msg): print('FAIL  ' + msg); sys.exit(1)

def req(path, data=None, method=None, headers=None):
    r = urllib.request.Request(base + path, data=data, method=method, headers=headers or {})
    return opener.open(r, timeout=60)

def xsrf(path):
    # The hub and each user server set their own _xsrf cookie; use the one
    # scoped to the server being called.
    return next((c.value for c in sorted(jar, key=lambda c: -len(c.path))
                 if c.name == '_xsrf' and path.startswith(c.path.rstrip('/'))), '')

# Log in through the form, as a browser would.
html = req('/hub/login').read().decode()
token = re.search(r'name="_xsrf" value="([^"]+)"', html).group(1)
r = req('/hub/login', urllib.parse.urlencode({'_xsrf': token, 'username': user, 'password': password}).encode(),
        headers={'Referer': base + '/hub/login'})
if '/hub/login' in r.url:
    die('login rejected')
ok(f'login as {user} -> {urllib.parse.urlsplit(r.url).path}')

# Start the server, then open JupyterLab the way a browser does: the page
# load completes the server's OAuth handshake with the hub and sets its cookie.
req('/hub/spawn').read()
user_prefix = f'/user/{user}'
for _ in range(90):
    try:
        lab = req(f'{user_prefix}/lab')
        if lab.status == 200 and 'JupyterLab' in lab.read().decode() and '/lab' in lab.url:
            break
    except urllib.error.HTTPError:
        pass
    time.sleep(2)
else:
    die('JupyterLab did not load')
ok('student server ready; JupyterLab page loads')

hdr = {'X-XSRFToken': xsrf(prefix + user_prefix + '/'), 'Content-Type': 'application/json', 'Referer': base + user_prefix + '/lab'}
kernel = json.load(req(f'{user_prefix}/api/kernels', b'{"name": "python3"}', headers=hdr))
ok(f"kernel started ({kernel['id'][:8]})")

# Kernel WebSocket, by hand (stdlib only).
port = u.port or (443 if u.scheme == 'https' else 80)
raw = socket.create_connection((u.hostname, port), timeout=60)
sock = ssl.create_default_context().wrap_socket(raw, server_hostname=u.hostname) if u.scheme == 'https' else raw
key = base64.b64encode(os.urandom(16)).decode()
cookies = '; '.join(f'{c.name}={c.value}' for c in jar)
path = f"{prefix}{user_prefix}/api/kernels/{kernel['id']}/channels"
origin = f'{u.scheme}://{u.netloc}'
sock.sendall((f'GET {path} HTTP/1.1\r\nHost: {u.netloc}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n'
              f'Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\nOrigin: {origin}\r\n'
              f'Cookie: {cookies}\r\n\r\n').encode())
resp = b''
while b'\r\n\r\n' not in resp:
    resp += sock.recv(1)
if b' 101 ' not in resp.split(b'\r\n')[0]:
    die('kernel WebSocket refused: ' + resp.split(b'\r\n')[0].decode())
ok('kernel WebSocket upgraded')

def recv_exact(n):
    buf = b''
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError('socket closed')
        buf += chunk
    return buf

def send_text(text):
    data, mask = text.encode(), os.urandom(4)
    n = len(data)
    head = bytes([0x81]) + (bytes([0x80 | n]) if n < 126 else bytes([0x80 | 126]) + n.to_bytes(2, 'big') if n < 65536
                            else bytes([0x80 | 127]) + n.to_bytes(8, 'big'))
    sock.sendall(head + mask + bytes(c ^ mask[i % 4] for i, c in enumerate(data)))

def recv_text():
    while True:
        b1, b2 = recv_exact(2)
        n = b2 & 0x7f
        if n == 126: n = int.from_bytes(recv_exact(2), 'big')
        elif n == 127: n = int.from_bytes(recv_exact(8), 'big')
        payload = recv_exact(n)
        if b1 & 0x0f == 1:
            return payload.decode()

code = 'import numpy, pandas, sklearn, matplotlib; print("sum", sum(range(10)), numpy.__version__)'
msg_id = uuid.uuid4().hex
send_text(json.dumps({'header': {'msg_id': msg_id, 'msg_type': 'execute_request', 'username': user,
                                 'session': uuid.uuid4().hex, 'version': '5.3'},
                      'parent_header': {}, 'metadata': {}, 'channel': 'shell',
                      'content': {'code': code, 'silent': False, 'store_history': False,
                                  'user_expressions': {}, 'allow_stdin': False}}))
output, deadline = '', time.time() + 90
while time.time() < deadline:
    m = json.loads(recv_text())
    if m.get('parent_header', {}).get('msg_id') != msg_id:
        continue
    if m['msg_type'] == 'stream':
        output += m['content']['text']
    if m['msg_type'] == 'error':
        die('kernel error: ' + m['content']['ename'])
    if m['msg_type'] == 'execute_reply':
        break
if 'sum 45' not in output:
    die(f'unexpected kernel output: {output!r}')
ok('kernel ran code over WebSocket: ' + output.strip())
sock.close()

nb = {'type': 'notebook', 'content': {'cells': [], 'metadata': {}, 'nbformat': 4, 'nbformat_minor': 5}}
req(f'{user_prefix}/api/contents/smoke-test.ipynb', json.dumps(nb).encode(), method='PUT', headers=hdr).read()
req(f'{user_prefix}/api/contents/smoke-test.ipynb', method='DELETE', headers=hdr).read()
ok('notebook saved and deleted in the student folder')
req(f"{user_prefix}/api/kernels/{kernel['id']}", method='DELETE', headers=hdr).read()
ok('kernel shut down')
