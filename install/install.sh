#!/bin/sh
set -eu

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
handoff_skill_source="$repo_root/skill/herdr-handoff"
namespace_skill_source="$repo_root/skill/herdr"
[ -f "$handoff_skill_source/SKILL.md" ] || { echo "missing skill source: $handoff_skill_source" >&2; exit 1; }
[ -f "$namespace_skill_source/SKILL.md" ] || { echo "missing skill source: $namespace_skill_source" >&2; exit 1; }

root=${HERDR_HANDOFF_SKILL_ROOT:-}
if [ -n "$root" ]; then
  codex_dir="$root/codex/skills"; claude_dir="$root/claude/skills"
  pi_dir="$root/pi/skills"; opencode_dir="$root/opencode/skills"
else
  codex_dir="${CODEX_HOME:-$HOME/.codex}/skills"
  claude_dir="${CLAUDE_SKILLS_DIR:-$HOME/.claude/skills}"
  pi_dir="${PI_SKILLS_DIR:-$HOME/.pi/agent/skills}"
  opencode_dir="${OPENCODE_SKILLS_DIR:-$HOME/.config/opencode/skills}"
fi

timestamp=$(date -u +%Y%m%dT%H%M%SZ)
install_one() {
  dir="$1"
  name="$2"
  source="$3"
  destination="$dir/$name"
  mkdir -p "$dir"
  if [ -L "$destination" ]; then
    target=$(readlink "$destination")
    [ "$target" = "$source" ] && return 0
    # A link into another checkout of *this* repo is an earlier install of the same skill: the pinned
    # revision moved to a different path, and leaving it would have every harness keep reading the old
    # skill text forever.  "this repo" is decided by that tree's own pyproject metadata, not by its
    # directory name, so an operator's own skill at a similar path stays untouched.
    if [ "$(basename "$target")" = "$name" ] && [ "$(basename "$(dirname "$target")")" = "skill" ] \
       && grep -q '^name = "herdr-handoff"$' "$(dirname "$(dirname "$target")")/pyproject.toml" 2>/dev/null
    then
      rm -f "$destination"
    else
      echo "collision: leaving existing $destination -> $target unchanged" >&2
      return 0
    fi
  elif [ -e "$destination" ]; then
    echo "collision: leaving existing $destination unchanged" >&2
    return 0
  fi
  ln -s "$source" "$destination"
  echo "installed $destination"
}

for dir in "$codex_dir" "$claude_dir" "$pi_dir" "$opencode_dir"; do
  install_one "$dir" herdr "$namespace_skill_source"
  install_one "$dir" herdr-handoff "$handoff_skill_source"
done
