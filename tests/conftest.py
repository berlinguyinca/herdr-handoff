"""Shared fixtures: a real, isolated, headless herdr server and recorder 'harness' executables.

No mocks: the server is the real ``herdr`` binary on a private socket, panes are real shells,
and the recorder scripts are real executables that receive the real argv the launcher built.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

HARNESSES = ("claude", "pi", "codex", "opencode")


def _recorder(bindir: Path, name: str) -> None:
    path = bindir / name
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "out = os.environ.get('HANDOFF_PROBE_DIR') or os.path.dirname(os.path.abspath(__file__))\n"
        f"open(os.path.join(out, '{name}.argv.json'), 'w').write(json.dumps({{'argv': sys.argv[1:], "
        "'cwd': os.getcwd(), 'handoff_id': os.environ.get('HERDR_HANDOFF_ID'), "
        "'handoff_file': os.environ.get('HERDR_HANDOFF_FILE')}))\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


@pytest.fixture
def recorder_bin(tmp_path: Path) -> Path:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name in HARNESSES:
        _recorder(bindir, name)
    return bindir


@pytest.fixture
def herdr_server(recorder_bin: Path):
    """A real headless herdr server whose panes see the recorder harnesses first on PATH."""
    herdr = shutil.which("herdr")
    if not herdr:
        pytest.skip("herdr binary not installed; real-herdr launch tests need it")
    home = Path(tempfile.mkdtemp(prefix="hd."))  # unix socket paths are length-limited: keep it short
    env = {k: v for k, v in os.environ.items() if not k.startswith("HERDR_")}
    env.update(HOME=str(home), XDG_CONFIG_HOME=str(home / "cfg"), HERDR_SOCKET_PATH=str(home / "t.sock"),
               PATH=f"{recorder_bin}:{os.environ['PATH']}")
    log = open(home / "server.log", "w")
    proc = subprocess.Popen([herdr, "server"], env=env, stdout=log, stderr=log, cwd=str(home))
    deadline = time.time() + 15
    while time.time() < deadline:
        done = subprocess.run([herdr, "workspace", "list"], env=env, capture_output=True, text=True)
        if done.returncode == 0:
            break
        time.sleep(0.2)
    else:
        proc.kill()
        pytest.skip("could not start an isolated herdr server in this environment")
    env["HANDOFF_PROBE_DIR"] = str(recorder_bin)
    yield {"env": env, "home": home, "probe": recorder_bin, "herdr": herdr}
    subprocess.run([herdr, "server", "stop"], env=env, capture_output=True, timeout=10)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
    shutil.rmtree(home, ignore_errors=True)


def wait_for_json(path: Path, timeout: float = 20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.is_file() and path.stat().st_size:
            try:
                return json.loads(path.read_text())
            except ValueError:
                pass
        time.sleep(0.2)
    raise AssertionError(f"{path} was not written within {timeout}s")
