"""`reap` closes the panes confirmed handoffs replaced, and `confirm` obeys the same live-pane guards.

No mocks: the server is a real headless herdr, panes are real shells, records are real files.  The pure
guard is a unit test; everything that closes (or refuses to close) a pane runs the real CLI.
"""
from __future__ import annotations

import json

import pytest

from conftest import pushed_repo
from herdr_handoff import HandoffStore, live_pane_refusal, validate_takeover
from test_confirm_close import confirm, edit_record, start
from test_source_worktree_guard import local_only_commit
from test_takeover import panes, run_cli, source_state

SOURCE, CHILD = "w1:p1", "w1:p2"
SEEN = {"terminal_id": "term_a", "agent_status": "idle"}


def refusal(**over):
    args = {"source_pane": SOURCE, "source_view": SEEN, "child_view": {"terminal_id": "term_b"},
            "caller_pane": CHILD, "recorded_terminal": "term_a", **over}
    return live_pane_refusal(**args)


# --- unit: the live-pane guard ------------------------------------------------------------------------

def test_a_quiet_source_that_is_the_one_recorded_may_be_closed():
    assert refusal() == ""
    assert refusal(recorded_terminal=None, caller_pane=None) == ""     # a record that never stored an identity


def test_the_pane_the_calling_process_runs_in_is_never_closed():
    assert refusal(caller_pane=SOURCE) == "source_is_caller"


def test_a_source_that_is_still_working_is_never_closed():
    assert refusal(source_view={**SEEN, "agent_status": "working"}) == "source_working"


def test_a_source_pane_whose_terminal_changed_is_a_reused_pane():
    assert refusal(source_view={**SEEN, "terminal_id": "term_other"}) == "source_pane_reused"


def test_a_successor_that_is_gone_leaves_the_source_as_the_only_live_session():
    assert refusal(child_view=None) == "successor_gone"


def test_the_close_options_accept_a_recorded_terminal(tmp_path):
    block = {"version": 1, "phase": "confirmed", "close_options": {"source_terminal_id": "term_a"}}
    assert validate_takeover(block) == block
    with pytest.raises(ValueError, match="source_terminal_id"):
        validate_takeover({**block, "close_options": {"source_terminal_id": 7}})


# --- integration: a real herdr server -----------------------------------------------------------------

def reap(s, *flags, env=None):
    return run_cli(["--root", str(s["root"]), "reap", *flags], env={**s["env"], **(env or {})})


def row(done, s):
    out = json.loads(done.stdout)
    return out, next(r for r in out["results"] if r["handoff_id"] == s["id"])


def confirmed_but_open(herdr_server, tmp_path, *extra, cwd=None):
    """A handoff whose successor confirmed while a takeover still held the lock, so confirm closed nothing."""
    s = start(herdr_server, cwd or tmp_path, tmp_path, *extra)
    with HandoffStore(s["root"]).takeover_lock(s["id"]):
        assert json.loads(confirm(s).stdout)["close"] == "not_attempted"
    assert s["source"] in panes(s["herdr"], s["full"])
    return s


@pytest.mark.integration
def test_takeover_records_the_identity_of_the_pane_it_replaces(herdr_server, tmp_path):
    s = start(herdr_server, tmp_path, tmp_path)
    recorded = source_state(s["root"], s["id"])["takeover"]["close_options"]
    assert recorded["source_terminal_id"].startswith("term_")


@pytest.mark.integration
def test_reap_is_a_dry_run_unless_told_to_apply(herdr_server, tmp_path):
    s = confirmed_but_open(herdr_server, tmp_path)
    done = reap(s)
    assert done.returncode == 0, done.stderr
    out, found = row(done, s)
    assert out["mode"] == "dry_run"
    assert found["action"] == "would_close" and found["source_pane"] == s["source"]
    assert found["successor_pane"] == s["child"]
    assert s["source"] in panes(s["herdr"], s["full"])
    assert source_state(s["root"], s["id"])["takeover"]["phase"] == "confirmed"


@pytest.mark.integration
def test_reap_apply_closes_the_replaced_pane_and_records_it(herdr_server, tmp_path):
    s = confirmed_but_open(herdr_server, tmp_path)
    out, found = row(reap(s, "--apply"), s)
    assert out["mode"] == "apply" and found["action"] == "closed"
    assert s["source"] not in panes(s["herdr"], s["full"]) and s["child"] in panes(s["herdr"], s["full"])
    block = source_state(s["root"], s["id"])["takeover"]
    assert block["phase"] == "source_closed" and block["source_closed_at"]
    again = json.loads(reap(s, "--apply").stdout)       # a closed record is not listed again
    assert s["id"] not in [r["handoff_id"] for r in again["results"]]


@pytest.mark.integration
def test_reap_skips_a_handoff_nobody_confirmed(herdr_server, tmp_path):
    s = start(herdr_server, tmp_path, tmp_path)
    out = json.loads(reap(s, "--apply").stdout)
    assert s["id"] not in [r["handoff_id"] for r in out["results"]]
    assert s["source"] in panes(s["herdr"], s["full"])


