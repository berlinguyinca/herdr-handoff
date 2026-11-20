"""`confirm` closes the source pane a takeover gave up waiting on, under the same close gate."""
from __future__ import annotations

import json
import os
import subprocess
import time

import pytest

from conftest import pushed_repo
from herdr_handoff import HandoffStore
from test_source_worktree_guard import local_only_commit
from test_takeover import create, panes, run_cli, source_state, takeover_args

CONFIRM_TIMEOUT = "3"


def start(herdr_server, cwd, tmp_path, *extra):
    """Run a takeover whose recorder harness never goes live, so it gives up before confirmation."""
    env, herdr = herdr_server["env"], herdr_server["herdr"]
    full = {**os.environ, **env}
    ws = json.loads(subprocess.run([herdr, "workspace", "create", "--cwd", str(cwd)], env=full,
                                   capture_output=True, text=True).stdout)
    source = ws["result"]["root_pane"]["pane_id"]
    root = tmp_path / "root"
    package = create(root, env=env)
    args = takeover_args(root, package["handoff_id"], source, cwd, *extra)
    args[args.index("--confirm-timeout") + 1] = CONFIRM_TIMEOUT
    done = run_cli(args, env={**env, "HERDR_PANE_ID": source})
    out = json.loads(done.stdout)
    assert source in panes(herdr, full)
    return {"env": env, "full": full, "herdr": herdr, "root": root, "id": package["handoff_id"],
            "source": source, "child": out["pane_id"]}


def confirm(s, pane=None):
    done = run_cli(["--root", str(s["root"]), "confirm", s["id"]],
                   env={**s["env"], "HERDR_PANE_ID": pane or s["child"]})
    return done


def edit_record(s, change):
    path = s["root"] / "outbox" / f"{s['id']}.json"
    data = json.loads(path.read_text())
    change(data["takeover"])
    path.write_text(json.dumps(data))


@pytest.mark.integration
def test_a_late_confirm_closes_the_source_pane(herdr_server, tmp_path):
    s = start(herdr_server, tmp_path, tmp_path)
    done = confirm(s)
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    assert out["close"] == "source_closed" and out["source_closed"] is True and out["source_closed_at"]
    assert out["reason"] == ""
    assert s["source"] not in panes(s["herdr"], s["full"])
    assert source_state(s["root"], s["id"])["takeover"]["phase"] == "source_closed"


@pytest.mark.integration
def test_a_second_confirm_after_the_close_is_idempotent(herdr_server, tmp_path):
    s = start(herdr_server, tmp_path, tmp_path)
    first = json.loads(confirm(s).stdout)
    second = json.loads(confirm(s).stdout)
    assert second["idempotent"] is True and second["close"] == "source_closed"
    assert second["source_closed_at"] == first["source_closed_at"]


@pytest.mark.integration
def test_a_late_confirm_refuses_a_source_with_unpushed_commits(herdr_server, tmp_path):
    repo = pushed_repo(tmp_path)
    local_only_commit(repo)
    s = start(herdr_server, repo, tmp_path)
    out = json.loads(confirm(s).stdout)
    assert out["close"] == "refused" and out["source_closed"] is False and "unpushed" in out["reason"]
    assert s["source"] in panes(s["herdr"], s["full"])
    assert source_state(s["root"], s["id"])["takeover"]["phase"] == "confirmed"


@pytest.mark.integration
def test_a_late_confirm_closes_a_dirty_source_when_takeover_allowed_it(herdr_server, tmp_path):
    repo = pushed_repo(tmp_path)
    local_only_commit(repo)
    s = start(herdr_server, repo, tmp_path, "--allow-dirty-source")
    out = json.loads(confirm(s).stdout)
    assert out["close"] == "source_closed", out
    assert s["source"] not in panes(s["herdr"], s["full"])


@pytest.mark.integration
def test_a_late_confirm_respects_keep_source(herdr_server, tmp_path):
    s = start(herdr_server, tmp_path, tmp_path, "--keep-source")
    out = json.loads(confirm(s).stdout)
    assert out["close"] == "not_attempted" and out["source_closed"] is False
    assert s["source"] in panes(s["herdr"], s["full"])


@pytest.mark.integration
def test_a_late_confirm_leaves_a_legacy_record_alone(herdr_server, tmp_path):
    s = start(herdr_server, tmp_path, tmp_path)
    edit_record(s, lambda block: block.pop("close_options"))
    out = json.loads(confirm(s).stdout)
    assert out["close"] == "not_attempted"
    assert s["source"] in panes(s["herdr"], s["full"])


@pytest.mark.integration
def test_a_late_confirm_refuses_when_the_record_names_another_tab(herdr_server, tmp_path):
    s = start(herdr_server, tmp_path, tmp_path)
    edit_record(s, lambda block: block.update(tab_id="w_other:t_other"))
    out = json.loads(confirm(s).stdout)
    assert out["close"] == "refused" and "not_same_tab" in out["reason"]
    assert s["source"] in panes(s["herdr"], s["full"])


@pytest.mark.integration
def test_a_late_confirm_when_the_source_pane_is_gone_reports_already_closed(herdr_server, tmp_path):
    s = start(herdr_server, tmp_path, tmp_path)
    subprocess.run([s["herdr"], "pane", "close", s["source"]], env=s["full"], capture_output=True, check=True)
    out = json.loads(confirm(s).stdout)
    assert out["close"] == "already_closed" and out["source_closed"] is True
    assert source_state(s["root"], s["id"])["takeover"]["phase"] == "already_closed"


@pytest.mark.integration
def test_confirm_does_not_close_while_a_takeover_holds_the_lock(herdr_server, tmp_path):
    s = start(herdr_server, tmp_path, tmp_path)
    with HandoffStore(s["root"]).takeover_lock(s["id"]):
        began = time.monotonic()
        out = json.loads(confirm(s).stdout)
    assert time.monotonic() - began < 5
    assert out["close"] == "not_attempted" and "takeover of this handoff is running" in out["reason"]
    assert s["source"] in panes(s["herdr"], s["full"])
    assert source_state(s["root"], s["id"])["takeover"]["phase"] == "confirmed"


@pytest.mark.integration
def test_only_the_successor_pane_can_trigger_the_close(herdr_server, tmp_path):
    s = start(herdr_server, tmp_path, tmp_path)
    done = confirm(s, pane=s["source"])
    assert done.returncode != 0 and "only the successor pane" in (done.stderr + done.stdout)
    assert s["source"] in panes(s["herdr"], s["full"])
