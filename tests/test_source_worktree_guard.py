"""The dirty-worktree gate that stands between a confirmed successor and closing the source pane.

Closing a pane kills the agent inside it, and the only thing that cannot be recovered is what that
agent had not yet made durable.  Git is what says whether work is durable, so the gate reads git -
against real repositories created here, never against a stubbed one.

The decided treatment of untracked files is part of the contract: an untracked-only tree is NOT
dirty.  Harness state directories (`.claude/`, `.pi/`, virtualenvs, build output) sit untracked in
almost every real checkout, so counting them would refuse almost every takeover and teach the
operator to pass the override flag by reflex.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from conftest import git, pushed_repo
from herdr_handoff import decide_takeover_close, source_worktree_state, worktree_close_refusal

SRC = str(Path(__file__).parents[1] / "src")


# --- what git says about the pane's worktree ---------------------------------------------------------

def test_a_clean_worktree_whose_commits_the_remote_has_passes(tmp_path):
    work = pushed_repo(tmp_path)
    state = source_worktree_state(work)
    assert state["checked"] is True and state["is_repo"] is True
    assert state["branch"] == "main"
    assert (state["tracked_changes"], state["untracked"], state["unpushed"]) == (0, 0, 0)
    assert worktree_close_refusal(state) == ""


def test_uncommitted_tracked_changes_are_counted_and_refuse_the_close(tmp_path):
    work = pushed_repo(tmp_path)
    (work / "README.md").write_text("half-finished edit\n", encoding="utf-8")
    state = source_worktree_state(work)
    assert state["tracked_changes"] == 1 and state["unpushed"] == 0
    assert worktree_close_refusal(state) == "worktree_dirty"
    # a staged change is just as uncommitted as an unstaged one
    git(work, "add", "README.md")
    assert source_worktree_state(work)["tracked_changes"] == 1


def test_untracked_and_ignored_files_alone_do_not_make_the_worktree_dirty(tmp_path):
    # Harness state and build output (.claude/, .pi/, compiled artefacts) sit untracked or gitignored in
    # almost every real checkout.  Neither counts as dirty; an ignored file is not even untracked.
    work = pushed_repo(tmp_path)
    (work / ".gitignore").write_text("build-output.md\n", encoding="utf-8")
    git(work, "add", ".gitignore")
    git(work, "commit", "-q", "-m", "ignore build output")
    git(work, "push", "-q", "origin", "main")
    (work / "build-output.md").write_text("compiled artefact\n", encoding="utf-8")
    (work / "scratch.md").write_text("notes\n", encoding="utf-8")
    state = source_worktree_state(work)
    assert state["tracked_changes"] == 0 and state["untracked"] == 1
    assert worktree_close_refusal(state) == ""


def test_a_commit_the_remote_does_not_have_refuses_the_close(tmp_path):
    work = pushed_repo(tmp_path)
    (work / "feature.py").write_text("def feature():\n    return 1\n", encoding="utf-8")
    git(work, "add", "feature.py")
    git(work, "commit", "-q", "-m", "work only on this machine")
    state = source_worktree_state(work)
    assert state["unpushed"] == 1 and state["tracked_changes"] == 0
    assert worktree_close_refusal(state) == "worktree_unpushed"
    # pushing makes the work durable, and the same gate then lets the close through
    git(work, "push", "-q", "origin", "main")
    assert source_worktree_state(work)["unpushed"] == 0
    assert worktree_close_refusal(source_worktree_state(work)) == ""


def test_commits_on_a_branch_other_than_upstream_still_count_as_pushed(tmp_path):
    # A developer branch whose commits sit on origin under the same name is durable, even with no
    # @{upstream} configured; a commit that exists nowhere else is not.
    work = pushed_repo(tmp_path)
    git(work, "checkout", "-q", "-b", "feat/thing")
    (work / "a.py").write_text("a = 1\n", encoding="utf-8")
    git(work, "add", "a.py")
    git(work, "commit", "-q", "-m", "a")
    assert source_worktree_state(work)["unpushed"] == 1
    git(work, "push", "-q", "origin", "feat/thing")
    assert source_worktree_state(work)["unpushed"] == 0


def test_a_repository_with_no_remote_at_all_is_unpushed(tmp_path):
    work = tmp_path / "solo"
    work.mkdir()
    git(work, "init", "-q", "-b", "main")
    (work / "only.py").write_text("only here\n", encoding="utf-8")
    git(work, "add", "only.py")
    git(work, "commit", "-q", "-m", "local only")
    state = source_worktree_state(work)
    assert state["is_repo"] is True and state["remotes"] == 0 and state["unpushed"] == 1
    assert worktree_close_refusal(state) == "worktree_unpushed"


def test_a_directory_that_is_not_a_repository_holds_nothing_git_could_have_lost(tmp_path):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    (scratch / "notes.md").write_text("scratch\n", encoding="utf-8")
    state = source_worktree_state(scratch)
    assert state["checked"] is True and state["is_repo"] is False
    assert worktree_close_refusal(state) == ""


def test_an_uninspectable_worktree_is_reported_as_unknown_and_refuses_the_close(tmp_path):
    # Erring toward leaving the pane alive: a path we cannot ask git about is not evidence of clean.
    for path in (None, "", tmp_path / "does-not-exist"):
        state = source_worktree_state(path)
        assert state["checked"] is False, path
        assert worktree_close_refusal(state) == "worktree_unknown"


def test_a_subdirectory_of_a_repository_is_guarded_as_the_whole_worktree(tmp_path):
    work = pushed_repo(tmp_path)
    nested = work / "pkg" / "deep"
    nested.mkdir(parents=True)
    (work / "README.md").write_text("dirty from above\n", encoding="utf-8")
    state = source_worktree_state(nested)
    assert state["path"] and Path(state["path"]).resolve() == work.resolve()
    assert worktree_close_refusal(state) == "worktree_dirty"


# --- the decision that combines it with the pane placement ------------------------------------------

def clean_state(**over):
    state = {"checked": True, "path": "/repo", "is_repo": True, "branch": "main", "tracked_changes": 0,
             "untracked": 0, "unpushed": 0, "remotes": 1, "detail": ""}
    return {**state, **over}


def allowed(**over):
    return decide_takeover_close(placement="split", same_tab=True, source_pane="w1:p1",
                                 child_pane="w1:p2", confirmed=True, **over)


def test_the_close_gate_refuses_a_dirty_or_unpushed_source_worktree():
    assert allowed(worktree=clean_state(tracked_changes=2)) == {"close": False, "reason": "worktree_dirty"}
    assert allowed(worktree=clean_state(unpushed=1)) == {"close": False, "reason": "worktree_unpushed"}
    assert allowed(worktree=clean_state(checked=False)) == {"close": False, "reason": "worktree_unknown"}


def test_the_close_gate_allows_a_worktree_that_is_clean_apart_from_untracked_files():
    assert allowed(worktree=clean_state(untracked=37)) == {"close": True, "reason": ""}


def test_the_explicit_override_closes_a_dirty_source_but_never_an_unconfirmed_successor():
    assert allowed(worktree=clean_state(tracked_changes=2), allow_dirty=True) == {"close": True, "reason": ""}
    # the override speaks for the worktree, not for the proof of liveness
    assert decide_takeover_close(placement="split", same_tab=True, source_pane="w1:p1", child_pane="w1:p2",
                                 confirmed=False, worktree=clean_state(tracked_changes=2),
                                 allow_dirty=True) == {"close": False, "reason": "unconfirmed"}
    assert decide_takeover_close(placement="tab", same_tab=True, source_pane="w1:p1", child_pane="w1:p2",
                                 confirmed=True, worktree=clean_state(tracked_changes=2),
                                 allow_dirty=True) == {"close": False, "reason": "not_same_tab"}


def test_placement_refusals_outrank_the_worktree_refusal():
    # a takeover that must not close for placement reasons says so rather than complaining about git
    assert decide_takeover_close(placement="tab", same_tab=True, source_pane="w1:p1", child_pane="w1:p2",
                                 confirmed=True,
                                 worktree=clean_state(tracked_changes=2)) == {"close": False,
                                                                             "reason": "not_same_tab"}


# --- the operator-facing surface ---------------------------------------------------------------------

def run_cli(args, *, env=None):
    base = {k: v for k, v in os.environ.items() if not k.startswith("HERDR_")}
    base["PYTHONPATH"] = SRC
    base.update(env or {})
    return subprocess.run([sys.executable, "-m", "herdr_handoff_cli", *args], env=base,
                          capture_output=True, text=True, timeout=60)


def test_takeover_help_names_the_guard_and_its_two_ways_out():
    done = run_cli(["takeover", "--help"])
    assert done.returncode == 0, done.stderr
    help_text = done.stdout
    assert "--keep-source" in help_text and "--allow-dirty-source" in help_text and "--source-cwd" in help_text
    # the untracked decision is documented where the operator reads it, not only in the source
    assert "untracked" in help_text


def test_the_refusal_statuses_are_documented_on_the_takeover_help():
    # a refusal must not be retried blindly: the operator needs to see which statuses are refusals
    done = run_cli(["takeover", "--help"])
    for status in ("launch_unconfirmed", "launch_failed", "source_close_failed", "source_dirty"):
        assert status in done.stdout, status
