#!/usr/bin/env python3
"""Small JSON CLI used by all four harness skills."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time

from herdr_handoff import (HandoffStore, __version__, build_handoff, build_launch_plan, build_memory_context,
                            compact_prompt, decide_takeover_close, resolve_memory_refs, resume_prompt,
                            short_prompt, takeover_prompt, validate_takeover, _handoff_root)


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
    """Where the outbox lives, for a process that was started by a handoff rather than by a human.

    A successor pane is handed `herdr-handoff confirm <id>` with no path in it - the typed command has a
    byte budget (herdr_handoff.MAX_TYPED_COMMAND) - so the root comes from HERDR_HANDOFF_FILE, which the
    launch command exports, rather than from an argument.
    """
    derived = _handoff_root(os.environ.get("HERDR_HANDOFF_FILE", ""))
    if derived:
        return derived
    return os.environ.get("HERDR_HANDOFF_DIR", ".herdr/handoffs")


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
_CONFIRM_AGENT_WAIT_MS = 2000
_TAKEOVER_REFUSAL_STATUSES = ("launch_unconfirmed", "launch_failed", "source_close_failed")


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


def _wait_for_line(pane, text):
    """A pane line herdr matched, or None within the short window."""
    try:
        result = _herdr_json(["herdr", "pane", "wait-output", pane, "--match", text,
                              "--timeout", str(_CONFIRM_OUTPUT_TIMEOUT_MS)]).get("result") or {}
    except (subprocess.CalledProcessError, ValueError, OSError):
        return None
    line = result.get("matched_line")
    return line if isinstance(line, str) else None


def confirm_successor(pane, *, harness, handoff_id, timeout):
    """Wait for herdr to observe the successor: its agent detected, and its pane producing output.

    The launcher is not allowed to believe its own optimism here - both facts are read back from herdr
    about the pane it hosts.  Until both hold, the session being replaced stays open.  Neither fact is
    remembered between polls: a harness that boots and then dies is detected for a moment, and a pane
    whose process has left answers `agent_not_found`, so the proof has to hold at the moment of closing.
    """
    deadline = time.monotonic() + max(0.0, float(timeout))
    evidence = {}
    while True:
        remaining = max(0.0, deadline - time.monotonic())
        matched = _wait_for_line(pane, handoff_id)
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


def takeover(package, *, root, parent_pane, cwd=None, store=None, confirm_timeout=25.0, keep_source=False):
    """Start the successor in the source pane's own tab, wait for proof it is live, close the source pane.

    Every step is written down before it happens, so a repeat trigger (or a crash between steps) either
    continues the recorded move or does nothing: a pane is never split twice and never closed twice.  When
    proof of liveness never arrives, or the successor did not land in the source tab, the source pane is
    left running and the refusal is reported instead.
    """
    store = store or HandoffStore(root)
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
    if not child and not source_pane:
        raise SystemExit("takeover needs the source pane identity (HERDR_PANE_ID or --parent-pane); "
                         "use `launch` to hand off without closing anything")

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
        store.record_takeover(handoff_id, {"phase": "launching", "source_pane": source_pane, "reason": None})
        plan = build_launch_plan(source_pane, harness, handoff_id, handoff_file,
                                 model_hint=target.get("model_hint"), cwd=cwd,
                                 prompt=takeover_prompt(handoff_id, document),
                                 fallback_prompt=compact_prompt(handoff_id, confirm=True),
                                 harness_binary=_harness_binary(harness))
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
        verdict = confirm_successor(pane, harness=harness, handoff_id=handoff_id, timeout=confirm_timeout)
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
    block = store.record_takeover(handoff_id, {"phase": "confirmed", "child_pane": pane, "evidence": evidence})["takeover"]
    if keep_source:
        return report("confirmed", block=block, source_closed=False)

    source_tab = _pane_tab(source_pane) if source_pane else None
    if source_pane and source_tab is None:  # the session being replaced is already gone: nothing to close
        closed = {"closed": True, "already": True, "reason": "source pane was already closed"}
    else:
        decision = decide_takeover_close(placement=placement, same_tab=source_tab is not None
                                         and source_tab == tab_id, source_pane=source_pane, child_pane=pane,
                                         confirmed=True)
        if not decision["close"]:
            store.record_takeover(handoff_id, {"phase": "confirmed", "child_pane": pane,
                                               "reason": decision["reason"]})
            return report("launch_unconfirmed", block=block, source_closed=False, reason=decision["reason"])
        closed = close_pane(source_pane)
        if not closed["closed"]:
            store.record_takeover(handoff_id, {"phase": "confirmed", "child_pane": pane, "reason": closed["reason"]})
            return report("source_close_failed", block=block, source_closed=False, reason=closed["reason"])
    block = store.record_takeover(handoff_id, {"phase": "already_closed" if closed["already"] else "source_closed",
                                                "child_pane": pane, "reason": closed["reason"] or None})["takeover"]
    return report("source_closed", block=block, source_closed=True, reason=closed["reason"])


def confirm_takeover(store, handoff_id, *, pane=None):
    """Record that the successor session confirms itself, from the pane it is actually running in."""
    package = store.get(handoff_id)
    recorded = validate_takeover(package.get("takeover")) or {}
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
    parser.add_argument("--root", default=_default_root())
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create")
    create.add_argument("--goal", required=True)
    create.add_argument("--source-harness")
    create.add_argument("--target-harness")
    create.add_argument("--model")
    create.add_argument("--session-id")
    create.add_argument("--mission-id")
    create.add_argument("--room-id")
    create.add_argument("--remaining", action="append", default=[])
    create.add_argument("--completed", action="append", default=[])
    for flag in ("decision", "risk", "validation", "constraint", "next-command", "file", "pr", "unverified"):
        create.add_argument(f"--{flag}", action="append", default=[])
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
    takeover_parser = sub.add_parser("takeover")
    takeover_parser.add_argument("handoff_id")
    takeover_parser.add_argument("--parent-pane", default=os.environ.get("HERDR_PANE_ID"))
    takeover_parser.add_argument("--cwd", default=os.getcwd())
    takeover_parser.add_argument("--confirm-timeout", type=float, default=25.0,
                                 help="seconds to wait for herdr to observe the successor before refusing to close")
    takeover_parser.add_argument("--keep-source", action="store_true",
                                 help="start and confirm the successor but leave the source pane running")
    confirm_parser = sub.add_parser("confirm")
    confirm_parser.add_argument("handoff_id")
    confirm_parser.add_argument("--pane", default=os.environ.get("HERDR_PANE_ID"))
    args = parser.parse_args(argv)
    store = HandoffStore(args.root)
    if args.command == "create":
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
                                    workspace=workspace, report=report,
                                    resume_point=args.resume_point or spec.get("resume_point"), **lists)
        except ValueError as exc:
            raise SystemExit(f"herdr-handoff: invalid handoff: {exc}")
        out = store.enqueue(package)
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
        dashboard = "not_configured"
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
        package = store.get(args.handoff_id)
        if package.get("state") != "queued" and not validate_takeover(package.get("takeover")):
            raise SystemExit(f"handoff must be queued before takeover (state={package.get('state')})")
        control_url = os.environ.get("HERDR_CONTROL_PLANE_URL")
        control_token = os.environ.get("HERDR_API_KEY")
        if control_url:
            store.sync(control_url, token=control_token, handoff_id=args.handoff_id)
        try:
            out = takeover(package, root=store.root, parent_pane=args.parent_pane, cwd=args.cwd, store=store,
                           confirm_timeout=args.confirm_timeout, keep_source=args.keep_source)
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
