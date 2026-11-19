"""`create --fallback` provenance, and a `list` that survives one corrupt record."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from herdr_handoff import build_handoff, render_handoff_markdown, validate_envelope
from test_cli_handoff import create, run_cli

SCHEMA = Path(__file__).resolve().parents[1] / "schemas" / "handoff-v2.schema.json"


def test_a_fallback_record_says_so_and_validates(tmp_path):
    jsonschema = pytest.importorskip("jsonschema")
    package = create(tmp_path / "root", "--fallback")
    assert package["provenance"] == {"fallback": True}
    validate_envelope(package)
    jsonschema.validate(package, json.loads(SCHEMA.read_text()))
    assert "Written by the fallback writer" in render_handoff_markdown(package)


def test_a_normal_record_carries_no_fallback_mark(tmp_path):
    package = create(tmp_path / "root")
    assert "fallback" not in package["provenance"]
    assert "fallback writer" not in render_handoff_markdown(package)


def test_the_fallback_mark_must_be_a_boolean():
    package = build_handoff(goal="g")
    package["provenance"] = {"fallback": "yes"}
    with pytest.raises(ValueError, match="fallback"):
        validate_envelope(package)


def test_list_skips_a_corrupt_record_names_it_on_stderr_and_exits_zero(tmp_path):
    root = tmp_path / "root"
    good = create(root)
    bad = root / "outbox" / ("handoff_" + "b" * 32 + ".json")
    bad.write_text("{ this is not json", encoding="utf-8")
    done = run_cli(["--root", str(root), "list"])
    assert done.returncode == 0, done.stderr
    assert [item["handoff_id"] for item in json.loads(done.stdout)] == [good["handoff_id"]]
    assert bad.name in done.stderr and "skipp" in done.stderr
    # the corrupt file is left alone for someone to look at
    assert bad.read_text(encoding="utf-8") == "{ this is not json"


def test_list_with_only_clean_records_prints_nothing_on_stderr(tmp_path):
    root = tmp_path / "root"
    create(root)
    done = run_cli(["--root", str(root), "list"])
    assert done.returncode == 0 and done.stderr == ""
