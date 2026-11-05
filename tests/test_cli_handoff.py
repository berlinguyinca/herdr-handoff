import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import HARNESSES, wait_for_json

SRC = str(Path(__file__).parents[1] / "src")


def run_cli(args, *, env=None, cwd=None, stdin=None):
    base = {k: v for k, v in os.environ.items() if not k.startswith("HERDR_")}
    base["PYTHONPATH"] = SRC
    base.update(env or {})
    return subprocess.run([sys.executable, "-m", "herdr_handoff_cli", *args], env=base, cwd=cwd,
                          input=stdin, capture_output=True, text=True, timeout=120)


def create(root, *extra, env=None, stdin=None, target="claude"):
    done = run_cli(["--root", str(root), "create", "--goal", "Ship it", "--source-harness", "pi",
                    "--target-harness", target, *extra], env=env, stdin=stdin)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_create_accepts_every_context_flag(tmp_path):
    package = create(
        tmp_path, "--completed", "a", "--remaining", "b", "--decision", "d1", "--decision", "d2",
        "--risk", "r", "--validation", "pytest: ok", "--constraint", "no deploy", "--next-command", "make test",
        "--file", "/abs/f.py", "--pr", "https://github.com/o/r/pull/1", "--resume-point", "step 3",
        "--unverified", "ci", "--repo", "o/r", "--branch", "feat/x", "--worktree", "/abs/wt",
        "--report", "/abs/report.md")
    ctx = package["context"]
    assert (ctx["decisions"], ctx["risks"], ctx["validation"]) == (["d1", "d2"], ["r"], ["pytest: ok"])
    assert ctx["constraints"] == ["no deploy"] and ctx["next_commands"] == ["make test"]
    assert ctx["resume_point"] == "step 3" and ctx["unverified"] == ["ci"] and ctx["files"] == ["/abs/f.py"]
    assert package["workspace"] == {"repo": "o/r", "branch": "feat/x", "worktree": "/abs/wt"}
    assert package["links"]["pull_requests"] == ["https://github.com/o/r/pull/1"]
    assert package["report"]["path"] == "/abs/report.md" and package["schema_version"] == 2
    assert package["state"] == "queued"


def test_from_json_on_stdin_merges_with_flags(tmp_path):
    spec = json.dumps({"decisions": ["from json"], "constraints": ["no deploy"], "workspace": {"branch": "b"}})
    package = create(tmp_path, "--from-json", "-", "--decision", "from flag", stdin=spec)
    assert package["context"]["decisions"] == ["from json", "from flag"]
    assert package["workspace"] == {"branch": "b"}


def test_from_json_rejects_unknown_keys_and_bad_types(tmp_path):
    done = run_cli(["--root", str(tmp_path), "create", "--goal", "g", "--from-json", "-"],
                   stdin=json.dumps({"bogus": 1}))
    assert done.returncode != 0 and "bogus" in done.stderr
    done = run_cli(["--root", str(tmp_path), "create", "--goal", "g", "--from-json", "-"],
                   stdin=json.dumps({"constraints": "string"}))
    assert done.returncode != 0 and "constraints" in done.stderr


def test_invalid_envelope_is_refused_and_nothing_is_queued(tmp_path):
    done = run_cli(["--root", str(tmp_path), "create", "--goal", "g", "--pr", "javascript:x"])
    assert done.returncode != 0 and "pull_requests" in done.stderr
    assert not list((tmp_path / "outbox").glob("*.json"))


def test_resume_prints_prompt_and_document_path(tmp_path):
    package = create(tmp_path, "--constraint", "no deploy", "--resume-point", "step 3")
    done = run_cli(["--root", str(tmp_path), "resume", package["handoff_id"]])
    out = json.loads(done.stdout)
    assert "no deploy" in out["prompt"] and "step 3" in out["prompt"]
    assert Path(out["document"]).is_absolute() and Path(out["document"]).is_file()
    assert out["document"] in out["prompt"]


# --- launch without a pane and without herdr: print the command, keep the handoff resumable -----------

def test_launch_without_herdr_prints_the_exact_command_and_stays_queued(tmp_path):
    package = create(tmp_path, "--constraint", "no deploy", target="codex", env={})
    no_herdr = tmp_path / "empty-bin"
    no_herdr.mkdir()
    done = run_cli(["--root", str(tmp_path), "launch", package["handoff_id"], "--cwd", str(tmp_path)],
                   env={"PATH": str(no_herdr)})
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    assert out["status"] == "manual_launch_required" and out["reason"]
    assert f"HERDR_HANDOFF_ID={package['handoff_id']}" in out["command"]
    assert out["command"].split("codex")[0].startswith("env ") and "codex " in out["command"]
    assert Path(out["document"]).is_file() and out["document"] in out["command"]
    assert f"herdr-handoff --root {tmp_path} resume {package['handoff_id']}" in out["resume_command"]
    stored = json.loads((tmp_path / "outbox" / f"{package['handoff_id']}.json").read_text())
    assert stored["state"] == "queued"                      # nothing started, so nothing is claimed
    resumed = run_cli(["--root", str(tmp_path), "resume", package["handoff_id"]])
    assert resumed.returncode == 0 and "no deploy" in json.loads(resumed.stdout)["prompt"]


