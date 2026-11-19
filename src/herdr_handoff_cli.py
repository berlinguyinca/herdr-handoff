#!/usr/bin/env python3
"""Small JSON CLI used by all four harness skills."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

from herdr_handoff import (DIRECTIONS, MAX_TYPED_COMMAND, HandoffStore, TakeoverInProgress, __version__,
                            build_handoff, build_launch_plan, build_memory_context, compact_prompt,
                            decide_takeover_close, resolve_memory_refs, resume_prompt, running_subagents,
                            shared_store_root, short_prompt, source_worktree_state, takeover_prompt,
                            unpushed_commits, worktree_close_refusal, validate_takeover, _handoff_root)


def _run(argv, *, capture=True):
    """Run herdr quietly: its own JSON replies must not mix into our single JSON result on stdout."""
    return subprocess.run(argv, check=True, text=True, capture_output=capture)


_PANE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9:_.-]{0,39}")
_LIST_FIELDS = ("completed", "remaining", "decisions", "risks", "validation", "constraints",
                "next_commands", "files", "pull_requests", "unverified")
_JSON_KEYS = set(_LIST_FIELDS) | {"resume_point", "workspace", "report"}


def _pane_of(response):
    result = response.get("result") or {}
    pane = (result.get("pane") or result.get("root_pane") or {}).get("pane_id")
    if not isinstance(pane, str) or not _PANE_ID.fullmatch(pane):
        raise RuntimeError("herdr did not return a pane")
    return pane, result


def _herdr_json(argv):
    return json.loads(_run(argv, capture=True).stdout)


def _place(plan, parent_pane, cwd, label):
    """Create the pane that will host the target harness: split, else a tab, else a workspace."""
    if parent_pane and plan["split"]:
        try:
            pane, result = _pane_of(_herdr_json(plan["split"]))
            return pane, result, "split"
        except (subprocess.CalledProcessError, RuntimeError, ValueError):
            pass  # a stale or foreign pane id must not block the handoff: fall through to a tab
    workspaces = _herdr_json(["herdr", "workspace", "list"]).get("result", {}).get("workspaces") or []
    ids = [w.get("workspace_id") for w in workspaces if isinstance(w.get("workspace_id"), str)]
    wanted = os.environ.get("HERDR_WORKSPACE_ID")
    focused = next((w["workspace_id"] for w in workspaces if w.get("focused")), None)
    workspace = wanted if wanted in ids else (focused or (ids[0] if ids else None))
    if workspace:
        pane, result = _pane_of(_herdr_json(["herdr", "tab", "create", "--workspace", workspace, "--cwd", cwd,
                                             "--label", label, "--focus"]))
        return pane, result, "tab"
    pane, result = _pane_of(_herdr_json(["herdr", "workspace", "create", "--cwd", cwd, "--label", label,
                                         "--focus"]))
    return pane, result, "workspace"


def _announce_pane(pane, *, label, harness, handoff_id):
    """Label the pane and register the handoff as its agent session, so herdr can show and detect it."""
    _run(["herdr", "pane", "rename", pane, label])
    _run(["herdr", "pane", "report-metadata", pane, "--source", "herdr-handoff",
          "--title", f"handoff -> {harness}", "--token", f"handoff_id={handoff_id}"])
    _run(["herdr", "pane", "report-agent-session", pane, "--source", "herdr-handoff",
          "--agent", harness, "--agent-session-id", handoff_id, "--session-start-source", "herdr-handoff"])


def _default_root():
    """The outbox root and whether it is the shared store, for a process given no `--root`.

    Precedence: HERDR_HANDOFF_FILE (a successor pane is handed `herdr-handoff confirm <id>` with no path
    in it - the typed command has a byte budget, herdr_handoff.MAX_TYPED_COMMAND - so the root comes from
    the file the launch command exports), HERDR_HANDOFF_DIR, then the shared store (HERDR_HANDOFF_STORE,
    else `~/.local/share/herdr-handoff/store`).  Only the shared store gets per-repo records imported.
    """
    derived = _handoff_root(os.environ.get("HERDR_HANDOFF_FILE", ""))
    if derived:
        return derived, False
    if os.environ.get("HERDR_HANDOFF_DIR"):
        return os.environ["HERDR_HANDOFF_DIR"], False
    return shared_store_root(os.environ), True


def _legacy_root(cwd=None):
    """The per-repo store older versions wrote: `<git toplevel, else cwd>/.herdr/handoffs`."""
    cwd = cwd or os.getcwd()
    top = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=cwd, text=True, capture_output=True)
    base = top.stdout.strip() if top.returncode == 0 and top.stdout.strip() else cwd
    return os.path.join(base, ".herdr", "handoffs")


def _first_time(store, key):
    """True the first time `key` is seen for this store; a marker file in the store remembers it.

    A store that cannot hold the marker reports every time rather than never.
    """
    marker = store.root / ".notices" / hashlib.sha256(key.encode("utf-8", "surrogateescape")).hexdigest()
    if marker.exists():
        return False
    try:
        marker.parent.mkdir(exist_ok=True)
        marker.touch()
    except OSError:
        pass
    return True


def _import_legacy_quietly(store):
    """Bring the current repo's old records into the shared store; report on stderr, never on stdout.

    The change of store is announced once per repo and each unreadable legacy record once, so a later
    command in the same repo is silent.
    """
    legacy = Path(_legacy_root())
    try:
        imported = store.import_legacy(legacy)
    except OSError as exc:
        print(f"herdr-handoff: legacy import failed: {exc}", file=sys.stderr)
        return
    outbox = legacy / "outbox"
    for name, reason in store.failed_legacy:             # reported every time, and no marker: it is retried
        print(f"herdr-handoff: legacy import failed: {name}: {reason}", file=sys.stderr)
    if (imported or store.skipped_legacy or any(outbox.glob("handoff_*.json"))) and not store.failed_legacy \
            and _first_time(store, f"legacy:{legacy.resolve()}"):
        print(f"herdr-handoff: handoffs are now kept in one shared store, {store.root}; this repo's "
              f".herdr/handoffs is only read once, to import its records.  Use --root to choose another store.",
              file=sys.stderr)
    if imported:
        print(f"herdr-handoff: imported {len(imported)} record(s) from this repo's .herdr/handoffs: "
              + ", ".join(imported), file=sys.stderr)
    for name, reason in store.skipped_legacy:
        stamp = (outbox / name).stat().st_mtime_ns if (outbox / name).exists() else 0
        if _first_time(store, f"corrupt:{legacy.resolve()}:{name}:{stamp}"):
            print(f"herdr-handoff: skipped legacy record {name}: {reason}", file=sys.stderr)


def _harness_binary(harness):
    """Resolve the harness executable now, so the successor runs this exact program.

    A pane starts a login shell, which rebuilds PATH; on macOS /usr/libexec/path_helper hoists /etc/paths
    and /etc/paths.d above anything herdr's server environment prepended, so a bare name can start a
    different program than the one intended (measured here: a handoff meant for one harness started
    another).  Returns None when the harness is unspecified or not on PATH, leaving `build_launch_plan` to
    reject an unknown harness and the pane to resolve a bare name.
    """
    if not isinstance(harness, str) or not harness:
        return None
    exe = shutil.which(harness)
    return exe if exe and os.access(exe, os.X_OK) else None


def launch(package, *, root, parent_pane, cwd=None, store=None):
    """Start the target harness in a herdr pane, or return the exact command when herdr is unavailable."""
    target = package.get("target") or {}
    harness = target.get("harness")
    cwd = os.path.abspath(cwd or os.getcwd())
    handoff_file = str(root / "outbox" / f"{package['handoff_id']}.json")
    document = str((store or HandoffStore(root)).write_document(package))
    plan = build_launch_plan(parent_pane, harness, package["handoff_id"], handoff_file,
                             model_hint=target.get("model_hint"), cwd=cwd,
                             prompt=short_prompt(package["handoff_id"], document),
                             fallback_prompt=compact_prompt(package["handoff_id"]),
                             harness_binary=_harness_binary(harness))
    manual = {"handoff_id": package["handoff_id"], "target_harness": harness, "status": "manual_launch_required",
              "command": plan["run_command"], "cwd": cwd, "document": document, "notes": plan["notes"],
              "resume_command": f"herdr-handoff --root {shlex.quote(str(root))} resume {package['handoff_id']}"}
    if not shutil.which("herdr"):
        return {**manual, "reason": "herdr is not installed; run the command in a terminal"}
    label = f"to-{harness}-{package['handoff_id'][-8:]}"
    try:
        pane, result, placement = _place(plan, parent_pane, cwd, label)
    except (subprocess.CalledProcessError, RuntimeError, ValueError, OSError) as exc:
        return {**manual, "reason": f"no usable herdr server ({type(exc).__name__}); run the command in a terminal"}
    _run(["herdr", "pane", "run", pane, plan["run_command"]])
    _announce_pane(pane, label=label, harness=harness, handoff_id=package["handoff_id"])
    return {"handoff_id": package["handoff_id"], "pane_id": pane, "tab_id": result.get("tab_id")
            or (result.get("root_pane") or {}).get("tab_id"), "target_harness": harness,
            "status": "interactive_started", "placement": placement, "document": document,
            "notes": plan["notes"]}


_CONFIRM_POLL_SECONDS = 0.5
_CONFIRM_OUTPUT_TIMEOUT_MS = 1000
_CONFIRM_SCROLLBACK_LINES = 200
_CONFIRM_AGENT_WAIT_MS = 2000
_TAKEOVER_REFUSAL_STATUSES = ("launch_unconfirmed", "launch_failed", "source_close_failed", "source_dirty",
                              "takeover_in_progress", "launch_state_unknown",
                              "subagents_running", "source_exit_failed")

_WORKTREE_REFUSAL_HINTS = {
    "worktree_dirty": "tracked changes are not committed",
    "worktree_unpushed": "commits exist that no remote branch has",
    "worktree_unknown": "git could not be asked whether the worktree is clean",
}


def _worktree_refusal_reason(token, guard):
    """Say what the guard saw and what would clear it, in the space a terminal operator reads."""
    where = guard.get("path") or "the source worktree"
    facts = [guard["detail"]] if guard.get("detail") else []
    if guard.get("tracked_changes"):
        facts.append(f"{guard['tracked_changes']} tracked file(s) uncommitted")
    if guard.get("unpushed"):
        facts.append(f"{guard['unpushed']} commit(s) no remote has")
    if guard.get("untracked") and token != "worktree_unknown":
        facts.append(f"{guard['untracked']} untracked file(s) ignored by this check")
    return (f"{token}: {where} ({', '.join(facts) or _WORKTREE_REFUSAL_HINTS.get(token, token)}); the "
            "successor is live and the source pane was left running. Commit or push the work and run "
            "takeover again, or pass --allow-dirty-source to close the source pane anyway.")


def _pane_view(pane):
    """herdr's own view of a pane, or None when herdr cannot see it (closed, or no server)."""
    try:
        result = _herdr_json(["herdr", "pane", "get", pane]).get("result") or {}
    except (subprocess.CalledProcessError, ValueError, OSError):
        return None
    return result.get("pane") if isinstance(result.get("pane"), dict) else result


