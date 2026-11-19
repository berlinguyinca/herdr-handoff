"""Proof of life: herdr observes the successor, and the handoff id must be on a line the launcher did not type.

Real isolated herdr server, real panes, real executables standing in for the harness.  The shell echoes the
launch command the launcher typed, and that echo carries the handoff id, so the echo alone proves nothing.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from conftest import _serve, _stop
from herdr_handoff import build_launch_plan, short_prompt
from herdr_handoff_cli import confirm_successor, id_outside_command

ID = "handoff_" + "ab12cd34" * 4


def _harness(bindir: Path, body: str) -> None:
    path = bindir / "pi"
    path.write_text("#!/usr/bin/env python3\nimport sys, time\n" + body)
    path.chmod(0o755)


SILENT = "print('pi-ready', flush=True)\ntime.sleep(300)\n"
SPEAKS_THEN_SCROLLS = ("print(sys.argv[-1], flush=True)\n"
                       "for n in range(80):\n    print('filler line', n, flush=True)\n"
                       "time.sleep(300)\n")


@pytest.fixture
def server_with(tmp_path, monkeypatch):
    """Start a real server whose `pi` runs the given body; yield a function that makes a pane and types the command."""
    servers = []

    def start(body):
        bindir = tmp_path / "harness-bin"
        bindir.mkdir(exist_ok=True)
        _harness(bindir, body)
        server = _serve(bindir)
        servers.append(server)
        for key in ("HOME", "XDG_CONFIG_HOME", "HERDR_SOCKET_PATH"):
            monkeypatch.setenv(key, server["env"][key])   # the herdr CLI finds this server through its environment
        return server

    yield start
    for server in servers:
        _stop(server)


def _new_pane(server, cwd):
    done = subprocess.run([server["herdr"], "workspace", "create", "--cwd", str(cwd)], env=server["env"],
                          capture_output=True, text=True, check=True)
    return json.loads(done.stdout)["result"]["root_pane"]["pane_id"]


def _type_launch(server, pane, tmp_path, *, pad=""):
    """Type the exact command the launcher builds into the pane and return it."""
    handoff_file = str(tmp_path / "root" / "outbox" / f"{ID}.json")
    document = str(tmp_path / "root" / "documents" / f"{ID}.md")
    plan = build_launch_plan(None, "pi", ID, handoff_file, prompt=pad + short_prompt(ID, document),
                             harness_binary=str(Path(server["probe"]) / "pi"))
    subprocess.run([server["herdr"], "pane", "run", pane, plan["run_command"]], env=server["env"], check=True,
                   capture_output=True)
    return plan["run_command"]


def _read(server, pane, source, lines=200):
    done = subprocess.run([server["herdr"], "pane", "read", pane, "--source", source, "--lines", str(lines)],
                          env=server["env"], capture_output=True, text=True, check=True)
    return done.stdout


# --- the pure decision --------------------------------------------------------------------------------

COMMAND = f"env HERDR_HANDOFF_ID={ID} HERDR_HANDOFF_FILE=/r/outbox/{ID}.json /bin/pi 'Resume HerdR handoff {ID}.'"


def test_the_echoed_launch_command_is_not_proof():
    assert id_outside_command(f"% {COMMAND}\n", ID, COMMAND) is None


def test_a_launch_command_wrapped_over_rows_is_not_proof_wherever_the_id_breaks():
    for width in range(20, 70, 7):
        rows = [("% " + COMMAND)[i:i + width] for i in range(0, len(COMMAND) + 2, width)]
        assert id_outside_command("\n".join(rows) + "\n", ID, COMMAND) is None, width


def test_the_id_on_any_other_line_is_proof():
    shown = f"% {COMMAND}\npi-ready\nResume HerdR handoff {ID}. First read /x\n"
    assert ID in id_outside_command(shown, ID, COMMAND)
    assert id_outside_command("nothing here\n", ID, COMMAND) is None


# --- against a real herdr server ----------------------------------------------------------------------

@pytest.mark.integration
def test_confirm_successor_accepts_id_seen_in_recent_scrollback(server_with, tmp_path):
    server = server_with(SPEAKS_THEN_SCROLLS)
    pane = _new_pane(server, tmp_path)
    command = _type_launch(server, pane, tmp_path)
    verdict = confirm_successor(pane, harness="pi", handoff_id=ID, timeout=30, typed_command=command)
    assert verdict["confirmed"] is True, verdict
    assert verdict["evidence"]["agent"] == "pi" and ID in verdict["evidence"]["matched_line"]
    assert ID not in _read(server, pane, "visible")      # the screen has scrolled past it; the scrollback has not


@pytest.mark.integration
def test_confirm_successor_rejects_id_only_in_the_typed_command_echo(server_with, tmp_path):
    server = server_with(SILENT)
    pane = _new_pane(server, tmp_path)
    command = _type_launch(server, pane, tmp_path)
    verdict = confirm_successor(pane, harness="pi", handoff_id=ID, timeout=6, typed_command=command)
    assert verdict["confirmed"] is False, verdict
    assert verdict["evidence"]["agent"] == "pi" and verdict["evidence"]["matched_line"] is None
    assert ID in _read(server, pane, "recent")           # the echo really is on the screen, and it is ignored


@pytest.mark.integration
def test_confirm_successor_rejects_a_wrapped_echo_even_when_a_row_break_splits_the_id(server_with, tmp_path):
    server = server_with(SILENT)
    for pad in range(0, 120, 4):      # slide the id across the terminal's row boundary until a row splits it
        pane = _new_pane(server, tmp_path)
        command = _type_launch(server, pane, tmp_path, pad=" " * pad + "x" * pad)
        rows = _read(server, pane, "recent").splitlines()
        if sum(row.count(ID) for row in rows) == command.count(ID):
            continue                                      # this padding left every id whole on its row
        verdict = confirm_successor(pane, harness="pi", handoff_id=ID, timeout=5, typed_command=command)
        assert verdict["confirmed"] is False, (pad, verdict)
        return
    pytest.fail("no padding made the terminal wrap inside the handoff id; widen the search")
