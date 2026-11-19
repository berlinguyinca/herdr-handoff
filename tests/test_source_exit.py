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
from herdr_handoff_cli import exit_source, harness_exited
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


OBEY = (
    "#!/usr/bin/env python3\n"
    "import os, sys\n"
    "print('claude-ready', flush=True)\n"
    "sys.stdin.readline()\n"
    "if len(sys.argv) > 1:\n"
    "    os.execvp(sys.argv[1], sys.argv[1:])     # leaves a long-lived foreground program behind\n"
)


@pytest.fixture
def obedient(tmp_path):
    """A real executable named like a harness that exits (or turns into `argv`) once it reads a line."""
    path = tmp_path / "obey" / "claude"
    path.parent.mkdir()
    path.write_text(OBEY)
    path.chmod(0o755)
    return str(path)


def use(server, monkeypatch):
    monkeypatch.setenv("HERDR_SOCKET_PATH", server["env"]["HERDR_SOCKET_PATH"])


def wait_agent(server, pane, present=True, timeout=15.0):
    full = {**os.environ, **server["env"]}
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        got = subprocess.run([server["herdr"], "agent", "get", pane], env=full, capture_output=True, text=True)
        if (got.returncode == 0) == present:
            return
        time.sleep(0.3)
    raise AssertionError("herdr never reported the agent")


def read_pane(server, pane):
    full = {**os.environ, **server["env"]}
    return subprocess.run([server["herdr"], "pane", "read", pane, "--source", "recent"], env=full,
                          capture_output=True, text=True).stdout


def test_exit_source_returns_true_when_the_agent_ends(herdr_server, tmp_path, monkeypatch, obedient):
    use(herdr_server, monkeypatch)
    pane, _ = workspace(herdr_server, tmp_path)
    run_in(herdr_server, pane, obedient)
    wait_agent(herdr_server, pane)
    assert exit_source(pane, text="", keys=["Enter"], timeout=15) is True


def test_exit_source_false_on_timeout_and_source_stays(live_herdr_server, tmp_path, monkeypatch):
    use(live_herdr_server, monkeypatch)
    pane, full = workspace(live_herdr_server, tmp_path)
    run_in(live_herdr_server, pane, "claude")          # the recorder harness sleeps and ignores input
    wait_agent(live_herdr_server, pane)
    assert exit_source(pane, text="/exit", keys=["Enter"], timeout=2) is False
    assert pane in panes(live_herdr_server["herdr"], full)


def test_already_exited_source_counts_as_exited(herdr_server, tmp_path, monkeypatch):
    use(herdr_server, monkeypatch)
    pane, _ = workspace(herdr_server, tmp_path)         # only the pane's own shell is running
    started = time.monotonic()
    assert exit_source(pane, text="/exit", keys=["Enter"], timeout=15) is True
    assert time.monotonic() - started < 5               # nothing was sent, nothing was waited for


def test_a_harness_that_is_the_panes_own_process_is_not_mistaken_for_a_shell(live_herdr_server, tmp_path,
                                                                            monkeypatch):
    use(live_herdr_server, monkeypatch)
    pane, full = workspace(live_herdr_server, tmp_path)
    run_in(live_herdr_server, pane, "exec claude")      # pid == shell_pid: nothing but the harness is there
    wait_agent(live_herdr_server, pane)
    assert exit_source(pane, text="/exit", keys=["Enter"], timeout=2) is False
    assert pane in panes(live_herdr_server["herdr"], full)


def test_an_exec_harness_that_obeys_ends_the_pane_and_the_exit_counts(herdr_server, tmp_path, monkeypatch,
                                                                     obedient):
    use(herdr_server, monkeypatch)
    pane, _ = workspace(herdr_server, tmp_path)
    run_in(herdr_server, pane, f"exec {obedient}")
    wait_agent(herdr_server, pane)
    assert exit_source(pane, text="", keys=["Enter"], timeout=15) is True


@pytest.mark.parametrize("program", ["sleep 30", "cat"])
def test_nothing_is_typed_into_a_foreground_program_that_is_not_an_agent(herdr_server, tmp_path, monkeypatch,
                                                                        program):
    use(herdr_server, monkeypatch)
    pane, full = workspace(herdr_server, tmp_path)
    run_in(herdr_server, pane, program)
    assert exit_source(pane, text="TYPED-MARKER-42", keys=["Enter"], timeout=2) is False
    assert "TYPED-MARKER-42" not in read_pane(herdr_server, pane)
    assert pane in panes(herdr_server["herdr"], full)


def test_a_foreground_program_left_behind_by_the_harness_is_not_exited_until_it_ends(
        herdr_server, tmp_path, monkeypatch, obedient):
    use(herdr_server, monkeypatch)
    pane, full = workspace(herdr_server, tmp_path)
    run_in(herdr_server, pane, f"{obedient} sleep 8")
    wait_agent(herdr_server, pane)
    assert exit_source(pane, text="", keys=["Enter"], timeout=2) is False     # the harness became `sleep 8`
    assert pane in panes(herdr_server["herdr"], full)


