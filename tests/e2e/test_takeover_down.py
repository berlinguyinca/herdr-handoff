"""End to end: `takeover --direction down` with a source exit, on an isolated herdr server.

The source pane runs a real long-lived, harness-named executable.  When `takeover` types the exit command
into it, it records `herdr pane layout` (the successor must already sit below it), then ends.  Nothing is
stubbed, and the user's own Herdr is never touched: every command talks to the isolated server's socket.
"""
from __future__ import annotations

import json
import os
import subprocess
import time

import pytest

from conftest import pushed_repo
from test_source_exit import use, wait_agent, workspace
from test_takeover import create, panes, run_cli, source_state, takeover_args

pytestmark = pytest.mark.integration

SOURCE = """#!/usr/bin/env python3
import json, os, subprocess, sys
open({pid_file!r}, "w").write(str(os.getpid()))
print("claude-ready", flush=True)
sys.stdin.readline()                              # the exit command typed by takeover
layout = subprocess.run([{herdr!r}, "pane", "layout", "--pane", {pane!r}], capture_output=True, text=True)
open({layout_file!r}, "w").write(layout.stdout)
"""


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_down_takeover_places_the_successor_below_and_ends_the_source(live_herdr_server, tmp_path, monkeypatch):
    env, herdr = live_herdr_server["env"], live_herdr_server["herdr"]
    use(live_herdr_server, monkeypatch)
    repo = pushed_repo(tmp_path)
    source, full = workspace(live_herdr_server, repo)
    pid_file, layout_file = tmp_path / "source.pid", tmp_path / "layout.json"
    script = tmp_path / "bin" / "claude"
    script.parent.mkdir()
    script.write_text(SOURCE.format(pid_file=str(pid_file), herdr=herdr, pane=source, layout_file=str(layout_file)))
    script.chmod(0o755)
    subprocess.run([herdr, "pane", "run", source, str(script)], env=full, check=True, capture_output=True)
    wait_agent(live_herdr_server, source)
    source_pid = int(pid_file.read_text())

    root = tmp_path / "root"
    package = create(root, env=env)
    done = run_cli(takeover_args(root, package["handoff_id"], source, repo, "--direction", "down",
                                 "--source-exit-text", "/exit", "--source-exit-key", "Enter",
                                 "--source-exit-timeout", "20"), env={**env, "HERDR_PANE_ID": source})
    assert done.returncode == 0, done.stdout + done.stderr
    out = json.loads(done.stdout)
    assert out["status"] == "source_closed" and out["source_closed"] is True
    child = out["pane_id"]

    # the child was below the source while the source was still up
    rects = {p["pane_id"]: p["rect"] for p in json.loads(layout_file.read_text())["result"]["layout"]["panes"]}
    assert rects[child]["y"] > rects[source]["y"] and rects[child]["x"] == rects[source]["x"]

    # the source process ended, its pane is closed, the child lives on
    deadline = time.monotonic() + 10
    while alive(source_pid) and time.monotonic() < deadline:
        time.sleep(0.2)
    assert not alive(source_pid)
    assert source not in panes(herdr, full) and child in panes(herdr, full)

    stored = source_state(root, package["handoff_id"])
    assert stored["takeover"]["phase"] == "source_closed" and stored["target"]["pane_id"] == child

    # the successor's own handoff names this one as its predecessor, which links the source record to it
    nxt = run_cli(["--root", str(root), "create", "--goal", "Continue again", "--source-harness", "claude",
                   "--target-harness", "pi", "--predecessor-id", package["handoff_id"],
                   "--source-pane", child], env=env)
    assert nxt.returncode == 0, nxt.stderr
    successor = json.loads(nxt.stdout)["handoff_id"]
    linked = source_state(root, package["handoff_id"])
    assert linked["successor_id"] == successor and linked["target"]["pane_id"] == child
