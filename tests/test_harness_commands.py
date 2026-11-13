import shlex
import shutil
import subprocess

import pytest

from herdr_handoff import (HARNESS_CLI, MAX_TYPED_COMMAND, build_launch_plan, compact_prompt,
                           harness_argv, short_prompt, takeover_prompt)

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


def test_a_resolved_binary_is_pinned_into_the_command_so_the_pane_runs_that_program(tmp_path):
    # A pane starts a login shell, which rebuilds PATH (macOS /usr/libexec/path_helper hoists /etc/paths
    # above anything herdr's server environment prepended), so a bare name can start a different program
    # than the one the launcher meant. Measured here: a handoff meant for one harness started another.
    exe = tmp_path / "pi"
    exe.write_text("#!/bin/sh\nexit 0\n")
    exe.chmod(0o755)
    plan = build_launch_plan(None, "pi", ID, "/tmp/h.json", prompt="go", harness_binary=str(exe))
    assert shlex.split(plan["run_command"])[3:] == [str(exe), "go"]
    assert plan["argv"][0] == str(exe)


def test_a_pinned_binary_must_be_an_existing_absolute_executable():
    for not_a_program in ("pi", "/definitely/not/here", "relative/../pi"):
        with pytest.raises(ValueError):
            harness_argv("pi", binary=not_a_program)


def test_the_typed_command_always_fits_what_herdr_can_actually_deliver():
    # herdr types a command longer than ~1 KB into a pane without the shell ever running it (measured:
    # 1022 bytes ran, 1072 did not), so a deep handoff root must not silently prevent the successor.
    deep = "/" + "/".join(["verylongdirectorysegment"] * 12) + "/handoffs"
    handoff_file = f"{deep}/outbox/handoff_{'a' * 32}.json"
    document = f"{deep}/documents/handoff_{'a' * 32}.md"
    prompt = takeover_prompt(ID, document)
    plan = build_launch_plan("w1:p1", "pi", ID, handoff_file, prompt=prompt,
                             fallback_prompt=compact_prompt(ID, confirm=True))
    assert len(plan["full_command"]) > MAX_TYPED_COMMAND
    assert len(plan["run_command"]) <= MAX_TYPED_COMMAND
    assert any("shortened" in note for note in plan["notes"])
    typed = shlex.split(plan["run_command"])
    # the env vars carry the paths, so the prompt itself stays short and names the successor command
    assert typed[1:3] == [f"HERDR_HANDOFF_ID={ID}", f"HERDR_HANDOFF_FILE={handoff_file}"]
    assert ID in plan["run_command"] and f"herdr-handoff confirm {ID}" in plan["run_command"]
    assert "verylongdirectorysegment" not in typed[-1]
    assert "HERDR_HANDOFF_FILE" in typed[-1]


def test_a_normal_handoff_is_typed_in_full_with_the_document_path():
    plan = build_launch_plan("w1:p1", "pi", ID, "/home/u/.local/share/herdr-handoff/outbox/x.json",
                             prompt=takeover_prompt(ID, "/home/u/.local/share/herdr-handoff/documents/x.md"),
                             fallback_prompt=compact_prompt(ID, confirm=True))
    assert plan["run_command"] == plan["full_command"]
    assert "/home/u/.local/share/herdr-handoff/documents/x.md" in plan["run_command"]
    assert plan["notes"] == []


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