def _pane_tab(pane):
    view = _pane_view(pane)
    tab = (view or {}).get("tab_id")
    return tab if isinstance(tab, str) else None


def _pane_cwd(pane):
    """The directory herdr itself reports for `pane` - the worktree its session was sitting in."""
    view = _pane_view(pane) or {}
    for key in ("foreground_cwd", "cwd"):
        value = view.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _agent_label(pane):
    """The agent herdr detected inside `pane` - herdr's observation, never our own assertion."""
    label, _ = _agent_state(pane)
    return label


def _agent_state(pane, *, wait_ms=None):
    """Read the agent herdr detected in `pane`, optionally letting herdr itself wait for a settled state.

    `herdr agent wait` only answers once the agent reaches idle/working/blocked/done (never the transient
    ``unknown``), and a pane whose process has left answers `agent_not_found`, which is how a successor
    that started and died is told apart from one that is still there.
    """
    argv = ["herdr", "agent", "wait", pane] if wait_ms else ["herdr", "agent", "get", pane]
    if wait_ms:
        argv += ["--until", "idle", "--until", "working", "--until", "blocked", "--until", "done",
                 "--timeout", str(int(wait_ms))]
    try:
        agent = (_herdr_json(argv).get("result") or {}).get("agent")
    except (subprocess.CalledProcessError, ValueError, OSError):
        return None, None
    if not isinstance(agent, dict):
        return None, None
    return agent.get("agent"), agent.get("agent_status")


def agent_session_of(reply):
    """The harness session id in an `herdr agent get` reply, as herdr gave it (an id, or a session path)."""
    agent = (reply.get("result") or {}).get("agent")
    session = agent.get("agent_session") if isinstance(agent, dict) else None
    value = session.get("value") if isinstance(session, dict) else None
    return value if isinstance(value, str) and value.strip() else None


def _agent_session(pane):
    try:
        return agent_session_of(_herdr_json(["herdr", "agent", "get", pane]))
    except (subprocess.CalledProcessError, ValueError, OSError):
        return None


