"""The CI configuration is a shipped artifact, so it is checked like one.

These tests read the Woodpecker pipeline and the gate scripts as text. They do not parse YAML: pyyaml is not a
dependency of this package and a test that needs a venv to check a config file is a test CI cannot run without
network. What is guarded here is the set of mistakes that make a CI config quietly useless:

* a pipeline whose referenced script does not exist (red herring on every PR),
* a `path:` include filter that stops the pipeline reporting on some pull requests, which makes it unsatisfiable
  the moment it is marked required in GitHub,
* a gate that narrows the suite it claims to run.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parents[1]
PIPELINE = ROOT / ".woodpecker" / "unit-gates.yml"
GATE = ROOT / "scripts" / "ci" / "unit-gates.sh"

# The lint scope, verbatim.  Widening or narrowing what CI lints has to be a deliberate edit in two places, and
# the reason the scope is a subset at all is written in the gate script next to this string.
LINT_SCOPE = "--select E4,E7,E9,F"


def _text(path: Path) -> str:
    assert path.is_file(), f"{path.relative_to(ROOT)} is missing"
    return path.read_text(encoding="utf-8")


def test_the_pipeline_exists_and_names_the_gate_scripts_that_exist():
    pipeline = _text(PIPELINE)
    referenced = set(re.findall(r"bash (scripts/ci/[a-z0-9.-]+\.sh)", pipeline))
    assert referenced, "the pipeline runs no gate script; it proves nothing"
    for script in sorted(referenced):
        path = ROOT / script
        assert path.is_file(), f"{pipeline.name} runs {script}, which is not in the repository"
        assert path.stat().st_mode & 0o111, f"{script} is not executable"
        assert path.read_text(encoding="utf-8").startswith("#!"), f"{script} has no shebang"


def test_the_pipeline_reports_on_every_pull_request():
    """A path-filtered check never reports on some PRs, so it cannot be made required (herdr-engineering ci-gates.md)."""
    pipeline = _text(PIPELINE)
    assert re.search(r"^\s*-\s*event:\s*pull_request\s*$", pipeline, re.MULTILINE), (
        "the pull_request trigger must be its own unconditional entry"
    )
    assert "path:" not in pipeline, "a path: include filter makes the check unsatisfiable once it is required"


def test_the_pipeline_also_runs_on_the_default_branch_and_by_hand():
    pipeline = _text(PIPELINE)
    assert re.search(r"^\s*-\s*event:\s*manual\s*$", pipeline, re.MULTILINE), "no manual run: no way to re-prove main"
    assert re.search(r"^\s*-\s*event:\s*push\s*$", pipeline, re.MULTILINE), "main/master is never gated by a push"
    assert re.search(r"^\s*branch:\s*master\s*$", pipeline, re.MULTILINE), "the push trigger must name the default branch"


def test_the_pipeline_builds_its_checkout_from_the_staged_credential():
    """`skip_clone` jobs that assume a working tree fail with something that looks like a broken commit."""
    pipeline = _text(PIPELINE)
    assert "skip_clone: true" in pipeline
    assert 'git fetch --no-tags --depth=1 origin "$CI_COMMIT_SHA"' in pipeline
    assert "test -r /etc/woodpecker-git-credentials" in pipeline, "no credential check: the failure names the wrong file"


def test_the_gate_installs_the_package_and_runs_the_whole_unit_suite():
    gate = _text(GATE)
    assert "python3 -m venv" in gate, "the gate must not install into the host interpreter"
    assert re.search(r"pip.*install.*-e\s+'\.\[dev\]'", gate), "the gate must install the package with its dev extra"
    assert "python -m pytest -q" in gate
    for narrowing in (" --deselect ", " -k ", "--collect-only", " -x "):
        assert narrowing not in gate, f"{narrowing.strip()} narrows the suite the gate claims to run"


def test_the_gate_lints_the_documented_scope_and_says_why_it_is_a_subset():
    gate = _text(GATE)
    assert LINT_SCOPE in gate, "the lint scope changed; this test and the gate header must be edited together"
    assert "PLW1510" in gate, "the header must say which findings the scope leaves out, and why"


def test_the_gate_proves_the_shipped_data_files_parse():
    """`skill/herdr/commands.json` and `schemas/*.json` are read by other people's tools with no test of their own."""
    gate = _text(GATE)
    assert "ls-files" in gate and "json.load" in gate, "no data-file parse check"
    assert "tomllib" in gate, "pyproject.toml is not parsed"
    assert "<<<<<<<" in gate, "no conflict-marker scan"


def test_the_gate_journals_because_the_agent_drops_the_log_tail():
    """ci.metabolomics.us pipeline 1: `unit-gates: success exit 0`, and the stored step log ended at "== pytest ==".

    The suite had run and passed; its summary was not in the log.  Readable output is part of the gate, so the run
    mirrors itself to an archive directory and prints where.
    """
    gate = _text(GATE)
    assert "journal.sh" in gate and "ci_journal unit-gates" in gate, "the gate does not journal its output"
    assert "tee" in _text(ROOT / "scripts" / "ci" / "journal.sh")
    assert "ci-logs" in _text(ROOT / "scripts" / "ci" / "log-archive.yml"), "no archive directory named"


def test_the_readme_names_the_command_that_reproduces_ci():
    assert "bash scripts/ci/unit-gates.sh" in _text(ROOT / "README.md")
