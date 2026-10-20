"""Write /exam/-aware copies of Mike's load test next to his originals.

Usage: python3 -I make-load-exam.py RELEASE_DIR EXAM_ROOT OUT_DIR

Creates RELEASE_DIR/tests/local-workflow-exam.py and RELEASE_DIR/tests/load-exam.py.
Changes, and nothing else:
- every hub path under the /exam/ base URL, using scripts/provision-accounts-exam.py
- the server-scoped _xsrf cookie is matched against the /exam-prefixed path
- the WebSocket Origin is scheme://host:port
- host memory/disk are sampled on this machine (EXAM_ROOT), not inside the hub
  container (Mike's /exam-storage mount exists only in his desktop setup)
- results go to OUT_DIR instead of overwriting Mike's release/ evidence
- user count from EXAM_LOAD_USERS (default 20)
- afterwards the test notebooks are deleted and the servers stopped
Each replacement must match exactly once, so a changed original fails loudly.
"""
import sys
from pathlib import Path

release, exam_root, out = (Path(a) for a in sys.argv[1:4])
tests = release / 'tests'


def patch(src, dst, replacements):
    text = (tests / src).read_text()
    for old, new in replacements:
        count = text.count(old)
        if count != 1:
            raise SystemExit(f'{src}: expected 1 match for {old!r}, found {count}')
        text = text.replace(old, new)
    (tests / dst).write_text(text)
    print(f'wrote {tests / dst}')


patch('local-workflow.py', 'local-workflow-exam.py', [
    ("ROOT / 'scripts/provision-accounts.py'", "ROOT / 'scripts/provision-accounts-exam.py'"),
    ("BASE = accounts.BASE\n", "BASE = accounts.BASE + '/exam'\nORIGIN = accounts.BASE\n"),
    ("path.startswith(c.path)]", "('/exam' + path).startswith(c.path)]"),
    ("origin=BASE,", "origin=ORIGIN,"),
])

cleanup = '''
        for name in NAMES:
            for nb in ('load-synchronized.ipynb', 'load-staggered.ipynb'):
                try:
                    workflow.request(sessions[name], f'/user/{name}/api/contents/{nb}', 'DELETE', allowed=(204, 404))
                except Exception:
                    pass
            try:
                workflow.stop(sessions[name], name)
            except Exception as error:
                print(f'cleanup: could not stop {name}: {error}', flush=True)
        print('cleanup: test notebooks deleted, servers stopped', flush=True)
'''

patch('load-local.py', 'load-exam.py', [
    ("with_name('local-workflow.py')", "with_name('local-workflow-exam.py')"),
    ("import time\n", "import os\nimport sys\nimport time\n"),
    ("NAMES = [f'student{i:02d}' for i in range(1, 21)]",
     "NAMES = [f'student{i:02d}' for i in range(1, 1 + int(os.environ.get('EXAM_LOAD_USERS', '20')))]"),
    ('s=os.statvfs("/exam-storage")', f's=os.statvfs("{exam_root}")'),
    ("host = json.loads(docker('exec', 'exam-hub', 'python', '-c', code))",
     "host = json.loads(subprocess.check_output([sys.executable, '-c', code], text=True, timeout=30))"),
    ("            ready = threading.Barrier(20)\n            finished = threading.Barrier(20)\n",
     "            ready = threading.Barrier(len(NAMES))\n            finished = threading.Barrier(len(NAMES))\n"),
    ("(ROOT / 'release/load-results.json')", f"Path('{out}/load-results.json')"),
    ("(ROOT / 'release/load-samples.json')", f"Path('{out}/load-samples.json')"),
    ("(ROOT / 'release/load-samples.json').write_text(json.dumps(samples, indent=2) + '\\n')\n".replace(
        "(ROOT / 'release/load-samples.json')", f"Path('{out}/load-samples.json')"),
     f"Path('{out}/load-samples.json').write_text(json.dumps(samples, indent=2) + '\\n')\n" + cleanup),
])
