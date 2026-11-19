"""The pure decision of whether a source harness has exited, with no herdr server: runs in the unit gate."""
from __future__ import annotations

from herdr_handoff_cli import harness_exited

SHELL_INFO = {"shell_pid": 10, "foreground_processes": [{"pid": 10, "name": "zsh", "argv0": "-zsh"}]}


def test_the_exited_decision_refuses_any_child_of_the_shell_and_an_unanswerable_ps():
    assert harness_exited(SHELL_INFO, False, children_of=lambda pid: []) is True
    assert harness_exited(SHELL_INFO, False, children_of=lambda pid: ["claude"]) is False   # suspended or &
    assert harness_exited(SHELL_INFO, False, children_of=lambda pid: None) is False         # ps failed


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