def _wait_for_line(pane, text):
    """A pane line herdr matched (unwrapped, scrollback included), or None within the short window."""
    try:
        result = _herdr_json(["herdr", "pane", "wait-output", pane, "--match", text, "--source", "recent-unwrapped",
                              "--timeout", str(_CONFIRM_OUTPUT_TIMEOUT_MS)]).get("result") or {}
    except (subprocess.CalledProcessError, ValueError, OSError):
        return None
    line = result.get("matched_line")
    return line if isinstance(line, str) else None


def _read_recent(pane):
    """The last `_CONFIRM_SCROLLBACK_LINES` rows of the pane, scrollback included ('' when unreadable)."""
    try:
        return _run(["herdr", "pane", "read", pane, "--source", "recent",
                     "--lines", str(_CONFIRM_SCROLLBACK_LINES)]).stdout
    except (subprocess.CalledProcessError, OSError):
        return ""


_OWN_WORDS = 8
"""Letters and digits beside the id that are not the typed command, for them to count as the successor's own."""


def id_outside_command(snapshot, handoff_id, typed_command):
    """Text around an occurrence of `handoff_id` in `snapshot` that is the successor's, not the launch echo.

    The launcher typed `typed_command` into the pane, so the shell echoes the id back inside it, and
    that echo is not the successor speaking: behind a prompt, redrawn with other spacing or quoting,
    wrapped over rows, inside a box whose border is drawn on every row.  So both sides are reduced to
    ASCII letters and digits (rows joined), which drops all of that.  An occurrence of the id is the echo
    when, going outward from it, the text matches the command until the command itself ends; it is proof
    only when no spot of the command explains it, and at least `_OWN_WORDS` letters or digits next to it
    stop matching before the command's edge and are not a piece of the command either.  A bare id has
    none, and anything undecidable is not proof, as is a missing `typed_command`.  The text returned is the
    reduced one around the id (underscores and punctuation are gone from it).
    """
    if not typed_command or not handoff_id:
        return None
    reduce = lambda text: re.sub(r"[^A-Za-z0-9]", "", text)  # noqa: E731
    text, command, target = reduce(snapshot), reduce(typed_command), reduce(handoff_id)
    if not target:
        return None
    width = len(target)
    anchors = [m.start() for m in re.finditer(re.escape(target), command)]

    def own_words(start, anchor):
        left = 0
        while left < min(start, anchor) and text[start - 1 - left] == command[anchor - 1 - left]:
            left += 1
        right = 0
        while start + width + right < len(text) and anchor + width + right < len(command) \
                and text[start + width + right] == command[anchor + width + right]:
            right += 1
        before = text[start - left - _OWN_WORDS:start - left] if left < anchor and start - left >= _OWN_WORDS else ""
        end = start + width + right
        after = text[end:end + _OWN_WORDS] if anchor + width + right < len(command) else ""
        return any(len(side) == _OWN_WORDS and side not in command for side in (before, after))

    for found in re.finditer(re.escape(target), text):
        if not any(not own_words(found.start(), anchor) for anchor in anchors):
            return text[max(0, found.start() - 60):found.end() + 60]
    return None


def _successor_spoke(pane, handoff_id, typed_command):
    """Evidence that the id is in the pane's output other than in the command typed to start it, else None.

    Read from the last `_CONFIRM_SCROLLBACK_LINES` rows of scrollback (a successor that printed the id and
    then scrolled it off the screen has still printed it), never from one matched row: a row of a wrapped
    echo has lost the context that shows it is an echo.
    """
    return id_outside_command(_read_recent(pane), handoff_id, typed_command)


def confirm_successor(pane, *, harness, handoff_id, timeout, typed_command):
    """Wait for herdr to observe the successor: its agent detected, and its pane producing output.

    The launcher is not allowed to believe its own optimism here - both facts are read back from herdr
    about the pane it hosts.  Until both hold, the session being replaced stays open.  Neither fact is
    remembered between polls: a harness that boots and then dies is detected for a moment, and a pane
    whose process has left answers `agent_not_found`, so the proof has to hold at the moment of closing.
    The id only counts when it is on a line that is not the typed launch command (`typed_command`).
    """
    deadline = time.monotonic() + max(0.0, float(timeout))
    evidence = {}
    while True:
        remaining = max(0.0, deadline - time.monotonic())
        matched = _successor_spoke(pane, handoff_id, typed_command)
        agent, status = _agent_state(pane, wait_ms=min(_CONFIRM_AGENT_WAIT_MS, max(200, int(remaining * 1000))))
        evidence = {"agent": agent, "agent_status": status,
                    "matched_line": matched[:200] if isinstance(matched, str) else None}
        if agent == harness and matched:
            return {"confirmed": True, "evidence": evidence}
        if time.monotonic() >= deadline:
            return {"confirmed": False, "evidence": evidence}
        time.sleep(_CONFIRM_POLL_SECONDS)


def close_pane(pane):
    """Close the pane being replaced.  herdr refuses to close a pane that is already gone, and that is
    the outcome we wanted, so a vanished pane counts as done rather than as failure."""
    try:
        _herdr_json(["herdr", "pane", "close", pane])
        return {"closed": True, "already": False, "reason": ""}
    except subprocess.CalledProcessError as exc:
        detail = f"{exc.stdout or ''}{exc.stderr or ''}"
        if "pane_not_found" in detail:
            return {"closed": True, "already": True, "reason": "source pane was already closed"}
        return {"closed": False, "already": False,
                "reason": f"herdr refused to close the source pane: {detail.strip()[:200]}"}


_EXIT_TEXT_KEY_PAUSE_SECONDS = 0.3
_EXIT_POLL_SECONDS = 0.5


_SHELLS = frozenset({"sh", "bash", "zsh", "fish", "dash", "ksh", "tcsh", "csh", "nu", "pwsh"})
_PS_TIMEOUT_SECONDS = 5


def _comm_of(pid):
    """The executable name of `pid` according to `ps`, or "" when it cannot be had."""
    try:
        done = subprocess.run(["ps", "-p", str(pid), "-o", "comm="], capture_output=True, text=True,
                              timeout=_PS_TIMEOUT_SECONDS, env={**os.environ, "LC_ALL": "C"})
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


def _children_of(pid):
    """Executable names of the processes whose parent is `pid`, or None when `ps` cannot say."""
    try:
        done = subprocess.run(["ps", "-A", "-o", "pid=,ppid=,comm="], capture_output=True, text=True,
                              timeout=_PS_TIMEOUT_SECONDS, env={**os.environ, "LC_ALL": "C"})
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    names = []
    for line in done.stdout.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[1] == str(pid):
            names.append(os.path.basename(parts[2]) or parts[2])
    return names


def _is_pid(value):
    return isinstance(value, int) and not isinstance(value, bool)