def test_exit_source_without_a_pane_is_false(herdr_server, monkeypatch):
    use(herdr_server, monkeypatch)
    assert exit_source(None, text="/exit", keys=["Enter"], timeout=1) is False
    assert exit_source("", text="/exit", keys=["Enter"], timeout=1) is False


SHELL_INFO = {"shell_pid": 10, "foreground_processes": [{"pid": 10, "name": "zsh", "argv0": "-zsh"}]}


def test_the_exited_decision_needs_every_condition():
    assert harness_exited(SHELL_INFO, False) is True
    assert harness_exited(SHELL_INFO, True) is False                     # herdr still reports an agent
    assert harness_exited(SHELL_INFO, None) is False                     # unknown agent state
    assert harness_exited(None, False) is False and harness_exited({}, False) is False
    assert harness_exited({"foreground_processes": SHELL_INFO["foreground_processes"]}, False) is False
    assert harness_exited({"shell_pid": 10}, False) is False             # no foreground list
    assert harness_exited({"shell_pid": 10, "foreground_processes": []}, False) is False
    assert harness_exited({"shell_pid": 10, "foreground_processes": None}, False) is False
    assert harness_exited({"shell_pid": True, "foreground_processes": [{"pid": True, "name": "zsh"}]},
                          False) is False
    other = {"shell_pid": 10, "foreground_processes": [{"pid": 10, "name": "zsh"}, {"pid": 11, "name": "vim"}]}
    assert harness_exited(other, False) is False                         # something besides the shell
    assert harness_exited({"shell_pid": 10, "foreground_processes": [{"pid": 11, "name": "zsh"}]},
                          False) is False                                # the shell is not in the foreground


def test_the_exited_decision_only_trusts_a_known_shell_by_name():
    def one(name, **extra):
        return {"shell_pid": 10, "foreground_processes": [{"pid": 10, "name": name, **extra}]}
    for shell in ("sh", "bash", "zsh", "fish", "dash", "ksh", "tcsh", "csh", "nu", "pwsh", "-zsh",
                  "/bin/bash"):
        assert harness_exited(one(shell), False) is True, shell
    for other in ("claude.exe", "node", "python3", "vim", ""):
        assert harness_exited(one(other), False, comm_of=lambda pid: "claude") is False, other
    # a process-info without a name asks `ps`; an unanswerable ps is not a shell
    nameless = {"shell_pid": 10, "foreground_processes": [{"pid": 10}]}
    assert harness_exited(nameless, False, comm_of=lambda pid: "-bash") is True
    assert harness_exited(nameless, False, comm_of=lambda pid: "codex") is False
    assert harness_exited(nameless, False, comm_of=lambda pid: "") is False


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
    seen = stored["evidence"]["source_exit"]            # what was observed, like the other refusals
    assert seen["agent"] is False and "sleep" in seen["foreground"]
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


def test_takeover_closes_after_the_source_obeys_the_exit_command(live_herdr_server, tmp_path, obedient):
    env, herdr = live_herdr_server["env"], live_herdr_server["herdr"]
    repo = pushed_repo(tmp_path)
    source, full = workspace(live_herdr_server, repo)
    run_in(live_herdr_server, source, obedient)
    wait_agent(live_herdr_server, source)
    root = tmp_path / "root"
    package = create(root, env=env)
    done = run_cli(takeover_args(root, package["handoff_id"], source, repo, "--source-exit-key", "Enter",
                                 "--source-exit-timeout", "15"), env={**env, "HERDR_PANE_ID": source})
    assert done.returncode == 0, done.stdout + done.stderr
    assert json.loads(done.stdout)["status"] == "source_closed" and source not in panes(herdr, full)


def test_takeover_keeps_both_panes_for_an_exec_harness_that_ignores_the_exit(live_herdr_server, tmp_path):
    env, herdr = live_herdr_server["env"], live_herdr_server["herdr"]
    repo = pushed_repo(tmp_path)
    source, full = workspace(live_herdr_server, repo)
    run_in(live_herdr_server, source, "exec claude")     # the harness IS the pane's process
    wait_agent(live_herdr_server, source)
    root = tmp_path / "root"
    package = create(root, env=env)
    done = run_cli(takeover_args(root, package["handoff_id"], source, repo, "--source-exit-text", "/exit",
                                 "--source-exit-key", "Enter", "--source-exit-timeout", "2"),
                   env={**env, "HERDR_PANE_ID": source})
    assert done.returncode == 1, done.stdout + done.stderr
    out = json.loads(done.stdout)
    assert out["status"] == "source_exit_failed"
    assert source in panes(herdr, full) and out["pane_id"] in panes(herdr, full)
    assert source_state(root, package["handoff_id"])["takeover"]["evidence"]["source_exit"]["agent"] is True


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