@pytest.mark.integration
def test_reap_respects_keep_source(herdr_server, tmp_path):
    s = start(herdr_server, tmp_path, tmp_path, "--keep-source")
    assert json.loads(confirm(s).stdout)["close"] == "not_attempted"
    _, found = row(reap(s, "--apply"), s)
    assert found["action"] == "skipped" and "keep-source" in found["reason"]
    assert s["source"] in panes(s["herdr"], s["full"])


@pytest.mark.integration
def test_reap_leaves_a_record_without_close_options_unless_asked(herdr_server, tmp_path):
    s = confirmed_but_open(herdr_server, tmp_path)
    edit_record(s, lambda block: block.pop("close_options"))
    _, found = row(reap(s, "--apply"), s)
    assert found["action"] == "skipped" and "--include-unrecorded" in found["reason"]
    assert s["source"] in panes(s["herdr"], s["full"])
    _, found = row(reap(s, "--apply", "--include-unrecorded"), s)
    assert found["action"] == "closed"
    assert s["source"] not in panes(s["herdr"], s["full"])


@pytest.mark.integration
def test_reap_applies_the_worktree_guard_and_records_the_refusal(herdr_server, tmp_path):
    repo = pushed_repo(tmp_path)
    local_only_commit(repo)
    s = confirmed_but_open(herdr_server, tmp_path, cwd=repo)
    _, found = row(reap(s, "--apply"), s)
    assert found["action"] == "skipped" and "worktree_unpushed" in found["reason"]
    assert s["source"] in panes(s["herdr"], s["full"])
    assert "worktree_unpushed" in source_state(s["root"], s["id"])["takeover"]["reason"]


@pytest.mark.integration
def test_reap_never_closes_a_pane_that_was_reused_since_the_handoff(herdr_server, tmp_path):
    s = confirmed_but_open(herdr_server, tmp_path)
    edit_record(s, lambda block: block["close_options"].update(source_terminal_id="term_somebody_else"))
    _, found = row(reap(s, "--apply"), s)
    assert found["action"] == "skipped" and "source_pane_reused" in found["reason"]
    assert s["source"] in panes(s["herdr"], s["full"])


@pytest.mark.integration
def test_reap_never_closes_the_pane_it_is_run_from(herdr_server, tmp_path):
    s = confirmed_but_open(herdr_server, tmp_path)
    _, found = row(reap(s, "--apply", env={"HERDR_PANE_ID": s["source"]}), s)
    assert found["action"] == "skipped" and "source_is_caller" in found["reason"]
    assert s["source"] in panes(s["herdr"], s["full"])


@pytest.mark.integration
def test_reap_keeps_the_source_when_the_successor_is_gone(herdr_server, tmp_path):
    s = confirmed_but_open(herdr_server, tmp_path)
    import subprocess
    subprocess.run([s["herdr"], "pane", "close", s["child"]], env=s["full"], capture_output=True, check=True)
    _, found = row(reap(s, "--apply"), s)
    assert found["action"] == "skipped" and "successor_gone" in found["reason"]
    assert s["source"] in panes(s["herdr"], s["full"])


@pytest.mark.integration
def test_reap_marks_a_source_pane_that_is_already_gone(herdr_server, tmp_path):
    import subprocess
    s = confirmed_but_open(herdr_server, tmp_path)
    subprocess.run([s["herdr"], "pane", "close", s["source"]], env=s["full"], capture_output=True, check=True)
    _, found = row(reap(s), s)
    assert found["action"] == "would_close" and "already" in found["reason"]
    _, found = row(reap(s, "--apply"), s)
    assert found["action"] == "already_closed"
    assert source_state(s["root"], s["id"])["takeover"]["phase"] == "already_closed"


@pytest.mark.integration
def test_reap_does_nothing_when_herdr_cannot_be_asked(herdr_server, tmp_path):
    s = confirmed_but_open(herdr_server, tmp_path)
    done = reap(s, "--apply", env={"HERDR_SOCKET_PATH": str(tmp_path / "nobody-listens.sock")})
    assert done.returncode == 1 and "herdr" in done.stderr
    assert source_state(s["root"], s["id"])["takeover"]["phase"] == "confirmed"
    assert s["source"] in panes(s["herdr"], s["full"])


@pytest.mark.integration
def test_confirm_refuses_a_source_pane_that_was_reused(herdr_server, tmp_path):
    s = start(herdr_server, tmp_path, tmp_path)
    edit_record(s, lambda block: block["close_options"].update(source_terminal_id="term_somebody_else"))
    out = json.loads(confirm(s).stdout)
    assert out["close"] == "refused" and "source_pane_reused" in out["reason"]
    assert s["source"] in panes(s["herdr"], s["full"])