def harness_exited(info, agent_present, *, comm_of=_comm_of, children_of=_children_of):
    """Whether the pane holds nothing but an idle shell, judged from herdr's own observations.

    `info` is `herdr pane process-info`'s `process_info`; `agent_present` is whether `herdr agent get`
    reports an agent in the pane.  The answer is True only when all of these hold, and any missing datum
    reads as False (the source keeps running):
      - herdr reports no agent in the pane;
      - `shell_pid` and a non-empty `foreground_processes` list are present (an idle shell is listed as
        its own foreground process, so an empty list is not evidence of anything);
      - every foreground process is the shell process itself;
      - that process is a real shell by executable name.  A harness started with `exec` (or as the pane's
        root command) has the shell's pid, so the pid alone proves nothing.
    """
    if agent_present is not False or not isinstance(info, dict):
        return False
    shell_pid, procs = info.get("shell_pid"), info.get("foreground_processes")
    if not _is_pid(shell_pid) or not isinstance(procs, list) or not procs:
        return False
    if any(not isinstance(proc, dict) or proc.get("pid") != shell_pid for proc in procs):
        return False
    name = procs[0].get("name")
    if not isinstance(name, str) or not name:
        name = comm_of(shell_pid)
    if not (isinstance(name, str) and os.path.basename(name).lstrip("-") in _SHELLS):
        return False
    children = children_of(shell_pid)
    return isinstance(children, list) and not children


def _gone(exc):
    return "pane_not_found" in f"{exc.stdout or ''}{exc.stderr or ''}"


def _observe(pane):
    """(gone, process_info, agent_present) for `pane`; any herdr error but a vanished pane is raised."""
    try:
        reply = _herdr_json(["herdr", "pane", "process-info", "--pane", pane])
    except subprocess.CalledProcessError as exc:
        if _gone(exc):
            return True, None, None
        raise
    info = (reply.get("result") or {}).get("process_info")
    try:
        agent = (_herdr_json(["herdr", "agent", "get", pane]).get("result") or {}).get("agent")
    except subprocess.CalledProcessError as exc:
        if _gone(exc):
            return True, None, None
        if "agent_not_found" not in f"{exc.stdout or ''}{exc.stderr or ''}":
            raise
        agent = None
    return False, info, isinstance(agent, dict)


def _source_evidence(pane):
    """What herdr shows in the source pane now, for the record of a refused exit."""
    try:
        gone, info, agent = _observe(pane)
    except (subprocess.CalledProcessError, ValueError, OSError):
        return {"observed": False}
    procs = (info or {}).get("foreground_processes")
    names = [str(proc.get("name") or proc.get("argv0") or "?") for proc in procs if isinstance(proc, dict)] \
        if isinstance(procs, list) else []
    shell_pid = (info or {}).get("shell_pid")
    children = _children_of(shell_pid) if _is_pid(shell_pid) else None
    return {"observed": True, "gone": gone, "agent": agent, "foreground": names, "children": children}


def exit_source(pane, *, text, keys, timeout):
    """Ask the harness in `pane` to exit, then wait until it has.  True means nothing is left to protect.

    The pane counts as exited when it is gone, or when `harness_exited` says only an idle shell is left.
    Typing happens only when herdr reports an agent in the pane: anything else holding the foreground (an
    editor, a REPL, a running command) must not receive an exit command, so that returns False.  The text
    goes in first and the keys follow after a short pause: Codex drops an Enter that arrives in the same
    instant as the text.  Any other herdr failure, or an agent still there after `timeout` seconds,
    returns False so the caller leaves the source pane open.  A harness that is suspended (Ctrl-Z) is
    still read as present for as long as herdr reports its agent.
    """
    if not isinstance(pane, str) or not pane:
        return False
    try:
        gone, info, agent = _observe(pane)
        if gone or harness_exited(info, agent):
            return True
        if not agent:
            return False
        try:
            if text:
                _run(["herdr", "pane", "send-text", pane, text])
            if text and keys:
                time.sleep(_EXIT_TEXT_KEY_PAUSE_SECONDS)
            if keys:
                _run(["herdr", "pane", "send-keys", pane, *keys])
        except subprocess.CalledProcessError as exc:
            return _gone(exc)
        deadline = time.monotonic() + timeout
        while True:
            time.sleep(_EXIT_POLL_SECONDS)
            gone, info, agent = _observe(pane)
            if gone or harness_exited(info, agent):
                return True
            if time.monotonic() >= deadline:
                return False
    except (subprocess.CalledProcessError, ValueError, OSError):
        return False


def takeover(package, *, store=None, root=None, **options):
    """Take `package` over, one process at a time per handoff; see `_takeover` for what that does.

    A second process taking over the same handoff waits a few seconds and then gets `takeover_in_progress`.
    The record is read again once the lock is held: whoever held it before may have finished the move.
    """
    store = store or HandoffStore(root)
    handoff_id = package["handoff_id"]
    with contextlib.ExitStack() as stack:
        try:
            stack.enter_context(store.takeover_lock(handoff_id))
        except TakeoverInProgress:
            return {"handoff_id": handoff_id, "status": "takeover_in_progress", "source_closed": False,
                    "idempotent": False, "pane_id": None,
                    "reason": "another takeover of this handoff is running; nothing was launched or closed",
                    "hint": "wait for it to finish and rerun, which reports its outcome"}
        return _takeover(store.get(handoff_id), store=store, **options)


