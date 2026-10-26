#!/bin/sh
set -eu

root=${HERDR_HANDOFF_SKILL_ROOT:-}
if [ -n "$root" ]; then
  dirs="$root/codex/skills $root/claude/skills $root/pi/skills $root/opencode/skills"
else
  dirs="${CODEX_HOME:-$HOME/.codex}/skills ${CLAUDE_SKILLS_DIR:-$HOME/.claude/skills} ${PI_SKILLS_DIR:-$HOME/.pi/agent/skills} ${OPENCODE_SKILLS_DIR:-$HOME/.config/opencode/skills}"
fi
for dir in $dirs; do
  destination="$dir/herdr-handoff"
  [ -L "$destination" ] && rm "$destination" && echo "removed $destination"
done
