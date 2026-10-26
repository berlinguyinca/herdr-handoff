"""Portable, private-by-default handoff packages and offline outbox.

The module intentionally has no network or harness dependency.  A Herdr
adapter can sync the JSON envelope later, while a local agent can always
create, inspect, and pass the package to another harness.
"""

from __future__ import annotations

import json
import re
import urllib.request
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

__version__ = "0.1.0"

_SECRET = re.compile(
    r"(?i)(bearer\s+|token\s*[=:]\s*|password\s*[=:]\s*|secret\s*[=:]\s*|api[_-]?key\s*[=:]\s*)[^\s,;]+"
)
_STATES = {
    "draft": {"queued", "cancelled"},
    "queued": {"claimed", "cancelled", "expired", "failed"},
    "claimed": {"accepted", "rejected", "queued", "cancelled", "expired", "failed"},
    "accepted": {"in_progress", "rejected", "cancelled", "failed"},
    "in_progress": {"completed", "failed", "cancelled"},
    "completed": set(),
    "rejected": set(),
    "cancelled": set(),
    "expired": set(),
    "failed": {"queued", "cancelled"},
}


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _clean(value):
    if isinstance(value, str):
        return _SECRET.sub(r"\1[REDACTED]", value)[:12000]
    if isinstance(value, list):
        return [_clean(item) for item in value[:200]]
    if isinstance(value, dict):
        return {str(key)[:120]: _clean(item) for key, item in list(value.items())[:200]}
    return value


def build_handoff(*, goal, completed=None, remaining=None, decisions=None, risks=None,
                  validation=None, provenance=None, artifacts=None, source_harness=None,
                  target_harness=None, model_hint=None, session_id=None, mission_id=None,
                  room_id=None, source_agent_id=None, target_agent_id=None, handoff_id=None):
    """Create a bounded, redacted handoff envelope suitable for any harness."""
    if not isinstance(goal, str) or not goal.strip():
        raise ValueError("goal is required")
    if source_harness not in {"pi", "claude", "codex", "opencode", None}:
        raise ValueError("unsupported source harness")
    if target_harness not in {"pi", "claude", "codex", "opencode", None}:
        raise ValueError("unsupported target harness")
    raw = {
        "handoff_id": handoff_id or "handoff_" + uuid4().hex,
        "schema_version": 1,
        "created_at": _now(),
        "updated_at": _now(),
        "state": "draft",
        "source": {"harness": source_harness, "agent_id": source_agent_id},
        "target": {"harness": target_harness, "agent_id": target_agent_id,
                    "model_hint": model_hint},
        "context": {
            "goal": goal, "completed": completed or [], "remaining": remaining or [],
            "decisions": decisions or [], "risks": risks or [], "validation": validation or [],
        },
        "provenance": provenance or {},
        "session_id": session_id, "mission_id": mission_id, "room_id": room_id,
        "artifacts": artifacts or [],
        "history": [],
    }
    package = _clean(raw)
    package["redaction"] = {"redacted": package != raw, "policy": "herdr-handoff-v1"}
    return package


def resume_prompt(package):
    """Return the bounded prompt given to a receiving harness."""
    context = package.get("context") or {}
    return "\n".join([
        "Resume this private HerdR handoff.",
        f"Goal: {context.get('goal', '')}",
        "Completed:\n- " + "\n- ".join(context.get("completed") or ["(none recorded)"]),
        "Remaining:\n- " + "\n- ".join(context.get("remaining") or ["(none recorded)"]),
        "Decisions:\n- " + "\n- ".join(context.get("decisions") or ["(none recorded)"]),
        f"Pinned artifacts: {json.dumps(package.get('artifacts') or [], sort_keys=True)}",
        "Do not publish or expose private infrastructure without explicit approval.",
    ])


class HandoffStore:
    """Filesystem-backed outbox with atomic writes and state transition checks."""

    def __init__(self, root):
        self.root = Path(root).expanduser()
        self.outbox = self.root / "outbox"
        self.outbox.mkdir(parents=True, exist_ok=True)

    def _path(self, handoff_id):
        if not re.fullmatch(r"handoff_[a-f0-9]{32}", handoff_id):
            raise ValueError("invalid handoff_id")
        return self.outbox / f"{handoff_id}.json"

    def get(self, handoff_id):
        path = self._path(handoff_id)
        if not path.is_file():
            raise KeyError(handoff_id)
        return json.loads(path.read_text(encoding="utf-8"))

    def enqueue(self, package):
        package = deepcopy(package)
        handoff_id = package.get("handoff_id")
        if not handoff_id:
            raise ValueError("handoff_id is required")
        path = self._path(handoff_id)
        if path.is_file():
            return self.get(handoff_id)
        package["state"] = "queued"
        package["updated_at"] = _now()
        package["history"] = [{"state": "queued", "actor": "local", "at": package["updated_at"]}]
        self._write(path, package)
        return package

    def transition(self, handoff_id, state, *, actor):
        package = self.get(handoff_id)
        old = package["state"]
        if state not in _STATES.get(old, set()):
            raise ValueError(f"invalid transition: {old} -> {state}")
        package["state"] = state
        package["updated_at"] = _now()
        package.setdefault("history", []).append({"state": state, "actor": actor, "at": package["updated_at"]})
        self._write(self._path(handoff_id), package)
        return package

    def list(self):
        return [self.get(path.stem) for path in sorted(self.outbox.glob("handoff_*.json"))]

    def sync(self, base_url, *, token=None, handoff_id=None):
        """Upload queued packages to a Herdr API; local state is unchanged on failure."""
        packages = [self.get(handoff_id)] if handoff_id else self.list()
        result = []
        for package in packages:
            request = urllib.request.Request(
                base_url.rstrip("/") + "/api/v1/handoffs", data=json.dumps(package).encode(),
                method="POST", headers={"Content-Type": "application/json", "Accept": "application/json"})
            if token:
                request.add_header("X-API-Key", token)
            with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310
                result.append(json.loads(response.read()))
        return result

    @staticmethod
    def _write(path, package):
        temp = path.with_suffix(".json.tmp")
        temp.write_text(json.dumps(package, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temp.replace(path)

