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

from herdr_handoff import (HandoffStore, __version__, build_handoff, build_launch_plan, build_memory_context,
                           resolve_memory_refs, resume_prompt, short_prompt)


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


def launch(package, *, root, parent_pane, cwd=None, store=None):
    """Start the target harness in a herdr pane, or return the exact command when herdr is unavailable."""
    target = package.get("target") or {}
    harness = target.get("harness")
    cwd = os.path.abspath(cwd or os.getcwd())
    handoff_file = str(root / "outbox" / f"{package['handoff_id']}.json")
    document = str((store or HandoffStore(root)).write_document(package))
    plan = build_launch_plan(parent_pane, harness, package["handoff_id"], handoff_file,
                             model_hint=target.get("model_hint"), cwd=cwd,
                             prompt=short_prompt(package["handoff_id"], document))
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
    _run(["herdr", "pane", "rename", pane, label])
    _run(["herdr", "pane", "run", pane, plan["run_command"]])
    _run(["herdr", "pane", "report-metadata", pane, "--source", "herdr-handoff",
          "--title", f"handoff -> {harness}", "--token", f"handoff_id={package['handoff_id']}"])
    _run(["herdr", "pane", "report-agent-session", pane, "--source", "herdr-handoff",
          "--agent", harness, "--agent-session-id", package["handoff_id"],
          "--session-start-source", "herdr-handoff"])
    return {"handoff_id": package["handoff_id"], "pane_id": pane, "tab_id": result.get("tab_id")
            or (result.get("root_pane") or {}).get("tab_id"), "target_harness": harness,
            "status": "interactive_started", "placement": placement, "document": document,
            "notes": plan["notes"]}


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
    parser.add_argument("--root", default=os.environ.get("HERDR_HANDOFF_DIR", ".herdr/handoffs"))
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
        out = launch(package, root=store.root, parent_pane=args.parent_pane, cwd=args.cwd, store=store)
        if out["status"] != "interactive_started":
            print(json.dumps(out, indent=2, sort_keys=True))
            return 0
        store.transition(args.handoff_id, "claimed", actor="local-launch")
        store.transition(args.handoff_id, "accepted", actor="local-launch")
        store.transition(args.handoff_id, "in_progress", actor="local-launch")
        if control_url:
            try:
                for action in ("claim", "accept", "start"):
                    store.remote_transition(control_url, args.handoff_id, action, token=control_token)
                dashboard = "in_progress"
            except Exception as exc:  # pane already exists; preserve local result and expose sync failure
                dashboard = f"sync_failed:{type(exc).__name__}"
        out["dashboard"] = dashboard
    else:
        out = store.transition(args.handoff_id, args.state, actor=args.actor)
    print(json.dumps(out, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
