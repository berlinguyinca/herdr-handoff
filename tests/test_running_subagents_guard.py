"""Takeover must not close a pane while Claude subagents launched from it are still running.

The writer side records one JSON file per running subagent under
`$HERDR_ENGINEERING_STATE/subagents/<sanitised pane id>/`.  These tests use real temp directories, the
real CLI in a subprocess and a real headless herdr server; nothing is stubbed.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from herdr_handoff import running_subagents, sanitize_pane_id, subagent_registry_dir
from test_takeover import create, panes, run_cli, takeover_args

HOUR = 3600


def record(state: Path, pane: str, agent_id: str, *, started: float | None = None, agent_type="general-purpose",
           raw: str | None = None) -> Path:
    folder = state / "subagents" / sanitize_pane_id(pane)
    folder.mkdir(parents=True, exist_ok=True)
    entry = folder / agent_id
    entry.write_text(raw if raw is not None else json.dumps(
        {"agent_id": agent_id, "agent_type": agent_type, "started": started or time.time()}), encoding="utf-8")
    return entry


# --- unit: the registry reader -----------------------------------------------------------------------

def test_pane_ids_are_sanitised_to_the_registry_alphabet():
    assert sanitize_pane_id("w1:p2") == "w1:p2"
    assert sanitize_pane_id("a/b c\\d*é") == "a_b_c_d__"
    assert sanitize_pane_id("../x") == ".._x"


def test_the_registry_root_defaults_and_honours_the_environment_override(tmp_path, monkeypatch):
    monkeypatch.delenv("HERDR_ENGINEERING_STATE", raising=False)
    assert subagent_registry_dir("w1:p1") == (Path.home() / ".local/share/herdr-engineering/state"
                                              / "subagents" / "w1:p1")
    monkeypatch.setenv("HERDR_ENGINEERING_STATE", str(tmp_path))
    assert subagent_registry_dir("w/1") == tmp_path / "subagents" / "w_1"


def test_a_missing_registry_means_no_subagents(tmp_path, monkeypatch):
    monkeypatch.setenv("HERDR_ENGINEERING_STATE", str(tmp_path))
    assert running_subagents("w1:p1") == []


def test_live_entries_are_listed_and_stale_ones_ignored_but_kept(tmp_path, monkeypatch):
    monkeypatch.setenv("HERDR_ENGINEERING_STATE", str(tmp_path))
    record(tmp_path, "w1:p1", "live1", agent_type="Explore")
    stale = record(tmp_path, "w1:p1", "old1", started=time.time() - 7 * HOUR)
    record(tmp_path, "w1:p2", "other-pane")
    found = running_subagents("w1:p1")
    assert [(a["agent_id"], a["agent_type"]) for a in found] == [("live1", "Explore")]
    assert stale.exists()


def test_an_unreadable_entry_falls_back_to_its_mtime(tmp_path, monkeypatch):
    monkeypatch.setenv("HERDR_ENGINEERING_STATE", str(tmp_path))
    fresh = record(tmp_path, "w1:p1", "garbled", raw="{not json")
    old = record(tmp_path, "w1:p1", "garbled-old", raw="{not json")
    os.utime(old, (time.time() - 7 * HOUR,) * 2)
    assert [a["agent_id"] for a in running_subagents("w1:p1")] == ["garbled"]
    assert fresh.exists()


# --- integration: the real CLI -----------------------------------------------------------------------

def start_source(server, cwd):
    full = {**os.environ, **server["env"]}
    ws = json.loads(subprocess.run([server["herdr"], "workspace", "create", "--cwd", str(cwd)], env=full,
                                   capture_output=True, text=True).stdout)
    return ws["result"]["root_pane"]["pane_id"], full


def run_takeover(server, root, package, source, cwd, state, *extra):
    return run_cli(takeover_args(root, package["handoff_id"], source, cwd, *extra),
                   env={**server["env"], "HERDR_PANE_ID": source, "HERDR_ENGINEERING_STATE": str(state)})


@pytest.mark.integration
def test_takeover_refuses_while_a_subagent_is_running_and_touches_nothing(live_herdr_server, tmp_path):
    source, full = start_source(live_herdr_server, tmp_path)
    state, root = tmp_path / "state", tmp_path / "root"
    record(state, source, "agent-abc", agent_type="Explore")
    package = create(root, env=live_herdr_server["env"])
    before = sorted(panes(live_herdr_server["herdr"], full))
    done = run_takeover(live_herdr_server, root, package, source, tmp_path, state)
    assert done.returncode == 1, done.stdout + done.stderr
    out = json.loads(done.stdout)
    assert out["status"] == "subagents_running" and out["source_closed"] is False
    assert out["subagents"] == [{"agent_id": "agent-abc", "agent_type": "Explore",
                                 "started": out["subagents"][0]["started"]}]
    assert "--allow-running-subagents" in out["hint"] and "--keep-source" in out["hint"]
    assert sorted(panes(live_herdr_server["herdr"], full)) == before
    assert json.loads((root / "outbox" / f"{package['handoff_id']}.json").read_text())["state"] == "queued"


@pytest.mark.integration
def test_takeover_proceeds_with_no_registry_and_with_only_a_stale_entry(live_herdr_server, tmp_path):
    source, full = start_source(live_herdr_server, tmp_path)
    state, root = tmp_path / "state", tmp_path / "root"
    record(state, "w9:p9", "someone-else")          # a different pane's entry never counts
    package = create(root, env=live_herdr_server["env"])
    done = run_takeover(live_herdr_server, root, package, source, tmp_path, state)
    assert done.returncode == 0, done.stdout + done.stderr
    assert json.loads(done.stdout)["status"] == "source_closed"

    source2, full = start_source(live_herdr_server, tmp_path)
    record(state, source2, "ancient", started=time.time() - 7 * HOUR)
    package2 = create(tmp_path / "root2", env=live_herdr_server["env"])
    done2 = run_takeover(live_herdr_server, tmp_path / "root2", package2, source2, tmp_path, state)
    assert done2.returncode == 0, done2.stdout + done2.stderr
    assert json.loads(done2.stdout)["status"] == "source_closed"


@pytest.mark.integration
def test_the_override_flag_bypasses_the_subagent_refusal(live_herdr_server, tmp_path):
    source, full = start_source(live_herdr_server, tmp_path)
    state, root = tmp_path / "state", tmp_path / "root"
    record(state, source, "agent-abc")
    package = create(root, env=live_herdr_server["env"])
    done = run_takeover(live_herdr_server, root, package, source, tmp_path, state, "--allow-running-subagents")
    assert done.returncode == 0, done.stdout + done.stderr
    out = json.loads(done.stdout)
    assert out["status"] == "source_closed"
    assert source not in panes(live_herdr_server["herdr"], full)


@pytest.mark.integration
def test_keep_source_skips_the_subagent_refusal(live_herdr_server, tmp_path):
    source, full = start_source(live_herdr_server, tmp_path)
    state, root = tmp_path / "state", tmp_path / "root"
    record(state, source, "agent-abc")
    package = create(root, env=live_herdr_server["env"])
    done = run_takeover(live_herdr_server, root, package, source, tmp_path, state, "--keep-source")
    assert done.returncode == 0, done.stdout + done.stderr
    assert json.loads(done.stdout)["status"] == "confirmed"
    assert source in panes(live_herdr_server["herdr"], full)
