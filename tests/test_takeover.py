"""Takeover: move a live session to a successor pane in the SAME herdr tab, then close the source.

Two layers, no mocks:
  * unit  - the pure safety decision (may the source pane be closed?) and prompt/plan building;
  * integration - a real headless herdr server with real panes and real harness executables,
    proving the close happens only on observed liveness, and that repeat triggers do nothing twice.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import wait_for_json
from herdr_handoff import (TAKEOVER_PHASES, HandoffStore, build_handoff, build_launch_plan,
                           decide_takeover_close, short_prompt, takeover_prompt, validate_takeover,
                           _handoff_root)

ID = "handoff_" + "a" * 32

SRC = str(Path(__file__).parents[1] / "src")


def run_cli(args, *, env=None, cwd=None):
    base = {k: v for k, v in os.environ.items() if not k.startswith("HERDR_")}
    base["PYTHONPATH"] = SRC
    base.update(env or {})
    return subprocess.run([sys.executable, "-m", "herdr_handoff_cli", *args], env=base, cwd=cwd,
                          capture_output=True, text=True, timeout=180)


def create(root, *, env=None, target="pi"):
    done = run_cli(["--root", str(root), "create", "--goal", "Continue the session", "--source-harness", "pi",
                    "--target-harness", target], env=env)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


# --- unit: the safety decision -----------------------------------------------------------------------

def test_close_is_refused_until_the_successor_is_confirmed():
    decision = decide_takeover_close(placement="split", same_tab=True, source_pane="w1:p1",
                                     child_pane="w1:p2", confirmed=False)
    assert decision["close"] is False and decision["reason"] == "unconfirmed"


def test_close_is_refused_when_the_successor_is_not_in_the_source_tab():
    # A tab/workspace placement means the split failed: the source tab is not being taken over.
    for placement in ("tab", "workspace"):
        decision = decide_takeover_close(placement=placement, same_tab=True, source_pane="w1:p1",
                                         child_pane="w1:p2", confirmed=True)
        assert decision["close"] is False and decision["reason"] == "not_same_tab"


def test_close_is_refused_without_a_source_pane_or_when_it_would_close_the_successor():
    assert decide_takeover_close(placement="split", same_tab=True, source_pane=None,
                                 child_pane="w1:p2", confirmed=True) == {"close": False,
                                                                         "reason": "no_source_pane"}
    assert decide_takeover_close(placement="split", same_tab=True, source_pane="w1:p2",
                                 child_pane="w1:p2", confirmed=True) == {"close": False,
                                                                         "reason": "same_pane"}


def test_close_is_allowed_only_for_a_confirmed_successor_in_the_source_tab():
    assert decide_takeover_close(placement="split", same_tab=True, source_pane="w1:p1",
                                 child_pane="w1:p2", confirmed=True) == {"close": True, "reason": ""}


def test_takeover_prompt_points_at_the_document_and_stays_a_single_line():
    prompt = takeover_prompt("handoff_" + "a" * 32, "/abs/handoffs/documents/handoff_" + "a" * 32,
                             root="/abs/handoffs")
    assert "\n" not in prompt and ("/abs/handoffs/documents/handoff_" + "a" * 32) in prompt
    assert "confirm" in prompt and "--root /abs/handoffs" in prompt
    assert len(prompt) < 1000


def test_the_takeover_prompt_drops_the_root_when_the_pane_already_has_it(tmp_path):
    # Every byte of a typed command counts against what herdr can deliver
    # (herdr_handoff.MAX_TYPED_COMMAND), and the successor can rebuild the root from the file it is
    # exported, so the short form is the one it is handed.
    prompt = takeover_prompt(ID, tmp_path / "documents" / f"{ID}.md")
    assert "--root" not in prompt
    assert f"`herdr-handoff confirm {ID}`" in prompt
    assert prompt == prompt.strip() and prompt.count("\n") == 0


def test_the_successor_rebuilds_the_root_from_the_variables_the_pane_is_exported(tmp_path):
    # `herdr-handoff confirm <id>` carries no path, so a takeover depends entirely on the variables the
    # launch command exports and on deriving the root back out of the file path.  A third variable would
    # cost typed bytes; a renamed one would leave the successor unable to release the pane it replaced.
    handoff_file = str(tmp_path / "handoffs" / "outbox" / f"{ID}.json")
    plan = build_launch_plan("w1:p1", "pi", ID, handoff_file,
                             prompt=takeover_prompt(ID, tmp_path / "documents" / f"{ID}.md"))
    typed = plan["run_command"].split()
    assert typed[0] == "env"
    exported = dict(word.split("=", 1) for word in typed[1:3])
    assert sorted(exported) == ["HERDR_HANDOFF_FILE", "HERDR_HANDOFF_ID"]
    assert exported["HERDR_HANDOFF_ID"] == ID
    assert _handoff_root(exported["HERDR_HANDOFF_FILE"]) == str(tmp_path / "handoffs")
    # an unusual layout derives no root rather than a wrong one, and the CLI falls back to HERDR_HANDOFF_DIR
    assert _handoff_root("/somewhere/else/package.json") is None


def test_the_document_spells_out_the_confirm_command_with_the_real_root(tmp_path):
    store = HandoffStore(tmp_path / "handoffs")
    package = build_handoff(goal="Goal", remaining=["r"])
    package["handoff_id"] = ID
    document = store.write_document(package).read_text()
    assert f"herdr-handoff --root {tmp_path / 'handoffs'} confirm {ID}" in document
    assert "stays open on purpose" in document


@pytest.mark.integration
def test_a_handoff_with_no_target_harness_refuses_without_crashing_or_a_pane(live_herdr_server, tmp_path):
    # `create` without --target-harness leaves the target unset; takeover must say so plainly, leave the
    # source pane alone, and never print a traceback at the operator.
    env, herdr = live_herdr_server["env"], live_herdr_server["herdr"]
    full = {**os.environ, **env}
    ws = json.loads(subprocess.run([herdr, "workspace", "create", "--cwd", str(tmp_path)], env=full,
                                   capture_output=True, text=True).stdout)
    source = ws["result"]["root_pane"]["pane_id"]
    root = tmp_path / "root"
    created = run_cli(["--root", str(root), "create", "--goal", "No target named"], env=env)
    assert created.returncode == 0, created.stderr
    done = run_cli(["--root", str(root), "takeover", json.loads(created.stdout)["handoff_id"],
                    "--parent-pane", source, "--confirm-timeout", "5"], env={**env, "HERDR_PANE_ID": source})
    assert done.returncode != 0
    assert "unsupported target harness" in done.stderr and "Traceback" not in done.stderr
    panes = json.loads(subprocess.run([herdr, "pane", "list"], env=full, capture_output=True,
                                      text=True).stdout)["result"]["panes"]
    assert [p["pane_id"] for p in panes] == [source]


def test_a_recorded_takeover_satisfies_the_published_json_schema(tmp_path):
    # the block is the only record of a destructive move, so the schema shipped for dashboards and the
    # validator that guards the close must not be allowed to drift apart.
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((Path(__file__).parents[1] / "schemas" / "handoff-v2.schema.json").read_text())
    store = HandoffStore(tmp_path / "handoffs")
    package = build_handoff(goal="Goal", remaining=["r"])
    package["handoff_id"] = ID
    store.enqueue(package)
    store.record_takeover(ID, {"phase": "source_closed", "source_pane": "w1:p1", "child_pane": "w1:p2",
                              "tab_id": "w1:t1", "placement": "split",
                              "evidence": {"agent_state": "idle"}})
    stored = store.get(ID)
    jsonschema.validate(stored, schema)
    assert validate_takeover(stored["takeover"]) == stored["takeover"]

    broken = dict(stored)
    broken["takeover"] = dict(stored["takeover"], phase="closed_by_hand")
    with pytest.raises(ValueError, match="takeover phase"):
        validate_takeover(broken["takeover"])
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(broken, schema)
    smuggled = dict(stored)
    smuggled["takeover"] = dict(stored["takeover"], closed_pane="w1:p9")
    with pytest.raises(ValueError, match="unsupported keys"):
        validate_takeover(smuggled["takeover"])
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(smuggled, schema)


def test_takeover_block_is_validated():
    good = {"version": 1, "phase": "source_closed", "source_pane": "w1:p1", "child_pane": "w1:p2",
            "tab_id": "w1:t1", "confirmed_at": "2026-11-13T03:00:00Z", "source_closed_at": "2026-11-13T03:00:05Z",
            "reason": None, "evidence": {"agent": "pi", "matched_line": "handoff_aaa"}}
    assert validate_takeover(good)["phase"] == "source_closed"
    for bad in ({"phase": "nope"}, {"phase": "confirmed", "child_pane": "not a pane id"},
                {"phase": "confirmed", "extra": 1}, "string", {"phase": "confirmed", "version": 2}):
        with pytest.raises(ValueError):
            validate_takeover(bad)
    assert validate_takeover(None) is None
    assert {"launching", "launched", "confirmed", "source_closed", "unconfirmed",
            "launch_failed", "already_closed"} == TAKEOVER_PHASES


def test_launch_plan_can_be_built_for_a_takeover_without_changing_a_plain_launch():
    plain = build_launch_plan("w1:p1", "pi", "handoff_" + "b" * 32, "/abs/outbox/x.json")
    assert plain["split"][:2] == ["herdr", "pane"] and "close" not in plain
    assert short_prompt("handoff_" + "b" * 32, "/abs/d.md").startswith("Resume HerdR handoff handoff_b")


# --- integration: a real herdr server ----------------------------------------------------------------

def panes(herdr, env):
    done = subprocess.run([herdr, "pane", "list"], env=env, capture_output=True, text=True, check=True)
    return [p["pane_id"] for p in json.loads(done.stdout)["result"]["panes"]]


def source_state(root, handoff_id):
    return json.loads((root / "outbox" / f"{handoff_id}.json").read_text())


@pytest.mark.integration
def test_takeover_closes_the_source_pane_only_after_herdr_reports_the_successor_live(live_herdr_server, tmp_path):
    env, herdr = live_herdr_server["env"], live_herdr_server["herdr"]
    full = {**os.environ, **env}
    ws = json.loads(subprocess.run([herdr, "workspace", "create", "--cwd", str(tmp_path)], env=full,
                                   capture_output=True, text=True).stdout)
    source = ws["result"]["root_pane"]["pane_id"]
    root = tmp_path / "root"
    package = create(root, env=env)
    done = run_cli(["--root", str(root), "takeover", package["handoff_id"], "--parent-pane", source,
                    "--cwd", str(tmp_path), "--confirm-timeout", "30"],
                   env={**env, "HERDR_PANE_ID": source})
    assert done.returncode == 0, done.stdout + done.stderr
    out = json.loads(done.stdout)
    assert out["status"] == "source_closed" and out["placement"] == "split"
    assert out["pane_id"] != source and out["source_pane"] == source
    # The successor lives in the source's own tab, and the source pane is gone.
    assert out["tab_id"] == ws["result"]["root_pane"]["tab_id"]
    assert source not in panes(herdr, full) and out["pane_id"] in panes(herdr, full)
    stored = source_state(root, package["handoff_id"])
    assert stored["takeover"]["phase"] == "source_closed"
    assert stored["takeover"]["confirmed_at"] and stored["takeover"]["source_closed_at"]
    assert stored["state"] == "in_progress"
    # The document the successor still has to read survives the close of the pane that made it.
    assert Path(out["document"]).is_file() and package["handoff_id"] in Path(out["document"]).read_text()
    assert wait_for_json(live_herdr_server["probe"] / "pi.argv.json")["handoff_id"] == package["handoff_id"]


@pytest.mark.integration
def test_takeover_refuses_to_close_an_unconfirmed_successor(herdr_server, tmp_path):
    # The recorder harnesses exit at once, so herdr never reports a live successor agent: the
    # destructive step must not happen.
    env, herdr = herdr_server["env"], herdr_server["herdr"]
    full = {**os.environ, **env}
    ws = json.loads(subprocess.run([herdr, "workspace", "create", "--cwd", str(tmp_path)], env=full,
                                   capture_output=True, text=True).stdout)
    source = ws["result"]["root_pane"]["pane_id"]
    root = tmp_path / "root"
    package = create(root, env=env)
    done = run_cli(["--root", str(root), "takeover", package["handoff_id"], "--parent-pane", source,
                    "--cwd", str(tmp_path), "--confirm-timeout", "3"],
                   env={**env, "HERDR_PANE_ID": source})
    assert done.returncode == 1, done.stderr
    out = json.loads(done.stdout)
    assert out["status"] == "launch_unconfirmed" and out["source_closed"] is False
    assert out["reason"].startswith("unconfirmed") and source in panes(herdr, full)
    assert out["evidence"]["agent"] is None          # herdr never saw a live agent in the successor pane
    assert source_state(root, package["handoff_id"])["takeover"]["phase"] == "unconfirmed"


@pytest.mark.integration
def test_takeover_repeat_trigger_never_opens_a_second_pane_or_closes_twice(live_herdr_server, tmp_path):
    env, herdr = live_herdr_server["env"], live_herdr_server["herdr"]
    full = {**os.environ, **env}
    ws = json.loads(subprocess.run([herdr, "workspace", "create", "--cwd", str(tmp_path)], env=full,
                                   capture_output=True, text=True).stdout)
    source = ws["result"]["root_pane"]["pane_id"]
    root = tmp_path / "root"
    package = create(root, env=env)
    args = ["--root", str(root), "takeover", package["handoff_id"], "--parent-pane", source,
            "--cwd", str(tmp_path), "--confirm-timeout", "30"]
    first = json.loads(run_cli(args, env={**env, "HERDR_PANE_ID": source}).stdout)
    after_first = sorted(panes(herdr, full))
    second = run_cli(args, env={**env, "HERDR_PANE_ID": source})
    assert second.returncode == 0, second.stderr
    out = json.loads(second.stdout)
    assert out["status"] == "source_closed" and out["idempotent"] is True
    assert out["pane_id"] == first["pane_id"] and out["source_closed_at"] == first["source_closed_at"]
    assert sorted(panes(herdr, full)) == after_first  # no second successor pane, nothing re-closed


@pytest.mark.integration
def test_takeover_finishes_a_record_the_launcher_left_confirmed_but_unclosed(live_herdr_server, tmp_path):
    # Crash-shaped case: the successor confirmed, the close never happened. Driving the record
    # again must close the source and must not start another pane.
    env, herdr = live_herdr_server["env"], live_herdr_server["herdr"]
    full = {**os.environ, **env}
    ws = json.loads(subprocess.run([herdr, "workspace", "create", "--cwd", str(tmp_path)], env=full,
                                   capture_output=True, text=True).stdout)
    source = ws["result"]["root_pane"]["pane_id"]
    root = tmp_path / "root"
    package = create(root, env=env)
    done = run_cli(["--root", str(root), "takeover", package["handoff_id"], "--parent-pane", source,
                    "--cwd", str(tmp_path), "--keep-source", "--confirm-timeout", "30"],
                   env={**env, "HERDR_PANE_ID": source})
    assert done.returncode == 0, done.stdout + done.stderr
    out = json.loads(done.stdout)
    assert out["status"] == "confirmed" and out["source_closed"] is False and source in panes(herdr, full)
    child = out["pane_id"]
    finish = run_cli(["--root", str(root), "takeover", package["handoff_id"], "--parent-pane", source,
                      "--cwd", str(tmp_path), "--confirm-timeout", "30"],
                     env={**env, "HERDR_PANE_ID": source})
    assert finish.returncode == 0, finish.stdout + finish.stderr
    out2 = json.loads(finish.stdout)
    assert out2["status"] == "source_closed" and out2["pane_id"] == child
    assert source not in panes(herdr, full) and child in panes(herdr, full)


@pytest.mark.integration
def test_takeover_without_a_source_pane_identity_creates_nothing_and_says_why(herdr_server, tmp_path):
    env, herdr = herdr_server["env"], herdr_server["herdr"]
    full = {**os.environ, **env}
    subprocess.run([herdr, "workspace", "create", "--cwd", str(tmp_path)], env=full, capture_output=True)
    before = panes(herdr, full)
    root = tmp_path / "root"
    package = create(root, env=env)
    done = run_cli(["--root", str(root), "takeover", package["handoff_id"], "--cwd", str(tmp_path)], env=env)
    assert done.returncode != 0 and "source pane" in (done.stderr + done.stdout)
    assert panes(herdr, full) == before
    assert source_state(root, package["handoff_id"])["state"] == "queued"


@pytest.mark.integration
def test_takeover_without_herdr_keeps_the_manual_launch_path(herdr_server, tmp_path):
    env = {k: herdr_server["env"][k] for k in ("HOME", "HERDR_SOCKET_PATH")}
    env["PATH"] = str(tmp_path / "empty-bin")  # real herdr exists on this host; remove it from PATH
    (tmp_path / "empty-bin").mkdir()
    root = tmp_path / "root"
    package = create(root)
    done = run_cli(["--root", str(root), "takeover", package["handoff_id"], "--parent-pane", "w1:p1"], env=env)
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    assert out["status"] == "manual_launch_required" and "herdr" in out["reason"]
    assert out["command"].startswith("env HERDR_HANDOFF_ID=")
    assert source_state(root, package["handoff_id"])["state"] == "queued"


@pytest.mark.integration
def test_confirm_stamps_the_successor_pane_from_its_own_environment(live_herdr_server, tmp_path):
    env, herdr = live_herdr_server["env"], live_herdr_server["herdr"]
    full = {**os.environ, **env}
    ws = json.loads(subprocess.run([herdr, "workspace", "create", "--cwd", str(tmp_path)], env=full,
                                   capture_output=True, text=True).stdout)
    source = ws["result"]["root_pane"]["pane_id"]
    root = tmp_path / "root"
    package = create(root, env=env)
    started = run_cli(["--root", str(root), "takeover", package["handoff_id"], "--parent-pane", source,
                       "--cwd", str(tmp_path), "--keep-source", "--confirm-timeout", "30"],
                      env={**env, "HERDR_PANE_ID": source})
    child = json.loads(started.stdout)["pane_id"]
    done = run_cli(["--root", str(root), "confirm", package["handoff_id"]], env={**env, "HERDR_PANE_ID": child})
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    assert out["phase"] == "confirmed" and out["pane_id"] == child
    assert source_state(root, package["handoff_id"])["takeover"]["confirmed_at"]
