"""A successor pane gets a distinctive name: the project plus a 5-10 word summary of the goal."""
from __future__ import annotations

from herdr_handoff import pane_label

ID = "handoff_" + "a" * 24 + "deadbeef"


def test_label_is_project_and_short_goal_summary():
    goal = ("Make Screenwright's review server automatically fabric-reachable (1:1 localhost->fabric URL) "
            "for insight web UI QA; the web UI branch itself is done.")
    label = pane_label("/home/u/insight-service", goal, ID)
    project, summary = label.split(": ", 1)
    assert project == "insight-service"
    assert 5 <= len(summary.split()) <= 10
    assert summary.startswith("Make Screenwright's review server")


def test_short_goal_is_kept_whole_and_first_sentence_only():
    assert pane_label("/x/repo", "Fix login redirect loop. Then update docs for the whole thing.", ID) \
        == "repo: Fix login redirect loop"


def test_empty_goal_falls_back_to_the_handoff_id_suffix():
    assert pane_label("/x/repo", "   ", ID) == "repo: handoff deadbeef"


def test_newlines_and_markdown_noise_are_flattened():
    assert "\n" not in pane_label("/x/repo", "# Port the\nparser to Scala 3 and add tests", ID)
