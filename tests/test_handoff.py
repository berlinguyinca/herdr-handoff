import json
from pathlib import Path

from herdr_handoff import HandoffStore, build_handoff, build_launch_plan, resume_prompt


def test_build_handoff_redacts_sensitive_context_and_preserves_refs():
    package = build_handoff(
        goal="Continue the artifact publication work",
        completed=["Implemented the registry"],
        remaining=["Add approval UI"],
        decisions=["Private by default"],
        risks=["token=secret-value"],
        validation=["pytest -q"],
        provenance={"repository": "berlinguyinca/herdr-engineering", "commit": "abc123"},
        artifacts=[{"artifact_id": "art_1", "version_id": "ver_1"}],
        source_harness="codex",
        target_harness="claude",
        model_hint="claude-sonnet",
    )

    assert package["schema_version"] == 1
    assert package["target"]["harness"] == "claude"
    assert package["target"]["model_hint"] == "claude-sonnet"
    assert package["artifacts"] == [{"artifact_id": "art_1", "version_id": "ver_1"}]
    assert "secret-value" not in json.dumps(package)
    assert package["redaction"]["redacted"] is True


def test_store_outbox_round_trip_and_idempotent_sync(tmp_path: Path):
    store = HandoffStore(tmp_path)
    package = build_handoff(goal="Resume", remaining=["Finish"], source_harness="pi", target_harness="codex")

    first = store.enqueue(package)
    second = store.enqueue(package)

    assert first["handoff_id"] == second["handoff_id"]
    assert len(list((tmp_path / "outbox").glob("*.json"))) == 1
    assert store.get(package["handoff_id"])["state"] == "queued"


def test_store_state_transitions_reject_invalid_claim(tmp_path: Path):
    store = HandoffStore(tmp_path)
    package = store.enqueue(build_handoff(goal="Resume", source_harness="pi", target_harness="codex"))

    claimed = store.transition(package["handoff_id"], "claimed", actor="agent-b")
    assert claimed["state"] == "claimed"

    try:
        store.transition(package["handoff_id"], "claimed", actor="agent-c")
    except ValueError as exc:
        assert "invalid transition" in str(exc)
    else:
        raise AssertionError("duplicate claim must be rejected")


def test_resume_prompt_is_bounded_and_contains_target_work(tmp_path: Path):
    store = HandoffStore(tmp_path)
    package = store.enqueue(build_handoff(goal="Resume now", remaining=["Review artifact"],
                                          artifacts=[{"artifact_id": "art_1", "version_id": "v1"}]))
    prompt = resume_prompt(package)
    assert "Review artifact" in prompt
    assert "art_1" in prompt
    assert "publish" in prompt.lower()


def test_launch_plan_splits_current_pane_and_starts_target_interactively():
    plan = build_launch_plan("wC:p1", "claude", "handoff_" + "a" * 32,
                             "/tmp/handoff.json", model_hint="sonnet", cwd="/work")
    assert plan["split"][0:4] == ["herdr", "pane", "split", "wC:p1"]
    assert "--direction" in plan["split"] and "right" in plan["split"]
    assert plan["run_binary"] == "claude"
    assert "--model sonnet" in plan["run_command"]
    assert "/tmp/handoff.json" in plan["run_command"]
    assert plan["metadata"]["handoff_id"].startswith("handoff_")
