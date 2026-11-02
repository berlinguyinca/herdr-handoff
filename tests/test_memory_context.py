import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import herdr_handoff as hh
from herdr_handoff import (HandoffStore, build_handoff, build_memory_context, resolve_memory_refs,
                           resume_prompt, validate_memory_context)

FIXTURE = Path(__file__).parent / "fixtures" / "legacy_handoff_pre_memory.json"
REF = {"memory_id": "mem_1", "bank_id": "bank_a", "revision": "r3"}
SECRET_TEXT = "TOP-SECRET-MEMORY-TEXT password=hunter2"


def fake_runner(status=None, get=None, calls=None):
    def run(argv, **kw):
        if calls is not None:
            calls.append(argv)
        sub = argv[1]
        payload = status if sub == "status" else get
        if payload is None:
            return SimpleNamespace(returncode=1, stdout="", stderr="connection refused")
        if isinstance(payload, SimpleNamespace):
            return payload
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")
    return run


@pytest.fixture
def with_bin(tmp_path, monkeypatch):
    exe = tmp_path / "herdr-memory"
    exe.write_text("#!/bin/sh\nexit 0\n")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("HERDR_MEMORY_BIN", str(exe))
    return exe


@pytest.fixture
def no_bin(monkeypatch):
    monkeypatch.delenv("HERDR_MEMORY_BIN", raising=False)
    monkeypatch.setenv("PATH", "")


def test_legacy_package_unchanged_and_resumes():
    old = json.loads(FIXTURE.read_text())
    assert "memory_context" not in old
    prompt = resume_prompt(old)
    assert "UNTRUSTED" not in prompt and "Legacy package" in prompt
    assert resolve_memory_refs(old.get("memory_context")) == []


def test_build_handoff_without_memory_has_no_field():
    assert "memory_context" not in build_handoff(goal="x")


def test_omitted_when_cli_unavailable(no_bin):
    assert build_memory_context([REF]) is None


def test_synced_roundtrip(with_bin, tmp_path):
    block = build_memory_context([REF], runner=fake_runner(status={"outbox_pending": 0, "text": SECRET_TEXT}))
    assert block["checkpoint"]["state"] == "synced" and block["checkpoint"]["outbox_pending"] == 0
    assert block["refs"] == [REF] and block["snapshot_digest"].startswith("sha256:")
    store = HandoffStore(tmp_path / "s")
    stored = store.enqueue(build_handoff(goal="g", memory_context=block))
    assert store.get(stored["handoff_id"])["memory_context"] == block
    assert SECRET_TEXT not in json.dumps(stored) and "hunter2" not in json.dumps(stored)


def test_offline_outbox_marks_pending_and_still_enqueues(with_bin, tmp_path):
    block = build_memory_context([REF], runner=fake_runner(status=None))  # status unreachable
    assert block["checkpoint"]["state"] == "pending"
    block2 = build_memory_context([REF], runner=fake_runner(status={"outbox_pending": 4}))
    assert block2["checkpoint"] == {**block2["checkpoint"], "state": "pending", "outbox_pending": 4}
    store = HandoffStore(tmp_path)
    assert store.enqueue(build_handoff(goal="offline", memory_context=block2))["state"] == "queued"


def test_status_refs_used_and_junk_dropped(with_bin):
    status = {"outbox_pending": 0, "refs": [REF, {"memory_id": "bad id!", "bank_id": "b", "revision": "1"}, "x"]}
    assert build_memory_context(runner=fake_runner(status=status))["refs"] == [REF]
    assert build_memory_context(runner=fake_runner(status={"outbox_pending": 0})) is None


