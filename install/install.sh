#!/bin/sh
set -eu

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
skill_source="$repo_root/skill/herdr-handoff"
[ -f "$skill_source/SKILL.md" ] || { echo "missing skill source: $skill_source" >&2; exit 1; }

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
for dir in "$codex_dir" "$claude_dir" "$pi_dir" "$opencode_dir"; do
  destination="$dir/herdr-handoff"
  mkdir -p "$dir"
  if [ -e "$destination" ] || [ -L "$destination" ]; then
    [ -L "$destination" ] && [ "$(readlink "$destination")" = "$skill_source" ] && continue
    mv "$destination" "$destination.backup.$timestamp"
  fi
  ln -s "$skill_source" "$destination"
  echo "installed $destination"
done