def _takeover(package, *, parent_pane, cwd=None, store, confirm_timeout=25.0, keep_source=False,
              source_cwd=None, allow_dirty=False, allow_running_subagents=False, direction="down",
              source_exit_text=None, source_exit_keys=(), source_exit_timeout=30.0, relaunch=False):
    """Start the successor in the source pane's own tab, wait for proof it is live, close the source pane.

    Every step is written down before it happens, so a repeat trigger (or a crash between steps) either
    continues the recorded move or does nothing: a pane is never split twice and never closed twice.  When
    proof of liveness never arrives, the successor did not land in the source tab, or the source worktree
    still holds work that is not durable, the source pane is left running and the refusal is reported.

    `source_cwd` names the worktree to guard; by default it is the directory herdr reports for the source
    pane, falling back to this process's own directory.  `allow_dirty` is the operator's explicit
    judgement that the work in that worktree may be left behind.  Claude subagents still running from the
    source pane would die with it, so unless `allow_running_subagents` or `keep_source` says otherwise the
    takeover is refused before anything is launched.

    When `source_exit_text` or `source_exit_keys` is given, the source harness is asked to exit and must
    be gone before its pane is closed; if it is not, the pane stays and the status is `source_exit_failed`.
    """
    handoff_id = package["handoff_id"]
    target = package.get("target") or {}
    harness = target.get("harness")
    cwd = os.path.abspath(cwd or os.getcwd())
    handoff_file = str(store.root / "outbox" / f"{handoff_id}.json")
    document = str(store.write_document(package))
    recorded = validate_takeover(package.get("takeover")) or {}
    hint = parent_pane if isinstance(parent_pane, str) and _PANE_ID.fullmatch(parent_pane) else None
    unavailable = None
    if shutil.which("herdr") is None:
        unavailable = "herdr is not installed; run the command in a terminal"
    else:
        try:
            _herdr_json(["herdr", "pane", "list"])
        except (subprocess.CalledProcessError, ValueError, OSError) as exc:
            unavailable = f"no usable herdr server ({type(exc).__name__}); run the command in a terminal"
    if unavailable:
        plan = build_launch_plan(hint, harness, handoff_id, handoff_file, model_hint=target.get("model_hint"),
                                 cwd=cwd, prompt=takeover_prompt(handoff_id, document, root=store.root))
        return {"handoff_id": handoff_id, "target_harness": harness, "status": "manual_launch_required",
                "reason": unavailable, "command": plan["run_command"], "cwd": cwd, "document": document,
                "source_pane": recorded.get("source_pane") or hint, "pane_id": None, "tab_id": None,
                "placement": None, "source_closed": False, "idempotent": False,
                "resume_command": f"herdr-handoff --root {shlex.quote(str(store.root))} resume {handoff_id}"}

    source_pane = next((item for item in (hint, recorded.get("source_pane"), os.environ.get("HERDR_PANE_ID"))
                        if isinstance(item, str) and _PANE_ID.fullmatch(item)), None)
    child = recorded.get("child_pane")
    if child and recorded.get("phase") in ("source_closed", "already_closed"):
        return {"handoff_id": handoff_id, "target_harness": harness, "status": "source_closed",
                "pane_id": child, "tab_id": recorded.get("tab_id"), "source_pane": recorded.get("source_pane"),
                "placement": recorded.get("placement"), "document": document, "source_closed": True,
                "confirmed_at": recorded.get("confirmed_at"), "source_closed_at": recorded.get("source_closed_at"),
                "reason": recorded.get("reason") or "", "idempotent": True}
    if not child and recorded.get("phase") == "launching" and not relaunch:
        return {"handoff_id": handoff_id, "target_harness": harness, "status": "launch_state_unknown",
                "reason": "an earlier takeover recorded that it was placing the successor pane and never "
                          "recorded the pane; a pane may exist that this record does not know",
                "hint": "look for the stray pane and close it, then pass --relaunch to start over",
                "source_pane": recorded.get("source_pane") or source_pane, "pane_id": None, "tab_id": None,
                "placement": None, "document": document, "source_closed": False, "idempotent": False}
    if not child and not source_pane:
        raise SystemExit("takeover needs the source pane identity (HERDR_PANE_ID or --parent-pane); "
                         "use `launch` to hand off without closing anything")

    if not child and source_pane and not keep_source and not allow_running_subagents:
        running = running_subagents(source_pane)
        if running:
            return {"handoff_id": handoff_id, "target_harness": harness, "status": "subagents_running",
                    "reason": f"{len(running)} subagent(s) launched from the source pane are still running",
                    "hint": "wait for them or stop them, then rerun; or pass --allow-running-subagents / "
                            "--keep-source",
                    "subagents": running, "source_pane": source_pane, "pane_id": None, "tab_id": None,
                    "placement": None, "document": document, "source_closed": False, "idempotent": False}

    # also on a rerun: the command typed the first time is rebuilt so its echo is never taken for proof
    plan = build_launch_plan(source_pane, harness, handoff_id, handoff_file,
                             model_hint=target.get("model_hint"), cwd=cwd,
                             prompt=takeover_prompt(handoff_id, document),
                             fallback_prompt=compact_prompt(handoff_id, confirm=True),
                             harness_binary=_harness_binary(harness), direction=direction)
    label = f"to-{harness}-{handoff_id[-8:]}"
    if child:
        pane, placement, tab_id = child, recorded.get("placement") or "split", recorded.get("tab_id")
        if _pane_tab(pane) is None:
            store.record_takeover(handoff_id, {"phase": "unconfirmed", "reason": "successor pane is gone"})
            return {"handoff_id": handoff_id, "target_harness": harness, "status": "launch_unconfirmed",
                    "reason": "successor pane is gone", "pane_id": pane, "tab_id": tab_id,
                    "source_pane": source_pane, "placement": placement, "document": document,
                    "source_closed": False, "idempotent": True}
    else:
        launching = {"phase": "launching", "source_pane": source_pane, "reason": None}
        if direction != "down":                 # the default stays out of the record, which older readers know
            launching["direction"] = direction
        store.record_takeover(handoff_id, launching)
        try:
            pane, result, placement = _place(plan, source_pane, cwd, label)
        except (subprocess.CalledProcessError, RuntimeError, ValueError, OSError) as exc:
            store.record_takeover(handoff_id, {"phase": "launch_failed",
                                               "reason": f"no usable herdr server ({type(exc).__name__})"})
            return {"handoff_id": handoff_id, "target_harness": harness, "status": "launch_failed",
                    "reason": f"no usable herdr server ({type(exc).__name__}); the source pane was left running",
                    "command": plan["run_command"], "cwd": cwd, "document": document,
                    "source_pane": source_pane, "pane_id": None, "tab_id": None, "placement": None,
                    "source_closed": False, "idempotent": False}
        # the pane is on record before the successor can start, so its own `confirm` finds it
        store.record_takeover(handoff_id, {"child_pane": pane})
        _run(["herdr", "pane", "run", pane, plan["run_command"]])
        _announce_pane(pane, label=label, harness=harness, handoff_id=handoff_id)
        tab_id = _pane_tab(pane) or (result.get("tab_id") or (result.get("root_pane") or {}).get("tab_id"))
        store.record_takeover(handoff_id, {"phase": "launched", "source_pane": source_pane, "child_pane": pane,
                                           "tab_id": tab_id if isinstance(tab_id, str) else None,
                                           "placement": placement, "reason": None})
        for state in ("claimed", "accepted", "in_progress"):
            store.transition(handoff_id, state, actor="local-takeover")

    def report(status, *, block, source_closed, reason="", **extra):
        return {"handoff_id": handoff_id, "target_harness": harness, "status": status, "pane_id": pane,
                "tab_id": tab_id, "source_pane": source_pane, "placement": placement, "document": document,
                "source_closed": source_closed, "confirmed_at": block.get("confirmed_at"),
                "source_closed_at": block.get("source_closed_at"), "reason": reason, "idempotent": False, **extra}

    block = validate_takeover(store.get(handoff_id).get("takeover")) or {}
    if block.get("confirmed_at"):
        confirmed, evidence = True, {**(block.get("evidence") or {}), "source": "confirmed record"}
    else:
        verdict = confirm_successor(pane, harness=harness, handoff_id=handoff_id, timeout=confirm_timeout,
                                    typed_command=plan["run_command"])
        confirmed, evidence = verdict["confirmed"], verdict["evidence"]
    if not confirmed:
        detail = (f"herdr saw agent={evidence.get('agent') or 'nothing'}"
                  f"/{evidence.get('agent_status') or 'no settled state'}"
                  f", handoff id {'seen' if evidence.get('matched_line') else 'not seen'} in its output")
        refused = store.record_takeover(handoff_id, {"phase": "unconfirmed",
                                                     "reason": f"unconfirmed: {detail}",
                                                     "evidence": evidence})["takeover"]
        return report("launch_unconfirmed", block=refused, source_closed=False,
                      reason=f"unconfirmed: {detail}", evidence=evidence)
    block = store.record_takeover(handoff_id, {"phase": "confirmed", "child_pane": pane, "evidence": evidence},
                                  target_ids={"pane_id": pane, "session_id": _agent_session(pane)})["takeover"]
    if keep_source:
        return report("confirmed", block=block, source_closed=False)

    source_tab = _pane_tab(source_pane) if source_pane else None
    guard, override = None, {}
    if source_pane and source_tab is None:  # the session being replaced is already gone: nothing to close
        closed = {"closed": True, "already": True, "reason": "source pane was already closed"}
    else:
        guard = source_worktree_state(source_cwd or _pane_cwd(source_pane) or os.getcwd())
        decision = decide_takeover_close(placement=placement, same_tab=source_tab is not None
                                         and source_tab == tab_id, source_pane=source_pane, child_pane=pane,
                                         confirmed=True, worktree=guard, allow_dirty=allow_dirty)
        if not decision["close"]:
            # A placement refusal is the old `launch_unconfirmed`; a worktree refusal is its own status,
            # because the thing to fix is the work, not the successor, and retrying changes nothing.
            dirty = decision["reason"].startswith("worktree_")
            status = "source_dirty" if dirty else "launch_unconfirmed"
            reason = _worktree_refusal_reason(decision["reason"], guard) if dirty else decision["reason"]
            store.record_takeover(handoff_id, {"phase": "confirmed", "child_pane": pane, "reason": reason,
                                               "evidence": {**(block.get("evidence") or {}),
                                                            "worktree": guard}})
            return report(status, block=block, source_closed=False, reason=reason, worktree=guard)
        if (source_exit_text or source_exit_keys) and not exit_source(
                source_pane, text=source_exit_text, keys=source_exit_keys, timeout=source_exit_timeout):
            reason = "the source harness did not exit after the exit command; its pane was left open"
            store.record_takeover(handoff_id, {"phase": "source_exit_failed", "child_pane": pane, "reason": reason,
                                               "evidence": {**(block.get("evidence") or {}),
                                                            "source_exit": _source_evidence(source_pane)}})
            return report("source_exit_failed", block=block, source_closed=False, reason=reason, worktree=guard)
        if allow_dirty and worktree_close_refusal(guard):
            override = {"dirty_allowed": True, "unpushed_commits": unpushed_commits(guard.get("path"))}
        closed = close_pane(source_pane)
        if not closed["closed"]:
            store.record_takeover(handoff_id, {"phase": "confirmed", "child_pane": pane, "reason": closed["reason"]})
            return report("source_close_failed", block=block, source_closed=False, reason=closed["reason"])
    block = store.record_takeover(handoff_id, {"phase": "already_closed" if closed["already"] else "source_closed",
                                                "child_pane": pane, "reason": closed["reason"] or None,
                                                **override})["takeover"]
    return report("source_closed", block=block, source_closed=True, reason=closed["reason"],
                  **({"worktree": guard} if guard else {}))


