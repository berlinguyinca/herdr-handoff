"""One takeover per handoff, and no lost update to a record, with real processes and a real herdr server."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from herdr_handoff import HandoffStore, build_handoff
from test_takeover import SRC, create, panes, run_cli, source_state

UPDATERS = '''
import sys
sys.path.insert(0, {src!r})
from herdr_handoff import HandoffStore
store, handoff_id, role = HandoffStore(sys.argv[1]), sys.argv[2], sys.argv[3]
for i in range(250):
    if role == "takeover":
        store.record_takeover(handoff_id, {{"reason": "r%d" % i}})
    else:
        store.link_successor(handoff_id, "handoff_%032x" % i)
'''


def _workspace(live, tmp_path):
    full = {**os.environ, **live["env"]}
    ws = json.loads(subprocess.run([live["herdr"], "workspace", "create", "--cwd", str(tmp_path)], env=full,
                                   capture_output=True, text=True).stdout)
    return ws["result"]["root_pane"]["pane_id"], full


def test_two_processes_updating_one_record_lose_no_update(tmp_path):
    store = HandoffStore(tmp_path / "root")
    package = store.enqueue(build_handoff(goal="g"))
    handoff_id = package["handoff_id"]
    store.record_takeover(handoff_id, {"phase": "launching", "source_pane": "w1:p1"})
    script = tmp_path / "updaters.py"
    script.write_text(UPDATERS.format(src=SRC))
    procs = [subprocess.Popen([sys.executable, str(script), str(tmp_path / "root"), handoff_id, role])
             for role in ("takeover", "link")]
    assert [p.wait(timeout=120) for p in procs] == [0, 0]
    final = store.get(handoff_id)
    assert final["takeover"]["reason"] == "r249" and final["successor_id"] == "handoff_%032x" % 249


def test_a_failed_write_leaves_no_temp_file_and_the_old_record(tmp_path):
    store = HandoffStore(tmp_path / "root")
    handoff_id = store.enqueue(build_handoff(goal="g"))["handoff_id"]
    before = store.get(handoff_id)
    with pytest.raises(TypeError):
        store._write(store.outbox / f"{handoff_id}.json", {"bad": object()})
    assert store.get(handoff_id) == before
    assert not [p for p in store.outbox.iterdir() if p.suffix == ".tmp" or p.name.startswith(".")]


def test_a_missing_or_corrupt_predecessor_is_reported_and_the_successor_stays_valid(tmp_path):
    root = tmp_path / "root"
    missing = "handoff_" + "c" * 32
    done = run_cli(["--root", str(root), "create", "--goal", "Next", "--source-harness", "pi",
                    "--predecessor-id", missing])
    assert done.returncode == 0 and "Traceback" not in done.stderr, done.stderr
    out = json.loads(done.stdout)
    assert missing in done.stderr and any(missing in w for w in out["warnings"])
    assert source_state(root, out["handoff_id"])["predecessor_id"] == missing
    corrupt = "handoff_" + "d" * 32
    (root / "outbox" / f"{corrupt}.json").write_text("{not json")
    done = run_cli(["--root", str(root), "create", "--goal", "Next", "--source-harness", "pi",
                    "--predecessor-id", corrupt])
    assert done.returncode == 0 and "Traceback" not in done.stderr and corrupt in done.stderr
    assert json.loads(done.stdout)["warnings"]
    assert (root / "outbox" / f"{corrupt}.json").read_text() == "{not json"


def test_confirm_before_a_child_pane_is_recorded_is_refused_cleanly(tmp_path):
    root = tmp_path / "root"
    package = create(root)
    done = run_cli(["--root", str(root), "confirm", package["handoff_id"], "--pane", "w1:p2"])
    assert done.returncode == 1 and "Traceback" not in done.stderr, done.stderr
    assert "no successor pane" in done.stderr
    assert "takeover" not in source_state(root, package["handoff_id"])


def test_the_help_and_docs_list_the_new_refusals():
    help_text = run_cli(["takeover", "--help"]).stdout
    for needle in ("takeover_in_progress", "launch_state_unknown", "--relaunch"):
        assert needle in help_text
    readme = (Path(__file__).parents[1] / "README.md").read_text()
    assert "takeover_in_progress" in readme and "launch_state_unknown" in readme


@pytest.mark.integration
def test_a_second_concurrent_takeover_of_one_handoff_refuses_and_opens_no_second_pane(herdr_server, tmp_path):
    # the recorder harnesses never become live agents, so the first takeover holds its lock for the whole
    # confirm timeout, long enough for the second to give up waiting
    env, herdr = herdr_server["env"], herdr_server["herdr"]
    full = {**os.environ, **env}
    ws = json.loads(subprocess.run([herdr, "workspace", "create", "--cwd", str(tmp_path)], env=full,
                                   capture_output=True, text=True).stdout)
    source = ws["result"]["root_pane"]["pane_id"]
    root = tmp_path / "root"
    handoff_id = create(root, env=env)["handoff_id"]
    base = {**{k: v for k, v in os.environ.items() if not k.startswith("HERDR_")}, **env,
            "PYTHONPATH": SRC, "HERDR_PANE_ID": source}
    args = [sys.executable, "-m", "herdr_handoff_cli", "--root", str(root), "takeover", handoff_id,
            "--parent-pane", source, "--cwd", str(tmp_path), "--confirm-timeout", "14"]
    first = subprocess.Popen(args, env=base, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            if source_state(root, handoff_id).get("takeover", {}).get("child_pane"):
                break
        except (OSError, ValueError):
            pass
        time.sleep(0.2)
    second = subprocess.run(args, env=base, capture_output=True, text=True, timeout=120)
    out_first, _ = first.communicate(timeout=120)
    assert second.returncode == 1, second.stdout + second.stderr
    assert json.loads(second.stdout)["status"] == "takeover_in_progress"
    assert json.loads(out_first)["status"] == "launch_unconfirmed"
    assert len(panes(herdr, full)) == 2          # the source and exactly one successor


@pytest.mark.integration
def test_a_takeover_that_died_between_placing_and_recording_the_pane_needs_relaunch(live_herdr_server, tmp_path):
    env, herdr = live_herdr_server["env"], live_herdr_server["herdr"]
    source, full = _workspace(live_herdr_server, tmp_path)
    root = tmp_path / "root"
    handoff_id = create(root, env=env)["handoff_id"]
    HandoffStore(root).record_takeover(handoff_id, {"phase": "launching", "source_pane": source})
    args = ["--root", str(root), "takeover", handoff_id, "--parent-pane", source, "--cwd", str(tmp_path),
            "--keep-source", "--confirm-timeout", "30"]
    before = sorted(panes(herdr, full))
    refused = run_cli(args, env={**env, "HERDR_PANE_ID": source})
    assert refused.returncode == 1, refused.stdout + refused.stderr
    assert json.loads(refused.stdout)["status"] == "launch_state_unknown"
    assert sorted(panes(herdr, full)) == before
    done = run_cli([*args, "--relaunch"], env={**env, "HERDR_PANE_ID": source})
    assert done.returncode == 0, done.stdout + done.stderr
    assert json.loads(done.stdout)["status"] == "confirmed" and len(panes(herdr, full)) == len(before) + 1
