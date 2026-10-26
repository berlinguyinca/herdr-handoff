#!/usr/bin/env python3
"""Small JSON CLI used by all four harness skills."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys

from herdr_handoff import HandoffStore, __version__, build_handoff, build_launch_plan, resume_prompt


def _run(argv, *, capture=False):
    return subprocess.run(argv, check=True, text=True, capture_output=capture)


def launch(package, *, root, parent_pane, cwd=None):
    target = package.get("target") or {}
    harness = target.get("harness")
    plan = build_launch_plan(parent_pane, harness, package["handoff_id"],
                             str(root / "outbox" / f"{package['handoff_id']}.json"),
                             model_hint=target.get("model_hint"), cwd=cwd)
    split = json.loads(_run(plan["split"], capture=True).stdout)
    result = split.get("result") or {}
    pane = (result.get("pane") or result.get("root_pane") or {}).get("pane_id")
    if not isinstance(pane, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9:_.-]{0,39}", pane):
        raise RuntimeError("herdr did not return a child pane")
    _run(["herdr", "pane", "rename", pane, f"to-{harness}-{package['handoff_id'][-8:]}"])
    _run(["herdr", "pane", "run", pane, plan["run_command"]])
    _run(["herdr", "pane", "report-metadata", pane, "--source", "herdr-handoff",
          "--title", f"handoff → {harness}", "--token", f"handoff_id={package['handoff_id']}"])
    _run(["herdr", "pane", "report-agent-session", pane, "--source", "herdr-handoff",
          "--agent", harness, "--agent-session-id", package["handoff_id"],
          "--session-start-source", "herdr-handoff"])
    _run(["herdr", "pane", "send-text", pane, resume_prompt(package)])
    _run(["herdr", "pane", "send-keys", pane, "enter"])
    return {"handoff_id": package["handoff_id"], "pane_id": pane, "tab_id": result.get("tab_id"),
            "target_harness": harness, "status": "interactive_started"}


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
    create.add_argument("--artifact", action="append", default=[])
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
        package = build_handoff(goal=args.goal, completed=args.completed, remaining=args.remaining,
                                artifacts=artifacts, source_harness=args.source_harness,
                                target_harness=args.target_harness, model_hint=args.model,
                                session_id=args.session_id, mission_id=args.mission_id,
                                room_id=args.room_id)
        out = store.enqueue(package)
    elif args.command == "inspect":
        out = store.get(args.handoff_id)
    elif args.command == "list":
        out = store.list()
    elif args.command == "resume":
        from herdr_handoff import resume_prompt
        out = {"handoff_id": args.handoff_id, "prompt": resume_prompt(store.get(args.handoff_id))}
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
        out = launch(package, root=store.root, parent_pane=args.parent_pane, cwd=args.cwd)
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