def confirm_takeover(store, handoff_id, *, pane=None):
    """Record that the successor session confirms itself, from the pane it is actually running in."""
    package = store.get(handoff_id)
    recorded = validate_takeover(package.get("takeover")) or {}
    if not recorded.get("child_pane"):
        raise SystemExit("herdr-handoff: no successor pane is recorded for this handoff yet; "
                         "confirm applies once a takeover has placed one")
    candidate = pane or os.environ.get("HERDR_PANE_ID")
    if not isinstance(candidate, str) or not _PANE_ID.fullmatch(candidate):
        raise SystemExit("confirm needs the successor pane identity (HERDR_PANE_ID or --pane)")
    if recorded.get("child_pane") and recorded["child_pane"] != candidate:
        raise SystemExit(f"only the successor pane {recorded['child_pane']} can confirm this handoff")
    if recorded.get("source_closed_at"):
        return {"handoff_id": handoff_id, "phase": recorded["phase"], "pane_id": candidate,
                "confirmed_at": recorded.get("confirmed_at"),
                "source_closed_at": recorded["source_closed_at"], "idempotent": True}
    block = store.record_takeover(handoff_id, {"phase": "confirmed", "child_pane": candidate,
                                               "source_pane": recorded.get("source_pane"),
                                               "evidence": {"source": "successor pane", "pane": candidate}})["takeover"]
    return {"handoff_id": handoff_id, "phase": block["phase"], "pane_id": candidate,
            "source_pane": block.get("source_pane"), "confirmed_at": block["confirmed_at"], "idempotent": False}


def _sync_after_start(store, handoff_id, control_url, token):
    """Mirror claim/accept/start on the control plane; a pane that started stays started if sync fails."""
    if not control_url:
        return "not_configured"
    try:
        for action in ("claim", "accept", "start"):
            store.remote_transition(control_url, handoff_id, action, token=token)
        return "in_progress"
    except Exception as exc:  # the pane is already running: keep the local result, expose the sync failure
        return f"sync_failed:{type(exc).__name__}"


def _load_spec(source):
    raw = sys.stdin.read() if source == "-" else open(source, encoding="utf-8").read()
    try:
        spec = json.loads(raw)
    except ValueError as exc:
        raise SystemExit(f"herdr-handoff: --from-json is not valid JSON: {exc}")
    if not isinstance(spec, dict):
        raise SystemExit("herdr-handoff: --from-json must be a JSON object")
    unknown = sorted(set(spec) - _JSON_KEYS)
    if unknown:
        raise SystemExit("herdr-handoff: --from-json has unknown keys: " + ", ".join(unknown))
    for key in _LIST_FIELDS:
        value = spec.get(key)
        if value is not None and not (isinstance(value, list) and all(isinstance(i, str) for i in value)):
            raise SystemExit(f"herdr-handoff: {key} must be a list of strings")
    return spec


