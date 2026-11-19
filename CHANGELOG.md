# Changelog

## Unreleased (0.5.0)

### Changed
- `takeover` and `launch` now split the successor pane **below** the source pane by default. Before, the split
  went to the right. This is intentional. Pass `takeover --direction right` for the old layout. The chosen
  direction is recorded in the takeover block as `direction`.

### Added
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
  same name), and both panes stay open. Without the flags the close is direct, as before.
