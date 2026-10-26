#!/usr/bin/env python3
"""Small JSON CLI used by all four harness skills."""

from __future__ import annotations

import argparse
import json
import os
import sys

from herdr_handoff import HandoffStore, build_handoff


def main(argv=None):
    parser = argparse.ArgumentParser(prog="herdr-handoff")
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
    listing = sub.add_parser("list")
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
    else:
        out = store.transition(args.handoff_id, args.state, actor=args.actor)
    print(json.dumps(out, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
