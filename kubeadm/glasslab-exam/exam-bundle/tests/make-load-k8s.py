"""Write Kubernetes-hub copies of Mike's load test next to his originals.

Usage: python3 -I make-load-k8s.py RELEASE_DIR HUB_ORIGIN OUT_DIR
  e.g. make-load-k8s.py /opt/exam-release http://192.168.1.50:30800 /opt/exam-loadtest-k8s

Creates scripts/provision-accounts-k8s.py, tests/local-workflow-k8s.py and
tests/load-k8s.py. Same changes as make-load-exam.py (/exam/ base URL, scoped
_xsrf cookie, Origin, results to OUT_DIR, cleanup afterwards, EXAM_LOAD_USERS),
plus: the hub is at HUB_ORIGIN, and the Docker-only parts are removed (host
sampling and `docker inspect` OOM checks). Node resources and OOM kills are
checked separately with kubectl and on the nodes. Every replacement must match
exactly once.
"""
import sys
from pathlib import Path

release, origin, out = Path(sys.argv[1]), sys.argv[2].rstrip('/'), Path(sys.argv[3])


def patch(src, dst, replacements):
    text = (release / src).read_text()
    for old, new in replacements:
        count = text.count(old)
        if count != 1:
            raise SystemExit(f'{src}: expected 1 match for {old!r}, found {count}')
        text = text.replace(old, new)
    (release / dst).write_text(text)
    print(f'wrote {release / dst}')


patch('scripts/provision-accounts-exam.py', 'scripts/provision-accounts-k8s.py', [
    ("BASE = 'http://127.0.0.1:8000'", f"BASE = '{origin}'"),
])

patch('tests/local-workflow.py', 'tests/local-workflow-k8s.py', [
    ("ROOT / 'scripts/provision-accounts.py'", "ROOT / 'scripts/provision-accounts-k8s.py'"),
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

patch('tests/load-local.py', 'tests/load-k8s.py', [
    ("with_name('local-workflow.py')", "with_name('local-workflow-k8s.py')"),
    ("import time\n", "import os\nimport sys\nimport time\n"),
    ("NAMES = [f'student{i:02d}' for i in range(1, 21)]",
     "NAMES = [f'student{i:02d}' for i in range(1, 1 + int(os.environ.get('EXAM_LOAD_USERS', '20')))]"),
    ("    sampler.start()\n", "    # No host sampling on Kubernetes; nodes are sampled separately.\n"),
    ("            ready = threading.Barrier(20)\n            finished = threading.Barrier(20)\n",
     "            ready = threading.Barrier(len(NAMES))\n            finished = threading.Barrier(len(NAMES))\n"),
    ("""        inspections = json.loads(docker('inspect', *['exam-user-' + n for n in NAMES]))
        results['oom_killed_containers'] = [o['Name'] for o in inspections if o['State']['OOMKilled']]
        assert not results['oom_killed_containers']
        host_samples = [s['host'] for s in samples if 'host' in s]
        results['minimum_memory_headroom_fraction'] = min(s['memory_available_fraction'] for s in host_samples)
        results['minimum_disk_headroom_fraction'] = min(s['disk_available_fraction'] for s in host_samples)
        results['swap_used_kib_max'] = max(s['swap_used_kib'] for s in host_samples)
""", "        results['resource_checks'] = 'nodes sampled separately; OOM kills checked with kubectl'\n"),
    ("""        assert results['minimum_memory_headroom_fraction'] >= .20
        assert results['minimum_disk_headroom_fraction'] >= .20
""", ""),
    ("        sampler.join(timeout=35)\n", ""),
    ("(ROOT / 'release/load-results.json')", f"Path('{out}/load-results.json')"),
    ("(ROOT / 'release/load-samples.json').write_text(json.dumps(samples, indent=2) + '\\n')\n",
     f"Path('{out}/load-samples.json').write_text(json.dumps(samples, indent=2) + '\\n')\n" + cleanup),
])
