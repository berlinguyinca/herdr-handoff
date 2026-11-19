# Changelog

## Unreleased (0.5.0)

### Changed
- Proof of life is herdr's own observation, and the docs now say so: the successor's agent is detected in a
  settled state and the handoff id is in its output. `confirm` stays as an optional, idempotent record of the
  successor's own acknowledgement and is not a condition for closing the source. The id match is stricter and
  wider: after `wait-output`, the last 200 rows of `herdr pane read --source recent` are searched too, and an id
  that appears only inside the typed launch command (echoed by the shell, wrapped or not) no longer counts.
- `takeover` and `launch` now split the successor pane **below** the source pane by default. Before, the split
  went to the right. This is intentional. Pass `takeover --direction right` for the old layout. The chosen
  direction is recorded in the takeover block as `direction`.

### Added
- `create` run inside a herdr pane now records `source.pane_id` (from `$HERDR_PANE_ID`; `--source-pane` overrides).
- Shared handoff store: with no `--root` a command now uses `HERDR_HANDOFF_STORE`, else
  `~/.local/share/herdr-handoff/store`, instead of `.herdr/handoffs` relative to the current directory (root
  order: `--root`, `HERDR_HANDOFF_FILE`, `HERDR_HANDOFF_DIR`, `HERDR_HANDOFF_STORE`, the default). The current
  repo's legacy records are imported on first use, silently on stdout, and never deleted.
  `herdr-handoff store import-legacy [LEGACY_ROOT]` imports on demand. `--root` behaves as before.
- Takeover block `direction` is now in `schemas/handoff-v2.schema.json` (the validator already accepted it).
- When `takeover --allow-dirty-source` closes a source over unsaved work, the takeover block records
  `dirty_allowed: true` and `unpushed_commits` (short ids from `git rev-list HEAD --not --remotes`). Both keys
  are optional in the schema.
- Envelope keys `predecessor_id`, `successor_id`, `source.pane_id`, `source.session_id`, `target.pane_id` and
  `target.session_id`, all optional. `create` takes `--predecessor-id`, `--source-pane` and `--source-session`.
- `takeover` records the successor's `target.pane_id` and, when herdr reports one, `target.session_id`.
  `create --predecessor-id X` sets `successor_id` on handoff X.
- `takeover --source-exit-text TEXT --source-exit-key KEY --source-exit-timeout SECONDS`: ask the source harness
  to exit and wait until `herdr pane process-info` shows no process but the pane's shell before closing the
  pane. A harness that does not exit gives the refusal status `source_exit_failed` (new takeover phase of the
  same name), and both panes stay open. Without the flags the close is direct, as before. The harness counts as
  exited only when herdr reports no agent, only an idle shell (by name) is in the foreground, or the pane is gone;
  missing data, an `exec`-started harness and non-agent programs fail closed, and nothing is typed into the latter.
