#!/usr/bin/env bash
# The herdr-handoff gate, as ci.metabolomics.us runs it (.woodpecker/unit-gates.yml) and as anyone can run it from a
# clean checkout:
#
#     bash scripts/ci/unit-gates.sh
#
# Installs nothing system-wide: the CI agent has no sudo and no package manager, so the gate builds a throwaway venv
# and throws it away.  Needs python3 (3.12+), git, and PyPI reachable from the host.
#
# Four checks, ordered by how fast they can say no:
#
#   1. lint        ruff, restricted to the correctness subset -- see the note below.
#   2. data files  every tracked .json and .toml parses.  skill/herdr/commands.json and schemas/handoff-v2.schema.json
#                  are read by harnesses and by other repositories, and a file that does not parse fails in someone
#                  else's tool, not here.
#   3. markers     no unresolved merge-conflict marker in a tracked file.
#   4. tests       the whole unit suite.  The `integration` marker (real harness CLIs) is deselected by pyproject's
#                  addopts; run `pytest -m integration` on a machine that has those installed.
#
# Why the lint scope is written out instead of left to defaults.  Measured with the pinned ruff (0.14.5) and
# --isolated, on this tree:
#   ruff check --isolated src tests                  -> All checks passed   (ruff's defaults ARE E4,E7,E9,F today)
#   ruff check --isolated --select ALL src tests     -> Found 2069 errors   (666 E501, 343 S101, 297 ANN001, 168 ANN201)
# So the scope is not a concession to existing dirt: it is ruff's own default set, named so it stops being a moving
# target.  Defaults change between releases and, without an explicit scope, ruff also discovers configuration by
# walking up from the checkout, so what blocks a merge could come from a file outside this repository.  E4,E7,E9,F is
# the set that finds real defects -- undefined names, unused imports, syntax and indentation errors -- and saying it
# literally means a ruff release cannot quietly widen or narrow this gate.  Widening it deliberately means editing the
# scope here and in tests/test_ci_config.py together, so the change is visible.
# `ruff format` is not a gate: `ruff format --check src tests` reports 15 files would be reformatted, and that churn
# would bury the change it ships with.
# Correction kept in place, because the number is tempting to re-add: an earlier revision of this comment said "the
# full default set reports 53 findings, 22 of them PLW1510".  That came from a broader --select in a venv that no
# longer exists and cannot be reproduced; the commands above produce the numbers above.
# Output is journalled (scripts/ci/journal.sh): the CI agent drops the tail of a step log, and on pipeline 1 the
# unit-gates step stored nine lines ending at "== pytest ==" -- success, exit 0, with the suite's own summary lost.
set -euo pipefail
cd "$(dirname "$0")/../.."
. "$(dirname "$0")/journal.sh"; ci_journal unit-gates

# The harness reads a step's output through a pipe and stops when the step's process exits, so anything still sitting
# in a Python buffer at that instant never reaches the log.  pytest is the last command here and its whole report is
# one block-buffered flush: pipelines 1 and 2 on ci.metabolomics.us both ended their stored log at "== pytest ==" for
# a suite that passed.  Unbuffered output makes each line land as it is produced.
export PYTHONUNBUFFERED=1

need() { command -v "$1" >/dev/null 2>&1 || { echo "missing on this host: $1" >&2; exit 1; }; }
need python3
need git
python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)' \
  || { echo "needs Python 3.12 or newer, found $(python3 -V 2>&1)" >&2; exit 1; }

venv="$(mktemp -d "${TMPDIR:-/tmp}/herdr-handoff-gates-venv.XXXXXX")"
trap 'rm -rf "$venv"' EXIT
python3 -m venv "$venv"
"$venv/bin/pip" install -q --upgrade pip >/dev/null
"$venv/bin/pip" install -q -e '.[dev]'
export PATH="$venv/bin:$PATH"

echo "== ruff: $(ruff --version) =="
ruff check src tests --select E4,E7,E9,F

echo "== tracked data files =="
python3 - <<'PY'
import json
import subprocess
import sys
import tomllib
from pathlib import Path

names = subprocess.run(["git", "ls-files", "-z"], capture_output=True, check=True).stdout.decode().split("\0")
parsed, broken = 0, []
for name in names:
    if not name or not Path(name).is_file():
        continue
    loader = json.loads if name.endswith(".json") else tomllib.loads if name.endswith(".toml") else None
    if loader is None:
        continue
    try:
        loader(Path(name).read_text(encoding="utf-8"))
        parsed += 1
    except Exception as exc:
        broken.append(f"{name}: {type(exc).__name__}: {exc}")
for line in broken:
    print(line, file=sys.stderr)
if broken:
    raise SystemExit(f"{len(broken)} tracked data file(s) do not parse")
print(f"{parsed} tracked json/toml file(s) parse")
PY

echo "== conflict markers =="
# Only the two ends of a conflict marker, not the `=======` divider: an all-equals line is a legitimate heading in
# reStructuredText and Markdown, and git itself only ever writes seven of them between the two markers below.
set +e
matches=$(git grep -nE '^(<<<<<<< |>>>>>>> )' -- . 2>&1)
status=$?
set -e
case $status in
  0) echo "unresolved merge conflict marker(s):" >&2; echo "$matches" >&2; exit 1 ;;
  1) echo "no conflict markers" ;;                       # grep's "nothing matched" is the pass
  *) echo "git grep failed (exit $status): $matches" >&2; exit 1 ;;
esac

echo "== pytest =="
# Keep the run's own log and repeat its verdict after a short drain: the summary is the one line an operator needs,
# and it is the line the race costs.  Nothing is printed after it, so it cannot be the tail that is lost.
status=0
# -rs, not a bare -q: on the agents the harness CLIs and the herdr binary are absent, and a suite that is green
# because it skipped is not evidence.  The reasons have to be in the log for whoever reads the run.
python -m pytest -q -rs 2>&1 | tee "$venv/pytest.log" || status=${PIPESTATUS[0]}
tail -n 1 "$venv/pytest.log" | sed 's/^/pytest: /'
# Stay alive for a moment after the verdict, because the agent's log shipper trails the process.  Writing the verdict
# last does not protect it: pipeline 3's recap arrived and pipeline 4's identical recap did not, the only difference
# being that -rs put six more lines in front of it.  The line has to be followed by quiet, not by process exit.
sleep 3
exit "$status"
