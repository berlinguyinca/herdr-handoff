"""The skill installer, run for real: an update must move the harness skills forward, not skip them.

`install/install.sh` symlinks each skill out of the checkout it lives in, and herdr-engineering installs a
*pinned* revision, so a machine that already has the skill linked from an earlier checkout is exactly the
case that used to be reported as a collision and left behind.  Nothing here is mocked: the script is run
with `sh` against a temporary skill root and two separate checkouts on disk.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).parents[1]
HARNESS_DIRS = ("codex", "claude", "pi", "opencode")


def _run_install(checkout: Path, skill_root: Path):
    env = {**os.environ, "HERDR_HANDOFF_SKILL_ROOT": str(skill_root)}
    return subprocess.run(["sh", str(checkout / "install" / "install.sh")], capture_output=True,
                          text=True, env=env, check=False)


def _checkout(tmp_path: Path, name: str, marker: str) -> Path:
    """A second checkout of the skill tree, distinguishable by a marker in the skill text."""
    target = tmp_path / name
    target.mkdir(parents=True)
    for skill in ("herdr-handoff", "herdr"):
        dest = target / "skill" / skill
        dest.mkdir(parents=True)
        (dest / "SKILL.md").write_text(f"---\nname: {skill}\n---\n{marker}\n")
    (target / "pyproject.toml").write_text('[project]\nname = "herdr-handoff"\nversion = "0.0.0"\n')
    (target / "install").mkdir()
    (target / "install" / "install.sh").write_text((REPO / "install" / "install.sh").read_text())
    return target


@pytest.fixture()
def skill_root(tmp_path):
    root = tmp_path / "harness-homes"
    root.mkdir()
    return root


def test_a_fresh_install_links_both_skills_into_all_four_harnesses(tmp_path, skill_root):
    checkout = _checkout(tmp_path, "checkout-a", "revision A")
    result = _run_install(checkout, skill_root)
    assert result.returncode == 0, result.stderr
    for harness in HARNESS_DIRS:
        for skill in ("herdr", "herdr-handoff"):
            link = skill_root / harness / "skills" / skill
            assert link.is_symlink(), f"{harness}/{skill} was not installed"
            assert link.resolve() == (checkout / "skill" / skill).resolve()


def test_re_running_the_same_checkout_changes_nothing(tmp_path, skill_root):
    checkout = _checkout(tmp_path, "checkout-a", "revision A")
    assert _run_install(checkout, skill_root).returncode == 0
    second = _run_install(checkout, skill_root)
    assert second.returncode == 0, second.stderr
    assert "installed " not in second.stdout
    assert "collision" not in second.stderr


def test_an_update_from_another_pinned_checkout_moves_the_skill_forward(tmp_path, skill_root):
    old = _checkout(tmp_path, "checkout-old", "revision A")
    new = _checkout(tmp_path, "checkout-new", "revision B: takeover")
    assert _run_install(old, skill_root).returncode == 0
    assert "takeover" not in (skill_root / "pi" / "skills" / "herdr-handoff" / "SKILL.md").read_text()

    updated = _run_install(new, skill_root)
    assert updated.returncode == 0, updated.stderr
    assert "collision" not in updated.stderr, updated.stderr
    for harness in HARNESS_DIRS:
        for skill in ("herdr", "herdr-handoff"):
            link = skill_root / harness / "skills" / skill
            assert link.is_symlink() and link.resolve() == (new / "skill" / skill).resolve(), f"{harness}/{skill}"
    assert "takeover" in (skill_root / "pi" / "skills" / "herdr-handoff" / "SKILL.md").read_text()


def test_a_skill_the_operator_wrote_is_never_touched(tmp_path, skill_root):
    checkout = _checkout(tmp_path, "checkout-a", "revision A")
    mine = skill_root / "pi" / "skills" / "herdr-handoff"
    mine.mkdir(parents=True)
    (mine / "SKILL.md").write_text("---\nname: herdr-handoff\n---\nmy own words\n")

    result = _run_install(checkout, skill_root)
    assert result.returncode == 0
    assert (mine / "SKILL.md").read_text().endswith("my own words\n")
    assert not mine.is_symlink()
    assert "collision" in result.stderr
    # the other harnesses still get installed
    assert (skill_root / "codex" / "skills" / "herdr-handoff").is_symlink()


def test_a_link_to_a_tree_that_cannot_prove_it_is_herdr_handoff_is_left_alone(tmp_path, skill_root):
    checkout = _checkout(tmp_path, "checkout-a", "revision A")
    unmarked = tmp_path / "looks-like-a-pin" / "skill" / "herdr-handoff"
    unmarked.mkdir(parents=True)
    (unmarked / "SKILL.md").write_text("---\nname: herdr-handoff\n---\nunmarked tree\n")
    link = skill_root / "opencode" / "skills" / "herdr-handoff"
    link.parent.mkdir(parents=True)
    link.symlink_to(unmarked)

    result = _run_install(checkout, skill_root)
    assert result.returncode == 0
    assert link.resolve() == unmarked.resolve()
    assert "collision" in result.stderr


def test_a_symlink_to_someone_elses_tree_is_left_alone(tmp_path, skill_root):
    checkout = _checkout(tmp_path, "checkout-a", "revision A")
    # same directory layout and same skill name, but it is not a herdr-handoff tree
    foreign = tmp_path / "other-project" / "skill" / "herdr-handoff"
    foreign.mkdir(parents=True)
    (tmp_path / "other-project" / "pyproject.toml").write_text('[project]\nname = "my-own-skills"\n')
    (foreign / "SKILL.md").write_text("---\nname: herdr-handoff\n---\nforeign\n")
    link = skill_root / "claude" / "skills" / "herdr-handoff"
    link.parent.mkdir(parents=True)
    link.symlink_to(foreign)

    result = _run_install(checkout, skill_root)
    assert result.returncode == 0
    assert link.resolve() == foreign.resolve()
    assert "collision" in result.stderr
