import json
from pathlib import Path

import pytest

from herdr_handoff import (HandoffStore, build_handoff, render_handoff_markdown, resume_prompt,
                           validate_envelope)

FULL = dict(
    goal="Ship the /handoff skill",
    completed=["CLI flags added"],
    remaining=["Open the PR"],
    decisions=["Reuse herdr-handoff, do not fork"],
    risks=["Pi has its own /handoff command"],
    validation=["pytest -q: 41 passed"],
    source_harness="claude", target_harness="codex",
    workspace={"repo": "berlinguyinca/herdr-engineering", "branch": "feat/handoff-skill",
               "worktree": "/work/tree", "cwd": "/work/tree"},
    pull_requests=["https://github.com/berlinguyinca/herdr-engineering/pull/1"],
    files=["/work/tree/herdr_engineering/handoff.py"],
    next_commands=["pytest -q tests/test_handoff.py"],
    constraints=["Do not deploy", "Do not merge"],
    resume_point="Write docs/operations/handoff.md",
    unverified=["CI status was not checked"],
    report={"path": "/home/u/.cache/herdr-engineering/summary/claude-abc.md"},
)


def test_every_field_lands_in_the_envelope():
    package = build_handoff(**FULL)
    ctx = package["context"]
    assert package["schema_version"] == 2
    assert ctx["decisions"] == FULL["decisions"] and ctx["risks"] == FULL["risks"]
    assert ctx["validation"] == FULL["validation"]
    assert ctx["constraints"] == ["Do not deploy", "Do not merge"]
    assert ctx["next_commands"] == FULL["next_commands"]
    assert ctx["files"] == FULL["files"]
    assert ctx["resume_point"] == FULL["resume_point"]
    assert ctx["unverified"] == FULL["unverified"]
    assert package["workspace"]["branch"] == "feat/handoff-skill"
    assert package["links"]["pull_requests"] == FULL["pull_requests"]
    assert package["report"]["path"].endswith("claude-abc.md")
    validate_envelope(package)


def test_legacy_inputs_keep_schema_version_1_and_no_new_keys():
    package = build_handoff(goal="old", remaining=["x"])
    assert package["schema_version"] == 1
    assert "workspace" not in package and "links" not in package and "report" not in package
    assert set(package["context"]) == {"goal", "completed", "remaining", "decisions", "risks", "validation"}
    validate_envelope(package)


def test_secrets_are_redacted_in_every_new_field():
    package = build_handoff(
        goal="g", constraints=["token=abc123secret"], next_commands=["curl -H 'Authorization: Bearer abcdef123456'"],
        unverified=["password: hunter2hunter2"], resume_point="use ghp_" + "A" * 30,
        decisions=["key AKIAABCDEFGHIJKLMNOP"], risks=["sk-" + "b" * 30])
    text = json.dumps(package)
    for leaked in ("abc123secret", "abcdef123456", "hunter2hunter2", "ghp_" + "A" * 30,
                   "AKIAABCDEFGHIJKLMNOP", "sk-" + "b" * 30):
        assert leaked not in text
    assert package["redaction"]["redacted"] is True


@pytest.mark.parametrize("mutate,message", [
    (lambda p: p.pop("handoff_id"), "handoff_id"),
    (lambda p: p["context"].update(goal=""), "goal"),
    (lambda p: p["context"].update(constraints="Do not deploy"), "constraints"),
    (lambda p: p["context"].update(decisions=[1]), "decisions"),
    (lambda p: p["links"].update(pull_requests=["javascript:alert(1)"]), "pull_requests"),
    (lambda p: p["workspace"].update(worktree="relative/path"), "worktree"),
    (lambda p: p["report"].update(path="relative.md"), "report.path"),
    (lambda p: p.update(schema_version=3), "schema_version"),
    (lambda p: p["target"].update(harness="vim"), "target.harness"),
])
def test_validate_rejects_malformed_envelopes(mutate, message):
    package = build_handoff(**FULL)
    mutate(package)
    with pytest.raises(ValueError) as err:
        validate_envelope(package)
    assert message in str(err.value)


def test_builder_refuses_malformed_new_fields():
    with pytest.raises(ValueError, match="pull_requests"):
        build_handoff(goal="g", pull_requests=["ftp://example.com/x"])
    with pytest.raises(ValueError, match="constraints"):
        build_handoff(goal="g", constraints="not a list")
    with pytest.raises(ValueError, match="worktree"):
        build_handoff(goal="g", workspace={"worktree": "rel"})


def test_resume_prompt_carries_the_new_context_and_the_document_path(tmp_path: Path):
    store = HandoffStore(tmp_path)
    package = store.enqueue(build_handoff(**FULL))
    doc = store.write_document(package)
    assert doc.is_absolute() and doc.is_file() and oct(doc.stat().st_mode & 0o777) == "0o600"
    prompt = resume_prompt(package, document=doc)
    for needle in ("Do not deploy", "Write docs/operations/handoff.md", "pytest -q tests/test_handoff.py",
                   "feat/handoff-skill", "pull/1", "Pi has its own /handoff command", "pytest -q: 41 passed",
                   "CI status was not checked", str(doc)):
        assert needle in prompt, needle
    assert prompt.index(str(doc)) < prompt.index("Goal:")  # told to read the file first


def test_markdown_document_is_self_sufficient_and_marks_unverified(tmp_path: Path):
    text = render_handoff_markdown(build_handoff(**FULL))
    for heading in ("# Handoff", "## Goal", "## Completed", "## Remaining", "## Decisions", "## Risks",
                    "## Validation", "## Resume point", "## Workspace", "## Pull requests", "## Key files",
                    "## Next commands", "## Constraints", "## Unverified", "## Full report"):
        assert heading in text, heading
    assert text.index("## Constraints") < text.index("## Next commands")  # rules before actions
    assert "claude-abc.md" in text


def test_document_for_legacy_package_has_no_empty_sections():
    text = render_handoff_markdown(build_handoff(goal="old", remaining=["x"]))
    assert "## Constraints" not in text and "## Workspace" not in text


def test_json_schema_matches_the_builder_when_jsonschema_is_available():
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((Path(__file__).parents[1] / "schemas" / "handoff-v2.schema.json").read_text())
    jsonschema.validate(build_handoff(**FULL), schema)
    jsonschema.validate(build_handoff(goal="old"), schema)
    bad = build_handoff(**FULL)
    bad["context"]["constraints"] = "x"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, schema)
