from __future__ import annotations

import json
import os
import subprocess
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory


REPO_ROOT = Path(__file__).resolve().parents[2]


class _ExoHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args: object) -> None:
        pass

    def do_GET(self) -> None:
        if self.path == "/state":
            body = {
                "topology": {
                    "nodes": ["master", "worker"],
                    "connections": {
                        "master": {"worker": [{
                            "sourceRdmaIface": "rdma_en5",
                            "sinkRdmaIface": "rdma_en5",
                        }]}
                    },
                }
            }
        else:
            body = {"data": []}
        encoded = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_POST(self) -> None:
        encoded = json.dumps({
            "choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}]
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


class GlasslabOpenCodeTests(unittest.TestCase):
    def test_launcher_uses_checked_endpoint_for_opencode_provider(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _ExoHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with TemporaryDirectory() as raw:
                root = Path(raw)
                capture = root / "capture.json"
                fake = root / "opencode"
                fake.write_text(
                    "#!/usr/bin/env python3\n"
                    "import json, os, pathlib, sys\n"
                    "config = json.loads(pathlib.Path(os.environ['OPENCODE_CONFIG']).read_text())\n"
                    "pathlib.Path(os.environ['CAPTURE']).write_text(json.dumps({\n"
                    "  'args': sys.argv[1:],\n"
                    "  'base_url': config['provider']['exo']['options']['baseURL'],\n"
                    "}))\n"
                )
                fake.chmod(0o755)
                api = f"http://127.0.0.1:{server.server_port}"
                completed = subprocess.run(
                    [str(REPO_ROOT / "scripts/glasslab-opencode.sh"), "hello"],
                    cwd=REPO_ROOT,
                    env={
                        **os.environ,
                        "GLASSLAB_EXO_API_BASE": api,
                        "OPENCODE_BIN": str(fake),
                        "CAPTURE": str(capture),
                    },
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
                result = json.loads(capture.read_text())
                self.assertEqual(result["base_url"], f"{api}/v1")
                self.assertEqual(result["args"][:2], ["run", "-m"])
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
