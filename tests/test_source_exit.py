"""A clean exit of the source harness before its pane is closed, against a real herdr server.

The panes are real `sh` panes: `read` ends when it gets a line (a harness that obeys its exit command),
`sleep` ignores everything typed at it (a harness that does not).  Nothing is stubbed.
"""
from __future__ import annotations

import json
import os
import subprocess
import time

import pytest

from conftest import pushed_repo
from herdr_handoff import MAX_TYPED_COMMAND
from herdr_handoff_cli import exit_source
from test_takeover import create, panes, run_cli, source_state, takeover_args

pytestmark = pytest.mark.integration


def workspace(server, cwd):
    full = {**os.environ, **server["env"]}
    ws = json.loads(subprocess.run([server["herdr"], "workspace", "create", "--cwd", str(cwd)], env=full,
                                   capture_output=True, text=True, check=True).stdout)
    return ws["result"]["root_pane"]["pane_id"], full


def run_in(server, pane, command):
    full = {**os.environ, **server["env"]}
    subprocess.run([server["herdr"], "pane", "run", pane, command], env=full, check=True, capture_output=True)
    time.sleep(1.0)     # let the shell start it


def interrupt(server, pane):
    full = {**os.environ, **server["env"]}
    subprocess.run([server["herdr"], "pane", "send-keys", pane, "C-c"], env=full, check=True, capture_output=True)
    time.sleep(1.0)


def test_exit_source_returns_true_when_process_ends(herdr_server, tmp_path, monkeypatch):
    monkeypatch.setenv("HERDR_SOCKET_PATH", herdr_server["env"]["HERDR_SOCKET_PATH"])
    pane, _ = workspace(herdr_server, tmp_path)
    run_in(herdr_server, pane, "sh -c 'read x'")
    assert exit_source(pane, text="", keys=["Enter"], timeout=15) is True


def test_exit_source_false_on_timeout_and_source_stays(herdr_server, tmp_path, monkeypatch):
    monkeypatch.setenv("HERDR_SOCKET_PATH", herdr_server["env"]["HERDR_SOCKET_PATH"])
    pane, full = workspace(herdr_server, tmp_path)
    run_in(herdr_server, pane, "sleep 300")
    assert exit_source(pane, text="/exit", keys=["Enter"], timeout=2) is False
    assert pane in panes(herdr_server["herdr"], full)


def test_already_exited_source_counts_as_exited(herdr_server, tmp_path, monkeypatch):
    monkeypatch.setenv("HERDR_SOCKET_PATH", herdr_server["env"]["HERDR_SOCKET_PATH"])
    pane, _ = workspace(herdr_server, tmp_path)         # only the pane's own shell is running
    started = time.monotonic()
    assert exit_source(pane, text="/exit", keys=["Enter"], timeout=15) is True
    assert time.monotonic() - started < 5               # nothing was sent, nothing was waited for


def test_exit_source_false_when_herdr_cannot_be_asked(herdr_server, tmp_path, monkeypatch):
    pane, _ = workspace(herdr_server, tmp_path)
    monkeypatch.setenv("HERDR_SOCKET_PATH", str(tmp_path / "no-server.sock"))   # nothing listens there
    assert exit_source(pane, text="/exit", keys=["Enter"], timeout=2) is False


def test_over_long_exit_text_is_refused_before_anything_is_launched(herdr_server, tmp_path):
    env, herdr = herdr_server["env"], herdr_server["herdr"]
    pane, full = workspace(herdr_server, tmp_path)
    root = tmp_path / "root"
    package = create(root, env=env)
    before = sorted(panes(herdr, full))
    done = run_cli(takeover_args(root, package["handoff_id"], pane, tmp_path,
                                 "--source-exit-text", "x" * (MAX_TYPED_COMMAND + 1)),
                   env={**env, "HERDR_PANE_ID": pane})
    assert done.returncode != 0 and "--source-exit-text" in done.stderr
    assert sorted(panes(herdr, full)) == before


def test_takeover_does_not_close_when_exit_fails(live_herdr_server, tmp_path):
    env, herdr = live_herdr_server["env"], live_herdr_server["herdr"]
    repo = pushed_repo(tmp_path)
    source, full = workspace(live_herdr_server, repo)
    run_in(live_herdr_server, source, "sleep 300")
    root = tmp_path / "root"
    package = create(root, env=env)
    args = takeover_args(root, package["handoff_id"], source, repo, "--source-exit-text", "/exit",
                         "--source-exit-key", "Enter", "--source-exit-timeout", "2")
    done = run_cli(args, env={**env, "HERDR_PANE_ID": source})
    assert done.returncode == 1, done.stdout + done.stderr
    out = json.loads(done.stdout)
    assert out["status"] == "source_exit_failed" and out["source_closed"] is False
    assert source in panes(herdr, full) and out["pane_id"] in panes(herdr, full)
    stored = source_state(root, package["handoff_id"])["takeover"]
    assert stored["phase"] == "source_exit_failed" and stored["confirmed_at"] and stored["reason"]
    after_first = sorted(panes(herdr, full))

    # a retry for the same handoff reuses the recorded successor instead of launching another
    again = run_cli(args, env={**env, "HERDR_PANE_ID": source})
    assert json.loads(again.stdout)["status"] == "source_exit_failed" and again.returncode == 1
    assert sorted(panes(herdr, full)) == after_first

    # once the source has really stopped, the same retry finishes the move: closed without sending anything
    interrupt(live_herdr_server, source)
    final = run_cli(args, env={**env, "HERDR_PANE_ID": source})
    assert final.returncode == 0, final.stdout + final.stderr
    assert json.loads(final.stdout)["status"] == "source_closed"
    assert source not in panes(herdr, full) and sorted(panes(herdr, full)) == sorted(
        p for p in after_first if p != source)


def test_takeover_closes_after_the_source_obeys_the_exit_command(live_herdr_server, tmp_path):
    env, herdr = live_herdr_server["env"], live_herdr_server["herdr"]
    repo = pushed_repo(tmp_path)
    source, full = workspace(live_herdr_server, repo)
    run_in(live_herdr_server, source, "sh -c 'read x'")
    root = tmp_path / "root"
    package = create(root, env=env)
    done = run_cli(takeover_args(root, package["handoff_id"], source, repo, "--source-exit-key", "Enter",
                                 "--source-exit-timeout", "15"), env={**env, "HERDR_PANE_ID": source})
    assert done.returncode == 0, done.stdout + done.stderr
    assert json.loads(done.stdout)["status"] == "source_closed" and source not in panes(herdr, full)


def test_a_dirty_source_is_refused_before_any_exit_is_sent(live_herdr_server, tmp_path):
    env, herdr = live_herdr_server["env"], live_herdr_server["herdr"]
    repo = pushed_repo(tmp_path)
    (repo / "README.md").write_text("half-finished edit\n", encoding="utf-8")
    source, full = workspace(live_herdr_server, repo)
    run_in(live_herdr_server, source, "sh -c 'read x'")
    root = tmp_path / "root"
    package = create(root, env=env)
    done = run_cli(takeover_args(root, package["handoff_id"], source, repo, "--source-exit-key", "Enter",
                                 "--source-exit-timeout", "5"), env={**env, "HERDR_PANE_ID": source})
    assert json.loads(done.stdout)["status"] == "source_dirty"
    time.sleep(1.0)
    view = subprocess.run([herdr, "pane", "process-info", "--pane", source], env=full, capture_output=True,
                          text=True).stdout
    assert '"read x"' in view or "read x" in view      # the source's process still waits: no Enter reached it
