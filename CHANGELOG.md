# Changelog

## Unreleased (0.5.0)

### Changed
- `takeover` and `launch` now split the successor pane **below** the source pane by default. Before, the split
  went to the right. This is intentional. Pass `takeover --direction right` for the old layout. The chosen
  direction is recorded in the takeover block as `direction`.

### Added
- Envelope keys `predecessor_id`, `successor_id`, `source.pane_id`, `source.session_id`, `target.pane_id` and
  `target.session_id`, all optional. `create` takes `--predecessor-id`, `--source-pane` and `--source-session`.
- `takeover` records the successor's `target.pane_id` and, when herdr reports one, `target.session_id`.
  `create --predecessor-id X` sets `successor_id` on handoff X.