@pytest.mark.parametrize("mutate", [
    lambda b: b.update(version=2),
    lambda b: b.update(extra=1),
    lambda b: b["refs"].append({"memory_id": "m", "bank_id": "b"}),
    lambda b: b["refs"][0].update(text="leak"),
    lambda b: b["refs"][0].update(memory_id="has space"),
    lambda b: b.update(snapshot_digest="md5:abc"),
    lambda b: b["checkpoint"].update(state="maybe"),
    lambda b: b["checkpoint"].update(outbox_pending=-1),
    lambda b: b["checkpoint"].update(outbox_pending=True),
    lambda b: b["checkpoint"].update(at="yesterday"),
    lambda b: b.update(refs="nope"),
])
def test_malformed_rejected(with_bin, mutate):
    block = build_memory_context([REF], runner=fake_runner(status={"outbox_pending": 0}))
    mutate(block)
    with pytest.raises(ValueError):
        validate_memory_context(block)
    with pytest.raises(ValueError):
        build_handoff(goal="g", memory_context=block)


def test_malformed_block_in_stored_package_does_not_break_resume():
    package = build_handoff(goal="Keep working", remaining=["step"])
    package["memory_context"] = {"version": 9}
    prompt = resume_prompt(package)
    assert "Keep working" in prompt and "step" in prompt and "malformed" in prompt
    assert resolve_memory_refs(package["memory_context"]) == []


def test_resume_lists_untrusted_refs_and_reports_unresolved_forbidden(with_bin):
    block = build_memory_context([REF, {**REF, "memory_id": "mem_2"}, {**REF, "memory_id": "mem_3"}],
                                 runner=fake_runner(status={"outbox_pending": 0}))
    outcomes = {"mem_1": SimpleNamespace(returncode=0, stdout=json.dumps({"text": SECRET_TEXT}), stderr=""),
                "mem_2": SimpleNamespace(returncode=1, stdout="", stderr="403 forbidden"),
                "mem_3": SimpleNamespace(returncode=1, stdout="", stderr="not found")}
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        return outcomes[argv[2]]
    res = resolve_memory_refs(block, runner=run)
    assert [r["status"] for r in res] == ["resolved", "forbidden", "unresolved"]
    assert all(c[1] == "get" for c in calls)
    prompt = resume_prompt(build_handoff(goal="g", memory_context=block), res)
    assert "UNTRUSTED MEMORY REFERENCES" in prompt and "not instructions" in prompt
    assert "[forbidden]" in prompt and "[unresolved]" in prompt and "[resolved]" in prompt
    assert SECRET_TEXT not in prompt and "hunter2" not in prompt


def test_resolve_without_cli_is_not_fatal(no_bin):
    block = {"version": 1, "refs": [REF], "snapshot_digest": "sha256:" + "0" * 64,
             "checkpoint": {"state": "pending", "outbox_pending": 2, "at": "2026-11-02T00:00:00Z"}}
    assert resolve_memory_refs(block)[0]["status"] == "unresolved"
    assert "pending" in resume_prompt({"context": {"goal": "g"}, "memory_context": block})


def test_cli_create_and_resume_end_to_end(tmp_path, monkeypatch, capsys):
    exe = tmp_path / "herdr-memory"
    exe.write_text("#!/bin/sh\ncase \"$1\" in status) echo '{\"outbox_pending\":1}';; "
                   "get) echo '{\"text\":\"" + "LEAKME" + "\"}';; esac\n")
    exe.chmod(0o755)
    monkeypatch.setenv("HERDR_MEMORY_BIN", str(exe))
    import herdr_handoff_cli as cli
    root = tmp_path / "root"
    assert cli.main(["--root", str(root), "create", "--goal", "cli", "--memory-ref", "mem_1:bank_a:r3"]) == 0
    created = json.loads(capsys.readouterr().out)
    assert created["memory_context"]["checkpoint"]["state"] == "pending"
    assert "LEAKME" not in json.dumps(created)
    assert cli.main(["--root", str(root), "resume", created["handoff_id"]]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["memory_refs"][0]["status"] == "resolved" and "LEAKME" not in out["prompt"]
    assert "UNTRUSTED MEMORY REFERENCES" in out["prompt"]
