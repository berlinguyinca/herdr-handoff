import shlex
import shutil
import subprocess

import pytest

from herdr_handoff import HARNESS_CLI, build_launch_plan, harness_argv, short_prompt

ID = "handoff_" + "a" * 32


@pytest.mark.parametrize("harness,model,expected", [
    ("claude", "sonnet", ["claude", "--model", "sonnet", "P"]),
    ("pi", "metabolomics/qwen", ["pi", "--model", "metabolomics/qwen", "P"]),
    ("codex", "gpt-5", ["codex", "--model", "gpt-5", "P"]),
    ("opencode", "anthropic/claude-sonnet", ["opencode", "--model", "anthropic/claude-sonnet", "--prompt", "P"]),
    ("claude", None, ["claude", "P"]),
    ("opencode", None, ["opencode", "--prompt", "P"]),
])
def test_argv_per_harness(harness, model, expected):
    argv, notes = harness_argv(harness, model_hint=model, prompt="P")
    assert argv == expected and notes == []


def test_opencode_model_without_provider_is_dropped_and_reported():
    argv, notes = harness_argv("opencode", model_hint="sonnet", prompt="P")
    assert argv == ["opencode", "--prompt", "P"]
    assert "provider/model" in notes[0]


def test_unsafe_model_and_unknown_harness_are_refused():
    with pytest.raises(ValueError):
        harness_argv("claude", model_hint="x; rm -rf /")
    with pytest.raises(ValueError):
        harness_argv("vim")


def test_shell_command_round_trips_a_hostile_prompt():
    prompt = "it's \"quoted\" $(whoami) `id`; & more"
    plan = build_launch_plan(None, "codex", ID, "/tmp/h.json", prompt=prompt)
    words = shlex.split(plan["run_command"])
    assert words[:3] == ["env", f"HERDR_HANDOFF_ID={ID}", "HERDR_HANDOFF_FILE=/tmp/h.json"]
    assert words[3:] == ["codex", prompt]
    assert plan["split"] is None


def test_short_prompt_is_one_line_and_names_the_document():
    text = short_prompt(ID, "/home/u/.local/share/herdr-handoff/documents/x.md")
    assert "\n" not in text and "/home/u/.local/share/herdr-handoff/documents/x.md" in text and ID in text


@pytest.mark.integration
@pytest.mark.parametrize("harness", sorted(HARNESS_CLI))
def test_flags_in_the_table_exist_in_the_real_harness_help(harness):
    """Slow (pi takes ~25 s to boot): opt in with -m integration."""
    exe = shutil.which(HARNESS_CLI[harness]["bin"])
    if not exe:
        pytest.skip(f"{harness} not installed")
    done = subprocess.run([exe, "--help"], capture_output=True, text=True, timeout=120)
    text = done.stdout + done.stderr
    assert HARNESS_CLI[harness]["model"] in text
    if HARNESS_CLI[harness]["prompt"]:
        assert HARNESS_CLI[harness]["prompt"] in text