def main(argv=None):
    parser = argparse.ArgumentParser(prog="herdr-handoff")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--root", default=None,
                        help="handoff root (default: see README, normally the shared store)")
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create")
    create.add_argument("--goal", required=True)
    create.add_argument("--source-harness")
    create.add_argument("--target-harness")
    create.add_argument("--model")
    create.add_argument("--session-id")
    create.add_argument("--predecessor-id", help="handoff_<32 hex> of the handoff this one continues")
    create.add_argument("--source-pane", default=os.environ.get("HERDR_PANE_ID"),
                        help="pane the handoff is made from (default: $HERDR_PANE_ID)")
    create.add_argument("--source-session", help="harness session id of the source")
    create.add_argument("--mission-id")
    create.add_argument("--room-id")
    create.add_argument("--remaining", action="append", default=[])
    create.add_argument("--completed", action="append", default=[])
    for flag in ("decision", "risk", "validation", "constraint", "next-command", "file", "pr", "unverified"):
        create.add_argument(f"--{flag}", action="append", default=[])
    create.add_argument("--fallback", action="store_true",
                        help="mark the record as written by the fallback writer (no skill/harness integration)")
    create.add_argument("--resume-point")
    create.add_argument("--repo")
    create.add_argument("--branch")
    create.add_argument("--worktree")
    create.add_argument("--workspace-cwd")
    create.add_argument("--report", help="absolute path of the full published summary report")
    create.add_argument("--report-dashboard")
    create.add_argument("--from-json", metavar="PATH|-", help="JSON object of the same fields; flags are appended")
    create.add_argument("--artifact", action="append", default=[])
    create.add_argument("--memory-ref", action="append", default=[], metavar="MEMORY_ID:BANK_ID:REVISION",
                        help="reference a memory by id (never its text); needs a configured herdr-memory CLI")
    inspect = sub.add_parser("inspect")
    inspect.add_argument("handoff_id")
    sub.add_parser("list")
    store_parser = sub.add_parser("store", help="manage the handoff store")
    store_sub = store_parser.add_subparsers(dest="store_command", required=True)
    import_parser = store_sub.add_parser("import-legacy",
                                         help="copy per-repo .herdr/handoffs records this store lacks")
    import_parser.add_argument("legacy_root", nargs="?", default=None,
                               help="a legacy handoff root (default: the current repo's .herdr/handoffs)")
    transition = sub.add_parser("transition")
    transition.add_argument("handoff_id")
    transition.add_argument("state")
    transition.add_argument("--actor", required=True)
    resume = sub.add_parser("resume")
    resume.add_argument("handoff_id")
    sync = sub.add_parser("sync")
    sync.add_argument("--url", required=True)
    sync.add_argument("--token")
    sync.add_argument("--handoff-id")
    launch_parser = sub.add_parser("launch")
    launch_parser.add_argument("handoff_id")
    launch_parser.add_argument("--parent-pane", default=os.environ.get("HERDR_PANE_ID"))
    launch_parser.add_argument("--cwd", default=os.getcwd())
    takeover_parser = sub.add_parser(
        "takeover", formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=("refusals (exit 1, the source pane stays running, the successor is never orphaned):\n"
                "  launch_unconfirmed    herdr never reported the successor live, or it left the source tab\n"
                "  launch_failed         the successor pane could not be started at all\n"
                "  source_close_failed   herdr refused the close\n"
                "  source_dirty          the source worktree holds work that is not durable: uncommitted\n"
                "                        tracked changes, or commits no remote branch has.  Untracked files\n"
                "                        alone never refuse - harness state is untracked everywhere.\n"
                "  subagents_running     Claude subagents launched from the source pane are still running\n"
                "                        (checked before anything is launched; the handoff stays queued).\n"
                "                        Wait for them or stop them, or pass --allow-running-subagents.\n"
                "  source_exit_failed    the source harness was still running after --source-exit-text/-key;\n"
                "                        both panes stay up, and a rerun reuses the successor.\n"
                "Do not retry a refusal blindly: make the work durable (commit, push), then run takeover\n"
                "  takeover_in_progress  another takeover of this handoff holds its lock (waits 5 s first);\n"
                "                        nothing was launched or closed.  Rerun once it has finished.\n"
                "  launch_state_unknown  an earlier takeover died after recording that it was placing the\n"
                "                        successor and before recording the pane.  Close any stray pane,\n"
                "                        then pass --relaunch.\n"
                "Do not retry a refusal blindly: make the work durable (commit, push), then run takeover\n"
                "again, or close the source anyway with --allow-dirty-source."))
    takeover_parser.add_argument("handoff_id")
    takeover_parser.add_argument("--parent-pane", default=os.environ.get("HERDR_PANE_ID"))
    takeover_parser.add_argument("--cwd", default=os.getcwd())
    takeover_parser.add_argument("--confirm-timeout", type=float, default=25.0,
                                 help="seconds to wait for herdr to observe the successor before refusing to close")
    takeover_parser.add_argument("--keep-source", action="store_true",
                                 help="start and confirm the successor but leave the source pane running")
    takeover_parser.add_argument("--direction", choices=sorted(DIRECTIONS), default="down",
                                 help="where the successor pane goes relative to the source (default: down)")
    takeover_parser.add_argument("--source-cwd", default=None,
                                 help="worktree to check before closing the source pane (default: the "
                                      "directory herdr reports for that pane, else this one)")
    takeover_parser.add_argument("--allow-dirty-source", action="store_true",
                                 help="close the source pane even with uncommitted or unpushed work in it "
                                      "(untracked files never block, so this flag is not needed for them)")
    takeover_parser.add_argument("--allow-running-subagents", action="store_true",
                                 help="hand off even though Claude subagents launched from the source pane "
                                      "are still running (they die when the pane closes)")
    takeover_parser.add_argument("--relaunch", action="store_true",
                                 help="start a new successor although an earlier takeover died before "
                                      "recording its pane (status launch_state_unknown)")
    takeover_parser.add_argument("--source-exit-text", default=None,
                                 help=f"text typed into the source pane to make its harness exit (max "
                                      f"{MAX_TYPED_COMMAND} bytes); sent before --source-exit-key")
    takeover_parser.add_argument("--source-exit-key", action="append", default=[],
                                 help="key pressed in the source pane after the text, e.g. Enter (repeatable)")
    takeover_parser.add_argument("--source-exit-timeout", type=float, default=30.0,
                                 help="seconds to wait for the source harness to exit before refusing to close")
    confirm_parser = sub.add_parser("confirm")
    confirm_parser.add_argument("handoff_id")
    confirm_parser.add_argument("--pane", default=os.environ.get("HERDR_PANE_ID"))
    args = parser.parse_args(argv)
    shared = False
    if args.root is None:
        args.root, shared = _default_root()
    store = HandoffStore(args.root)
    if shared and args.command != "store":
        _import_legacy_quietly(store)
    if args.command == "store":
        try:
            imported = store.import_legacy(args.legacy_root or _legacy_root())
        except OSError as exc:
            print(f"herdr-handoff: legacy import failed: {exc}", file=sys.stderr)
            return 1
        if store.failed_legacy:
            for name, reason in store.failed_legacy:
                print(f"herdr-handoff: legacy import failed: {name}: {reason}", file=sys.stderr)
            return 1
        out = {"imported": imported, "skipped": [{"file": n, "reason": r} for n, r in store.skipped_legacy]}
    elif args.command == "create":
        artifacts = [{"artifact_id": item} for item in args.artifact]
        refs = []
        for item in args.memory_ref:
            parts = item.split(":")
            if len(parts) != 3:
                raise SystemExit("--memory-ref must be MEMORY_ID:BANK_ID:REVISION")
            refs.append(dict(zip(("memory_id", "bank_id", "revision"), parts)))
        spec = _load_spec(args.from_json) if args.from_json else {}
        flags = {"completed": args.completed, "remaining": args.remaining, "decisions": args.decision,
                 "risks": args.risk, "validation": args.validation, "constraints": args.constraint,
                 "next_commands": args.next_command, "files": args.file, "pull_requests": args.pr,
                 "unverified": args.unverified}
        lists = {key: list(spec.get(key) or []) + flags[key] for key in flags}
        workspace = dict(spec.get("workspace") or {}) if isinstance(spec.get("workspace"), dict) else spec.get("workspace")
        for key, value in (("repo", args.repo), ("branch", args.branch), ("worktree", args.worktree),
                           ("cwd", args.workspace_cwd)):
            if value:
                workspace = {**(workspace or {}), key: value}
        report = spec.get("report")
        if args.report or args.report_dashboard:
            report = {**(report or {}), **({"path": args.report} if args.report else {}),
                      **({"dashboard": args.report_dashboard} if args.report_dashboard else {})}
        try:
            package = build_handoff(goal=args.goal, artifacts=artifacts, source_harness=args.source_harness,
                                    target_harness=args.target_harness, model_hint=args.model,
                                    session_id=args.session_id, mission_id=args.mission_id,
                                    room_id=args.room_id, memory_context=build_memory_context(refs),
                                    provenance={"fallback": True} if args.fallback else None,
                                    workspace=workspace, report=report,
                                    resume_point=args.resume_point or spec.get("resume_point"),
                                    predecessor_id=args.predecessor_id, source_pane_id=args.source_pane,
                                    source_session_id=args.source_session, **lists)
        except ValueError as exc:
            raise SystemExit(f"herdr-handoff: invalid handoff: {exc}")
        out = store.enqueue(package)
        if args.predecessor_id:
            warning = store.link_successor(args.predecessor_id, out["handoff_id"])
            if warning:
                print(f"herdr-handoff: warning: {warning}", file=sys.stderr)
                out = {**out, "warnings": [warning]}
    elif args.command == "inspect":
        out = store.get(args.handoff_id)
    elif args.command == "list":
        out = store.list()
    elif args.command == "resume":
        package = store.get(args.handoff_id)
        resolutions = resolve_memory_refs(package["memory_context"]) if package.get("memory_context") else []
        document = str(store.write_document(package))
        out = {"handoff_id": args.handoff_id, "document": document,
               "prompt": resume_prompt(package, resolutions, document=document)}
        if resolutions:
            out["memory_refs"] = [{**r["ref"], "status": r["status"]} for r in resolutions]
    elif args.command == "sync":
        out = store.sync(args.url, token=args.token, handoff_id=args.handoff_id)
    elif args.command == "launch":
        package = store.get(args.handoff_id)
        if package.get("state") != "queued":
            raise SystemExit(f"handoff must be queued before launch (state={package.get('state')})")
        control_url = os.environ.get("HERDR_CONTROL_PLANE_URL")
        control_token = os.environ.get("HERDR_API_KEY")
        if control_url:
            store.sync(control_url, token=control_token, handoff_id=args.handoff_id)
        try:
            out = launch(package, root=store.root, parent_pane=args.parent_pane, cwd=args.cwd, store=store)
        except ValueError as exc:
            raise SystemExit(str(exc)) from None      # e.g. no target harness named: say it, no traceback
        if out["status"] != "interactive_started":
            print(json.dumps(out, indent=2, sort_keys=True))
            return 0
        store.transition(args.handoff_id, "claimed", actor="local-launch")
        store.transition(args.handoff_id, "accepted", actor="local-launch")
        store.transition(args.handoff_id, "in_progress", actor="local-launch")
        out["dashboard"] = _sync_after_start(store, args.handoff_id, control_url, control_token)
    elif args.command == "takeover":
        if args.source_exit_text is not None and len(args.source_exit_text.encode()) > MAX_TYPED_COMMAND:
            parser.error(f"--source-exit-text is over {MAX_TYPED_COMMAND} bytes, which herdr cannot type reliably")
        package = store.get(args.handoff_id)
        if package.get("state") != "queued" and not validate_takeover(package.get("takeover")):
            raise SystemExit(f"handoff must be queued before takeover (state={package.get('state')})")
        control_url = os.environ.get("HERDR_CONTROL_PLANE_URL")
        control_token = os.environ.get("HERDR_API_KEY")
        if control_url:
            store.sync(control_url, token=control_token, handoff_id=args.handoff_id)
        try:
            out = takeover(package, root=store.root, parent_pane=args.parent_pane, cwd=args.cwd, store=store,
                           confirm_timeout=args.confirm_timeout, keep_source=args.keep_source,
                           source_cwd=args.source_cwd, allow_dirty=args.allow_dirty_source,
                           allow_running_subagents=args.allow_running_subagents, direction=args.direction,
                           source_exit_text=args.source_exit_text, source_exit_keys=args.source_exit_key,
                           source_exit_timeout=args.source_exit_timeout, relaunch=args.relaunch)
        except ValueError as exc:
            raise SystemExit(str(exc)) from None      # e.g. no target harness named: say it, no traceback
        if out["status"] in ("confirmed", "source_closed"):
            out["dashboard"] = _sync_after_start(store, args.handoff_id, control_url, control_token)
        print(json.dumps(out, indent=2, sort_keys=True))
        return 1 if out["status"] in _TAKEOVER_REFUSAL_STATUSES else 0
    elif args.command == "confirm":
        out = confirm_takeover(store, args.handoff_id, pane=args.pane)
    else:
        out = store.transition(args.handoff_id, args.state, actor=args.actor)
    print(json.dumps(out, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
