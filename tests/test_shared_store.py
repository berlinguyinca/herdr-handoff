"""The shared handoff store: one root for every repo, with per-repo `.herdr/handoffs` records imported."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from herdr_handoff import HandoffStore, build_handoff, shared_store_root

SRC = str(Path(__file__).parents[1] / "src")


def run_cli(args, *, env=None, cwd=None):
    base = {k: v for k, v in os.environ.items() if not k.startswith("HERDR_")}
    base["PYTHONPATH"] = SRC
    base.update(env or {})
    return subprocess.run([sys.executable, "-m", "herdr_handoff_cli", *args], env=base, cwd=cwd,
                          capture_output=True, text=True, timeout=120)


def make(root, goal="g"):
    done = run_cli(["--root", str(root), "create", "--goal", goal])
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)["handoff_id"]


@pytest.fixture
def world(tmp_path):
    """A fake home, a shared store, and a git repo with a legacy store; nothing touches the real home."""
    home, store, repo = tmp_path / "home", tmp_path / "store", tmp_path / "repo"
    home.mkdir()
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "sub").mkdir()
    env = {"HOME": str(home), "HERDR_HANDOFF_STORE": str(store)}
    return {"home": home, "store": store, "repo": repo, "env": env, "legacy": repo / ".herdr" / "handoffs"}


def test_shared_store_root_uses_env_then_home():
    assert shared_store_root({"HERDR_HANDOFF_STORE": "/x/y", "HOME": "/h"}) == Path("/x/y")
    assert shared_store_root({"HOME": "/h"}) == Path("/h/.local/share/herdr-handoff/store")


def test_default_root_is_shared_not_cwd_relative(world):
    done = run_cli(["create", "--goal", "g"], env=world["env"], cwd=world["repo"] / "sub")
    assert done.returncode == 0, done.stderr
    handoff_id = json.loads(done.stdout)["handoff_id"]
    assert (world["store"] / "outbox" / f"{handoff_id}.json").is_file()
    assert not (world["repo"] / "sub" / ".herdr").exists() and not world["legacy"].exists()


def test_default_root_without_store_variable_is_under_home(world):
    done = run_cli(["create", "--goal", "g"], env={"HOME": str(world["home"])}, cwd=world["repo"])
    assert done.returncode == 0, done.stderr
    handoff_id = json.loads(done.stdout)["handoff_id"]
    default = world["home"] / ".local" / "share" / "herdr-handoff" / "store"
    assert (default / "outbox" / f"{handoff_id}.json").is_file()


def test_root_precedence_flag_file_dir_store(world, tmp_path):
    flag, file_root, dir_root = tmp_path / "flag", tmp_path / "filer", tmp_path / "dirr"
    env = {**world["env"], "HERDR_HANDOFF_FILE": str(file_root / "outbox" / "x.json"),
           "HERDR_HANDOFF_DIR": str(dir_root)}
    on_flag = run_cli(["--root", str(flag), "create", "--goal", "g"], env=env, cwd=world["repo"])
    on_file = run_cli(["create", "--goal", "g"], env=env, cwd=world["repo"])
    del env["HERDR_HANDOFF_FILE"]
    on_dir = run_cli(["create", "--goal", "g"], env=env, cwd=world["repo"])
    del env["HERDR_HANDOFF_DIR"]
    on_store = run_cli(["create", "--goal", "g"], env=env, cwd=world["repo"])
    for root, done in ((flag, on_flag), (file_root, on_file), (dir_root, on_dir), (world["store"], on_store)):
        assert done.returncode == 0, done.stderr
        assert len(list((root / "outbox").glob("handoff_*.json"))) == 1, root


def test_import_legacy_copies_missing_and_skips_present(tmp_path):
    legacy, shared = tmp_path / "legacy", HandoffStore(tmp_path / "shared")
    old, both = make(legacy, "old"), make(legacy, "legacy copy")
    shared.enqueue({**build_handoff(goal="shared copy"), "handoff_id": both})
    assert shared.import_legacy(legacy) == [old]
    assert shared.get(both)["context"]["goal"] == "shared copy"          # the shared record wins, nothing duplicated
    assert shared.get(old)["context"]["goal"] == "old"
    assert sorted(p.name for p in shared.outbox.glob("*.json")) == sorted(f"{i}.json" for i in (old, both))


def test_import_is_idempotent_and_leaves_legacy_untouched(tmp_path):
    legacy, shared = tmp_path / "legacy", HandoffStore(tmp_path / "shared")
    ids = {make(legacy, "a"), make(legacy, "b")}
    before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in (legacy / "outbox").iterdir()}
    assert set(shared.import_legacy(legacy)) == ids
    assert shared.import_legacy(legacy) == []
    assert {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in (legacy / "outbox").iterdir()} == before
    assert sorted(p.name for p in shared.outbox.iterdir()) == sorted(f"{i}.json" for i in ids)  # no temp files


def test_import_from_a_missing_legacy_root_is_empty(tmp_path):
    shared = HandoffStore(tmp_path / "shared")
    assert shared.import_legacy(tmp_path / "nope") == []
    assert not (tmp_path / "nope").exists()


def test_corrupt_legacy_record_is_skipped_and_reported(tmp_path):
    legacy, shared = tmp_path / "legacy", HandoffStore(tmp_path / "shared")
    good = make(legacy, "good")
    bad = "handoff_" + "0" * 32
    (legacy / "outbox" / f"{bad}.json").write_text("{not json")
    liar = "handoff_" + "1" * 32
    (legacy / "outbox" / f"{liar}.json").write_text(json.dumps({"handoff_id": "handoff_" + "2" * 32}))
    assert shared.import_legacy(legacy) == [good]
    assert sorted(name for name, _ in shared.skipped_legacy) == sorted([f"{bad}.json", f"{liar}.json"])
    assert not (shared.outbox / f"{bad}.json").exists()


def test_legacy_record_resolves_by_id_and_import_is_silent_on_stdout(world):
    old = make(world["legacy"], "from the repo")
    done = run_cli(["inspect", old], env=world["env"], cwd=world["repo"] / "sub")
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout)["context"]["goal"] == "from the repo"       # stdout stays machine-readable
    assert old in done.stderr                                       # the import is reported on stderr
    assert (world["store"] / "outbox" / f"{old}.json").is_file()
    assert (world["legacy"] / "outbox" / f"{old}.json").is_file()   # never deleted
    listed = run_cli(["list"], env=world["env"], cwd=world["repo"])
    assert [p["handoff_id"] for p in json.loads(listed.stdout)] == [old]
    assert old not in listed.stderr                                 # already imported: nothing to report


def test_outside_a_repo_the_cwd_legacy_store_is_imported(world, tmp_path):
    plain = tmp_path / "plain"
    old = make(plain / ".herdr" / "handoffs", "no repo")
    done = run_cli(["inspect", old], env=world["env"], cwd=plain)
    assert done.returncode == 0 and json.loads(done.stdout)["context"]["goal"] == "no repo"


def test_explicit_root_never_auto_imports(world):
    make(world["legacy"], "legacy")
    explicit = world["store"].parent / "explicit"
    assert run_cli(["--root", str(explicit), "list"], env=world["env"], cwd=world["repo"]).stdout.strip() == "[]"
    assert not world["store"].exists()


def test_documents_and_confirm_paths_point_into_the_root_that_holds_the_record(world):
    old = make(world["legacy"], "x")
    done = run_cli(["resume", old], env=world["env"], cwd=world["repo"])
    assert done.returncode == 0, done.stderr
    document = Path(json.loads(done.stdout)["document"])
    assert document.parent == (world["store"] / "documents").resolve()
    assert f"--root {world['store'].resolve()}" in document.read_text()


def test_store_import_legacy_command_prints_ids_and_skips(world, tmp_path):
    other = tmp_path / "other-legacy"
    good = make(other, "g")
    (other / "outbox" / f"handoff_{'0' * 32}.json").write_text("[]")
    done = run_cli(["store", "import-legacy", str(other)], env=world["env"], cwd=world["repo"])
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    assert out["imported"] == [good] and [s["file"] for s in out["skipped"]] == [f"handoff_{'0' * 32}.json"]
    assert json.loads(run_cli(["store", "import-legacy", str(other)], env=world["env"]).stdout)["imported"] == []


def test_store_import_legacy_defaults_to_the_current_repo(world):
    old = make(world["legacy"], "repo")
    done = run_cli(["store", "import-legacy"], env=world["env"], cwd=world["repo"] / "sub")
    assert json.loads(done.stdout)["imported"] == [old]


RACE = '''
import sys, json
sys.path.insert(0, {src!r})
from pathlib import Path
from herdr_handoff import HandoffStore
shared, legacy, role = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
store = HandoffStore(shared)
if role == "import":
    for _ in range(3):
        store.import_legacy(legacy)
else:
    for path in sorted((legacy / "outbox").glob("handoff_*.json")):
        record = json.loads(path.read_text())
        record["context"]["goal"] = "updated in the shared store"
        store._write(store.outbox / path.name, record)
'''


def test_an_import_never_overwrites_a_record_updated_in_the_shared_store_meanwhile(tmp_path):
    legacy = tmp_path / "legacy"
    for n in range(250):
        make(legacy, f"legacy {n}")
    script = tmp_path / "race.py"
    script.write_text(RACE.format(src=SRC))
    shared = tmp_path / "shared"
    HandoffStore(shared)
    procs = [subprocess.Popen([sys.executable, str(script), str(shared), str(legacy), role])
             for role in ("import", "update")]
    assert [p.wait(timeout=120) for p in procs] == [0, 0]
    goals = {json.loads(p.read_text())["context"]["goal"] for p in (shared / "outbox").glob("handoff_*.json")}
    assert goals == {"updated in the shared store"}                  # no update was lost to a stale copy
    assert not [p for p in (shared / "outbox").iterdir() if not p.name.startswith("handoff_")]


def test_a_failed_import_leaves_no_temp_file_behind(tmp_path):
    legacy, shared = tmp_path / "legacy", HandoffStore(tmp_path / "shared")
    make(legacy, "a")
    (legacy / "outbox" / f"handoff_{'f' * 32}.json").write_text("{bad")
    shared.import_legacy(legacy)
    assert all(p.name.startswith("handoff_") for p in shared.outbox.iterdir())


def test_shared_store_root_expands_tilde_from_home_only():
    assert shared_store_root({"HERDR_HANDOFF_STORE": "~/s", "HOME": "/h"}) == Path("/h/s")
    assert shared_store_root({"HERDR_HANDOFF_STORE": "~", "HOME": "/h"}) == Path("/h")
    assert shared_store_root({"HERDR_HANDOFF_STORE": "~bob/s", "HOME": "/h"}) == Path("~bob/s")


def test_the_legacy_note_and_corrupt_report_appear_once_per_repo_on_stderr(world):
    old = make(world["legacy"], "x")
    (world["legacy"] / "outbox" / f"handoff_{'0' * 32}.json").write_text("{bad")
    first = run_cli(["list"], env=world["env"], cwd=world["repo"])
    second = run_cli(["list"], env=world["env"], cwd=world["repo"])
    assert json.loads(first.stdout) and json.loads(second.stdout)      # stdout is only ever the JSON
    assert "--root" in first.stderr and "skipped legacy record" in first.stderr
    assert second.stderr == ""
    assert old in first.stderr


def test_store_import_legacy_reports_an_os_error_cleanly(world, tmp_path):
    legacy = tmp_path / "legacy"
    make(legacy, "x")
    HandoffStore(world["store"])
    (world["store"] / "outbox").chmod(0o500)          # the shared store cannot take a new record
    try:
        done = run_cli(["store", "import-legacy", str(legacy)], env=world["env"], cwd=world["repo"])
    finally:
        (world["store"] / "outbox").chmod(0o755)
    assert done.returncode == 1 and "Traceback" not in done.stderr and "legacy import failed" in done.stderr
