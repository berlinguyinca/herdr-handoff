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
SPEAKS_THEN_SCROLLS = ("print('resumed handoff', sys.argv[-1].split()[3], flush=True)\n"
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


def test_an_echo_with_a_prompt_redrawn_spacing_or_quoting_is_not_proof():
    redrawn = COMMAND.replace(" ", "   ").replace("'", '"')
    prompted = f"wohlgemuth@Gerts-MacBook-Pro ~ % {COMMAND}"
    assert id_outside_command(redrawn, ID, COMMAND) is None
    assert id_outside_command(prompted, ID, COMMAND) is None
    assert id_outside_command(f"[main] {prompted} <-", ID, COMMAND) is None


def test_a_wrapped_echo_is_not_proof_however_it_is_read_back():
    # unwrapped it is one line; wrapped, each row alone holds only a piece of the command
    for width in range(20, 70, 7):
        rows = [("% " + COMMAND)[i:i + width] for i in range(0, len(COMMAND) + 2, width)]
        assert id_outside_command("\n".join(rows) + "\n", ID, COMMAND) is None, width


def test_a_line_that_quotes_the_command_and_adds_its_own_text_is_proof():
    shown = f"% {COMMAND}\npi-ready\nresumed handoff {ID} and reading the document\n"
    assert ID.replace("_", "") in id_outside_command(shown, ID, COMMAND)
    quoting = f"pi: given /bin/pi 'Resume HerdR handoff, resumed handoff {ID} now"
    assert ID.replace("_", "") in id_outside_command(quoting, ID, COMMAND)
    assert id_outside_command("nothing here\n", ID, COMMAND) is None


def test_a_bare_quote_of_the_prompt_is_not_enough():
    assert id_outside_command(f"> Resume HerdR handoff {ID}.", ID, COMMAND) is None


def _bordered(text, width, border):
    """`text` wrapped to `width` columns the way a boxed input field shows it: a border on every row."""
    return "\n".join(f"{border} {text[i:i + width]:<{width}} {border}" for i in range(0, len(text), width)) + "\n"


def test_a_bordered_echo_in_a_narrow_pane_is_not_proof_even_with_the_id_alone_on_a_row():
    # boxes, corners and other drawing characters between rows are not part of what was typed
    for border in ("┃", "│", "|", "║"):
        for width in range(40, 52):
            for lead in range(0, 24):                      # slide the wrap so the id lands on a row of its own
                shown = _bordered(" " * lead + COMMAND, width, border)
                assert id_outside_command(shown, ID, COMMAND) is None, (border, width, lead)


def test_an_echo_read_while_it_is_still_being_typed_is_not_proof():
    # herdr types the command into the pane in pieces, so a read can land anywhere inside the echo
    for cut in range(len(COMMAND) + 1):
        for lead in ("% ", "wohlgemuth@Gerts-MacBook-Pro ~ % "):
            assert id_outside_command(lead + COMMAND[:cut], ID, COMMAND) is None, (cut, lead)
    for cut in range(len(COMMAND) + 1):                        # and with the rows wrapped at 47 columns
        rows = [COMMAND[:cut][i:i + 47] for i in range(0, cut, 47)]
        assert id_outside_command("\n".join(rows), ID, COMMAND) is None, cut


def test_a_bare_id_is_not_proof_but_the_ids_own_surroundings_are():
    assert id_outside_command(ID, ID, COMMAND) is None
    assert id_outside_command(f"\n{ID}\n", ID, COMMAND) is None
    assert id_outside_command(f"pi started\nnow working on {ID} for you", ID, COMMAND) is not None


def test_confirm_successor_has_no_default_for_the_typed_command():
    with pytest.raises(TypeError):
        confirm_successor("w1:p1", harness="pi", handoff_id=ID, timeout=0)


# --- against a real herdr server ----------------------------------------------------------------------

@pytest.mark.integration
def test_confirm_successor_accepts_id_seen_in_recent_scrollback(server_with, tmp_path):
    server = server_with(SPEAKS_THEN_SCROLLS)
    pane = _new_pane(server, tmp_path)
    command = _type_launch(server, pane, tmp_path)
    verdict = confirm_successor(pane, harness="pi", handoff_id=ID, timeout=30, typed_command=command)
    assert verdict["confirmed"] is True, verdict
    assert verdict["evidence"]["agent"] == "pi" and ID.replace("_", "") in verdict["evidence"]["matched_line"]
    assert ID not in _read(server, pane, "visible")      # the screen has scrolled past it; the scrollback has not


@pytest.mark.integration
def test_confirm_successor_rejects_id_only_in_the_typed_command_echo(server_with, tmp_path):
    server = server_with(SILENT)
    pane = _new_pane(server, tmp_path)
    command = _type_launch(server, pane, tmp_path)
    verdict = confirm_successor(pane, harness="pi", handoff_id=ID, timeout=6, typed_command=command)
    screen = _read(server, pane, "recent")
    assert verdict["confirmed"] is False, (verdict, screen)       # the screen says what was taken for proof
    assert verdict["evidence"]["agent"] == "pi" and verdict["evidence"]["matched_line"] is None, (verdict, screen)
    assert ID in screen           # the echo really is on the screen, and it is ignored


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


ECHOER = ("import os\n"
          "print('pi-ready', flush=True)\n"
          "print(os.environ['VARIANT'], flush=True)\n"
          "time.sleep(300)\n")


@pytest.mark.integration
@pytest.mark.parametrize("variant", ["spacing", "quoting", "prompt"])
def test_an_echo_redrawn_by_something_else_is_not_proof(server_with, tmp_path, monkeypatch, variant):
    # the "successor" prints a redrawn copy of the very command that started it
    server = server_with(SILENT)
    pane = _new_pane(server, tmp_path)
    command = _type_launch(server, pane, tmp_path)
    redrawn = {"spacing": command.replace(" ", "  "), "quoting": command.replace("'", '"'),
               "prompt": "user@host ~ % " + command}[variant]
    _harness(Path(server["probe"]), f"print('pi-ready', flush=True)\nprint({redrawn!r}, flush=True)\ntime.sleep(300)\n")
    pane2 = _new_pane(server, tmp_path)
    _type_launch(server, pane2, tmp_path)
    verdict = confirm_successor(pane2, harness="pi", handoff_id=ID, timeout=5, typed_command=command)
    assert verdict["confirmed"] is False, verdict


@pytest.mark.integration
def test_a_successor_line_quoting_the_command_with_its_own_text_is_proof(server_with, tmp_path):
    server = server_with(SILENT)
    pane = _new_pane(server, tmp_path)
    command = _type_launch(server, pane, tmp_path)
    _harness(Path(server["probe"]), "print(\"pi: given 'Resume HerdR handoff, resumed handoff\", sys.argv[-1].split()[3], "
                                     "'now', flush=True)\ntime.sleep(300)\n")
    pane2 = _new_pane(server, tmp_path)
    _type_launch(server, pane2, tmp_path)
    verdict = confirm_successor(pane2, harness="pi", handoff_id=ID, timeout=30, typed_command=command)
    assert verdict["confirmed"] is True, verdict
