import json
from pathlib import Path

import pytest

from herdr_handoff import build_handoff, validate_envelope
from test_cli_handoff import run_cli

PRED = "handoff_" + "a" * 32
SCHEMA = Path(__file__).resolve().parents[1] / "schemas" / "handoff-v2.schema.json"


def test_build_handoff_records_predecessor_and_pane_ids():
    package = build_handoff(goal="g", predecessor_id=PRED, source_pane_id="w1-2", source_session_id="sess-1")
    assert package["schema_version"] == 2
    assert package["predecessor_id"] == PRED
    assert package["source"]["pane_id"] == "w1-2"
    assert package["source"]["session_id"] == "sess-1"
    validate_envelope(package)


def test_old_record_without_new_keys_still_validates():
    package = build_handoff(goal="old")
    assert "predecessor_id" not in package and "successor_id" not in package
    assert "pane_id" not in package["source"] and "session_id" not in package["target"]
    validate_envelope(package)


@pytest.mark.parametrize("key", ["predecessor_id", "successor_id"])
@pytest.mark.parametrize("bad", ["handoff_123", "nope", "handoff_" + "A" * 32, 7])
def test_predecessor_id_must_match_handoff_pattern(key, bad):
    with pytest.raises(ValueError, match=key):
        validate_envelope({**build_handoff(goal="g"), key: bad})
    if key == "predecessor_id":
        with pytest.raises(ValueError, match=key):
            build_handoff(goal="g", predecessor_id=bad)


def test_successor_and_target_ids_validate():
    package = {**build_handoff(goal="g"), "successor_id": PRED}
    package["target"] = {**package["target"], "pane_id": "w1-3", "session_id": "sess-2"}
    validate_envelope(package)


def test_bad_pane_id_is_rejected():
    with pytest.raises(ValueError, match="source.pane_id"):
        build_handoff(goal="g", source_pane_id="not a pane!")


def test_dangling_predecessor_validates():
    # validation never looks the id up in a store: nothing exists under this id anywhere
    validate_envelope(build_handoff(goal="g", predecessor_id="handoff_" + "0" * 32))


def test_json_schema_accepts_new_keys_and_rejects_bad_pattern():
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(SCHEMA.read_text())
    package = build_handoff(goal="g", predecessor_id=PRED, source_pane_id="w1-2", source_session_id="s")
    package["successor_id"] = PRED
    jsonschema.validate(package, schema)
    jsonschema.validate(build_handoff(goal="old"), schema)
    for key in ("predecessor_id", "successor_id"):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate({**package, key: "handoff_short"}, schema)


def test_create_cli_records_ids(tmp_path):
    result = run_cli(["--root", str(tmp_path), "create", "--goal", "g", "--predecessor-id", PRED,
                      "--source-pane", "w1-2", "--source-session", "sess-1"])
    assert result.returncode == 0, result.stderr
    package = json.loads(result.stdout)
    assert package["predecessor_id"] == PRED
    assert package["source"]["pane_id"] == "w1-2" and package["source"]["session_id"] == "sess-1"
