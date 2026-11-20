"""The close-on-proof default is operator-facing behaviour, so the prose is part of the contract.

A command that closes a terminal is only safe if the operator can read what it will do and what makes it
refuse.  These tests pin the two places that answer - the CLI's own `--help` and the shipped skill/README
text - so a change in behaviour cannot quietly outlive the documentation an agent follows.
"""

import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "src" / "herdr_handoff_cli.py"

OPERATOR_TEXT = ("README.md", "skill/herdr/SKILL.md", "skill/herdr-handoff/SKILL.md")
REFUSAL_STATUSES = ("launch_unconfirmed", "launch_failed", "source_close_failed", "source_dirty",
                    "subagents_running")


def _text(relative):
    return (ROOT / relative).read_text(encoding="utf-8")


def _bullet_containing(marker, text):
    """One routing-rule bullet, so an assertion cannot be satisfied by the word appearing elsewhere."""
    start = text.index(marker)
    rest = text[start + len(marker):]
    end = rest.find("\n- ")
    return text[start:] if end == -1 else text[start:start + len(marker) + end]


@pytest.mark.parametrize("relative", OPERATOR_TEXT)
def test_the_prose_says_takeover_closes_on_proof_and_how_to_stop_it(relative):
    text = _text(relative)
    assert "takeover" in text, f"{relative} never mentions takeover"
    assert "--keep-source" in text, f"{relative} does not document how to keep both panes"


def test_the_to_harness_routing_rule_hands_off_rather_than_leaving_the_source_running():
    rule = _bullet_containing("- `toClaude", _text("skill/herdr/SKILL.md"))
    assert "takeover" in rule, rule
    assert "--keep-source" in rule, rule


@pytest.mark.parametrize("relative", ("README.md", "skill/herdr-handoff/SKILL.md"))
def test_the_refusal_vocabulary_and_the_worktree_override_are_documented(relative):
    text = _text(relative)
    for status in REFUSAL_STATUSES:
        assert status in text, f"{relative} omits the {status} refusal"
    assert "--allow-dirty-source" in text, f"{relative} does not document the worktree override"
    # The rule an agent must not get wrong: nothing closes until the successor is proven and the work is durable.
    assert re.search(r"(?i)(durable|commit)", text), f"{relative} never says the work must be durable"


def test_takeover_help_lists_the_flags_and_the_refusals():
    done = subprocess.run([sys.executable, str(CLI), "takeover", "--help"], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    for token in ("--keep-source", "--direction", "--source-cwd", "--allow-dirty-source", "--confirm-timeout", *REFUSAL_STATUSES):
        assert token in done.stdout, f"takeover --help omits {token}"


@pytest.mark.parametrize("relative", OPERATOR_TEXT)
def test_docs_state_proof_is_herdr_observation(relative):
    text = _text(relative)
    flat = " ".join(text.split())
    assert "herdr observes" in flat, f"{relative} does not say herdr's observation is the proof of life"
    # the successor running `confirm` is an optional record, never the condition for closing the source
    assert not re.search(r"(?i)successor (confirms itself|has run\s+`herdr-handoff confirm)", flat), relative
    assert not re.search(r"(?i)confirmation comes from the successor", flat), relative
    assert not re.search(r"(?i)closes? the (source|current) pane once .{0,80}\bhas run\b", flat), relative


def test_generated_text_never_makes_confirm_the_condition_for_closing(tmp_path):
    from conftest import HARNESSES
    from herdr_handoff import (MAX_TYPED_COMMAND, HandoffStore, build_handoff, build_launch_plan, compact_prompt,
                               takeover_prompt)
    store = HandoffStore(tmp_path / "root")
    package = build_handoff(goal="g")
    document = store.write_document(package)
    handoff_id = package["handoff_id"]
    texts = {"document": document.read_text(encoding="utf-8"),
             "takeover_prompt": takeover_prompt(handoff_id, document),
             "takeover_prompt_root": takeover_prompt(handoff_id, document, root=tmp_path / "root"),
             "compact_prompt": compact_prompt(handoff_id, confirm=True)}
    for harness in HARNESSES:
        plan = build_launch_plan("w1:p1", harness, handoff_id, str(tmp_path / "root" / "outbox" / "x.json"),
                                 prompt=takeover_prompt(handoff_id, document), harness_binary=sys.executable,
                                 fallback_prompt=compact_prompt(handoff_id, confirm=True))
        assert len(plan["run_command"].encode()) <= MAX_TYPED_COMMAND, harness
        texts[f"command:{harness}"] = plan["run_command"]
    for name, text in texts.items():
        flat = " ".join(text.split())
        assert not re.search(r"(?i)can be closed|stays open on purpose|so the pane you replaced", flat), name
        if "confirm" in flat:
            assert "optional" in flat and "herdr observes" in flat, name


def test_the_skill_says_confirm_closes_a_late_confirmed_source():
    text = _text("skill/herdr-handoff/SKILL.md")
    assert "confirm" in text and "closes the source pane" in text
    assert "the close does not wait for it" not in text


def test_confirm_help_names_the_close():
    done = subprocess.run([sys.executable, str(CLI), "confirm", "--help"], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    assert "close" in done.stdout
