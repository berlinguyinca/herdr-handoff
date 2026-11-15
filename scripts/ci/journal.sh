# Source from a gate script:   . "$(dirname "$0")/journal.sh"; ci_journal <gate-name>
#
# Mirrors everything the gate prints into a journal file.  This exists because the CI agent drops the tail of a step
# log: on the first run of this gate (ci.metabolomics.us berlinguyinca/herdr-handoff, pipeline 1) the unit-gates step
# reported `success exit 0` and its stored log ended at "== pytest ==" -- the suite had run and passed, and the server
# kept nine lines that did not include the summary.  A gate whose passing output you cannot read cannot be
# investigated, only trusted.
#
# In CI the journal goes to the first writable directory named by scripts/ci/log-archive.yml, written as the gate goes,
# so even a killed step leaves its last lines on disk; anywhere else, or with none usable, it is a node-local temp
# file.  The first line the gate prints names the file, and when no archive directory was usable it says on which
# host and why.  Ported from the same helper in herdr-engineering (scripts/ci/journal.sh).
ci_journal() {
  local gate="$1" cfg dir="" candidate target why=""
  cfg="$(dirname "${BASH_SOURCE[0]}")/log-archive.yml"
  if [ -n "${CI_PIPELINE_NUMBER:-}" ] && [ -r "$cfg" ]; then
    while IFS= read -r candidate; do
      candidate="${candidate#*- }"
      if [ -d "$(dirname "$candidate")" ] && mkdir -p "$candidate" 2>/dev/null && [ -w "$candidate" ]; then dir="$candidate"; break; fi
      why="$why $candidate (parent missing or not writable)"
    done < <(grep -E '^[[:space:]]+- /' "$cfg")
  fi
  if [ -n "$dir" ]; then
    mkdir -p "$dir/${CI_REPO//\//_}" 2>/dev/null || true
    target="$dir/${CI_REPO//\//_}/${CI_PIPELINE_NUMBER}-${gate}.log"
  else
    target="${TMPDIR:-/tmp}/herdr-handoff-${gate}-$$.log"
  fi
  echo "journal: $target"
  [ -z "$dir" ] && [ -n "${CI_PIPELINE_NUMBER:-}" ] && echo "log archive not used on $(hostname):${why:- no directory configured}"
  exec > >(tee "$target") 2>&1
}
