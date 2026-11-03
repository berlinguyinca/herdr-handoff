"""Runs the REAL herdr-memory CLI (python -m herdr_memory.cli) against a local fake Hindsight HTTP server.

Proves the flags/commands herdr-handoff uses (`status`, `get ID --scope S --kind K`) exist on the real CLI.
The Hindsight server itself is a local fake; nothing here touches a live Hindsight. Skipped when the
herdr-memory source tree is not present (set HERDR_MEMORY_SRC to its ``src`` directory).
"""
import json
import os
import re
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from herdr_handoff import build_handoff, build_memory_context, resolve_memory_refs, resume_prompt

SRC = Path(os.environ.get("HERDR_MEMORY_SRC", "~/IdeaProjects/.worktrees/hm-p01/src")).expanduser()
pytestmark = pytest.mark.skipif(not (SRC / "herdr_memory" / "cli.py").is_file(),
                                reason="herdr-memory source tree not available")
SECRET_TEXT = "Release gate requires two reviewers (PRIVATE-MEMORY-BODY)"


class Fake(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def reply(self, code, payload):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode())

    def handle_any(self, method):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length)) if length else None
        if self.path == "/health":
            return self.reply(200, {"status": "healthy", "version": "9.9.9"})
        match = re.match(r"/v1/default/banks/([^/]+)(/.*)", self.path)
        docs = self.server.docs.setdefault(match.group(1), {})
        rest = match.group(2)
        if method == "POST" and rest == "/memories":
            for item in body["items"]:
                docs[item["document_id"]] = item
            return self.reply(200, {"success": True})
        doc = re.match(r"/documents/(.+)", rest)
        if method == "GET" and doc and doc.group(1) in docs:
            item = docs[doc.group(1)]
            return self.reply(200, {"text": item["content"], "metadata": item["metadata"]})
        self.reply(404, {})

    def do_POST(self):
        self.handle_any("POST")

    def do_GET(self):
        self.handle_any("GET")


@pytest.fixture
def env(tmp_path, monkeypatch):
    server = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    server.docs = {}
    threading.Thread(target=server.serve_forever, daemon=True).start()
    key = tmp_path / "atlas.key"
    key.write_text("atlas-key")
    key.chmod(0o600)
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"url": f"http://127.0.0.1:{server.server_port}",
                                  "cache_dir": str(tmp_path / "cache"), "timeout_seconds": 2,
                                  "projects": {"atlas": {"credentials": {"codex": str(key)}}}}))
    monkeypatch.setenv("HERDR_MEMORY_CONFIG", str(config))
    monkeypatch.setenv("HERDR_HARNESS", "codex")
    monkeypatch.setenv("HERDR_MEMORY_PROJECT", "atlas")
    monkeypatch.setenv("PYTHONPATH", str(SRC))
    monkeypatch.setenv("HERDR_MEMORY_BIN", f"{sys.executable} -m herdr_memory.cli")
    yield server
    server.shutdown()
    server.server_close()


def cli(*args):
    done = subprocess.run([sys.executable, "-m", "herdr_memory.cli", "--project", "atlas", *args],
                          capture_output=True, text=True, timeout=30, check=False)
    return done.returncode, json.loads(done.stdout)


def test_real_cli_roundtrip_with_handoff(env, tmp_path):
    code, retained = cli("retain", SECRET_TEXT, "--evidence", "git:abc:x")
    assert code == 0 and retained["outcome"] == "persisted"
    ref = {"memory_id": retained["memory_id"], "bank_id": retained["bank_id"], "revision": str(retained["revision"])}
    ghost = {**ref, "memory_id": "mem-does-not-exist"}
    denied = {"memory_id": "mem-x", "bank_id": "herdr-global", "revision": "1"}

    block = build_memory_context([ref, ghost, denied])
    assert block["checkpoint"]["state"] == "synced" and block["checkpoint"]["outbox_pending"] == 0
    package = build_handoff(goal="real cli", memory_context=block)
    assert SECRET_TEXT not in json.dumps(package) and "PRIVATE-MEMORY-BODY" not in json.dumps(package)

    resolutions = resolve_memory_refs(package["memory_context"])
    assert [r["status"] for r in resolutions] == ["resolved", "unresolved", "forbidden"]
    prompt = resume_prompt(package, resolutions)
    assert "UNTRUSTED MEMORY REFERENCES" in prompt and "PRIVATE-MEMORY-BODY" not in prompt

    # A newer revision on the server makes the packaged ref stale, not resolved.
    code, corrected = cli("correct", retained["memory_id"], SECRET_TEXT + " v2", "--evidence", "git:abc:y")
    assert code == 0
    assert resolve_memory_refs(package["memory_context"])[0]["status"] == "stale"


def test_real_cli_unreachable_server_marks_checkpoint_pending(env):
    code, retained = cli("retain", "A note", "--evidence", "git:abc:x")
    ref = {"memory_id": retained["memory_id"], "bank_id": retained["bank_id"], "revision": str(retained["revision"])}
    env.shutdown()
    env.server_close()
    block = build_memory_context([ref])
    assert block["checkpoint"]["state"] == "pending"