def test_launch_with_herdr_installed_but_no_server_falls_back(tmp_path):
    import shutil
    if not shutil.which("herdr"):
        pytest.skip("herdr binary not installed")
    package = create(tmp_path, target="pi")
    done = run_cli(["--root", str(tmp_path), "launch", package["handoff_id"]],
                   env={"HERDR_SOCKET_PATH": str(tmp_path / "nobody-home.sock")})
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout)["status"] == "manual_launch_required"


def test_launch_refuses_a_handoff_that_is_not_queued(tmp_path):
    package = create(tmp_path)
    run_cli(["--root", str(tmp_path), "transition", package["handoff_id"], "cancelled", "--actor", "t"])
    done = run_cli(["--root", str(tmp_path), "launch", package["handoff_id"]])
    assert done.returncode != 0 and "queued" in done.stderr


# --- launch with a real herdr server ------------------------------------------------------------------

@pytest.mark.parametrize("harness", HARNESSES)
def test_no_pane_creates_a_workspace_and_starts_the_real_harness_argv(herdr_server, tmp_path, harness):
    env = herdr_server["env"]
    work = tmp_path / "work dir"
    work.mkdir()
    package = create(tmp_path / "root", "--constraint", "no deploy", "--model", "provider/model-x",
                     env={k: env[k] for k in ("HOME", "HERDR_SOCKET_PATH", "PATH")}, target=harness)
    done = run_cli(["--root", str(tmp_path / "root"), "launch", package["handoff_id"], "--cwd", str(work)],
                   env={k: env[k] for k in ("HOME", "HERDR_SOCKET_PATH", "PATH")})
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    assert out["status"] == "interactive_started" and out["placement"] == "workspace"
    seen = wait_for_json(herdr_server["probe"] / f"{harness}.argv.json")
    assert seen["handoff_id"] == package["handoff_id"]
    assert seen["handoff_file"].endswith(f"{package['handoff_id']}.json") and Path(seen["handoff_file"]).is_file()
    assert os.path.realpath(seen["cwd"]) == os.path.realpath(work)
    argv = seen["argv"]
    document = str(Path(out["document"]))
    assert "--model" in argv and argv[argv.index("--model") + 1] == "provider/model-x"
    prompt = argv[argv.index("--prompt") + 1] if harness == "opencode" else argv[-1]
    assert document in prompt and package["handoff_id"] in prompt and "\n" not in prompt
    stored = json.loads((tmp_path / "root" / "outbox" / f"{package['handoff_id']}.json").read_text())
    assert stored["state"] == "in_progress"


def test_no_pane_reuses_the_workspace_by_adding_a_tab(herdr_server, tmp_path):
    env = {k: herdr_server["env"][k] for k in ("HOME", "HERDR_SOCKET_PATH", "PATH")}
    herdr = herdr_server["herdr"]
    full = {**os.environ, **env}
    subprocess.run([herdr, "workspace", "create", "--cwd", str(tmp_path), "--label", "existing"],
                   env=full, check=True, capture_output=True)
    before = json.loads(subprocess.run([herdr, "tab", "list"], env=full, capture_output=True, text=True).stdout)
    package = create(tmp_path / "root", env=env, target="claude")
    done = run_cli(["--root", str(tmp_path / "root"), "launch", package["handoff_id"], "--cwd", str(tmp_path)], env=env)
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    assert out["placement"] == "tab"
    after = json.loads(subprocess.run([herdr, "tab", "list"], env=full, capture_output=True, text=True).stdout)
    assert len(after["result"]["tabs"]) == len(before["result"]["tabs"]) + 1
    wait_for_json(herdr_server["probe"] / "claude.argv.json")


def test_with_a_pane_it_still_splits_that_pane(herdr_server, tmp_path):
    env = {k: herdr_server["env"][k] for k in ("HOME", "HERDR_SOCKET_PATH", "PATH")}
    full = {**os.environ, **env}
    ws = json.loads(subprocess.run([herdr_server["herdr"], "workspace", "create", "--cwd", str(tmp_path)],
                                   env=full, capture_output=True, text=True).stdout)
    parent = ws["result"]["root_pane"]["pane_id"]
    package = create(tmp_path / "root", env=env, target="codex")
    done = run_cli(["--root", str(tmp_path / "root"), "launch", package["handoff_id"], "--cwd", str(tmp_path)],
                   env={**env, "HERDR_PANE_ID": parent})
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    assert out["placement"] == "split" and out["pane_id"] != parent
    assert wait_for_json(herdr_server["probe"] / "codex.argv.json")["handoff_id"] == package["handoff_id"]
