# Changelog

## 0.5.0 - 2026-11-18

### Changed
- One takeover per handoff: a takeover holds an exclusive `flock` on `<outbox>/<id>.takeover.lock`, and a second
  one waits five seconds and refuses with the new status `takeover_in_progress` (exit 1). Each update of a record
  (takeover phases, transitions, `link_successor`, `confirm`) now runs under a per-record lock
  (`<outbox>/<id>.lock`) so two processes lose no update, and records are written through a unique, fsynced
  temp file. A record left in phase `launching` with no child pane is refused with `launch_state_unknown`
  unless `takeover --relaunch` is passed. `confirm` before a successor pane is recorded is refused. A predecessor
  that `create --predecessor-id` cannot link (missing or unreadable) is reported on stderr and in a `warnings`
  field of the JSON instead of being ignored.
- `list` (and `sync`, which iterates records) skip a record that does not parse, name it on stderr and exit 0.
- Handoffs are no longer stored under `.herdr/handoffs` in the current repo by default. With no `--root` they go
  to one shared store (`HERDR_HANDOFF_STORE`, else `~/.local/share/herdr-handoff/store`), so a repo's earlier
  handoffs are not found where they were written until they are imported: the first command in a repo copies
  that repo's `.herdr/handoffs` records into the shared store, once, and says so on stderr. The old files are
  never changed or deleted, and a copy edited there afterwards is not synced. `--root` restores the old
  behaviour.
- Proof of life is herdr's own observation, and the docs now say so: the successor's agent is detected in a
  settled state and the handoff id is in its output. `confirm` stays as an optional, idempotent record of the
  successor's own acknowledgement and is not a condition for closing the source. The id match is stricter and
  wider: after `wait-output`, the last 200 rows of `herdr pane read --source recent` are searched too, and an id
  that appears only as an echo of the typed launch command (behind a prompt, redrawn, wrapped, or boxed with
  a border on every row) no longer counts, the comparison being on letters and digits only; a bare id alone
  is not proof; the successor's own words around the id do.
- `takeover` and `launch` now split the successor pane **below** the source pane by default. Before, the split
  went to the right. This is intentional. Pass `takeover --direction right` for the old layout. A direction
  other than the default is recorded in the takeover block as `direction`; the default is left out.
- Successor prompts say that herdr's takeover closes the replaced pane once herdr observes the successor running,
  unless the close is refused, rather than that the pane closes "by itself".
- A legacy record on a filesystem without hard links is imported by an exclusive create; a record that cannot be
  written is reported on every command (and not marked as reported) until it can.

### Compatibility
- A 0.4.0 binary rejects records that carry the new takeover phases or keys: upgrade all hosts that share a store
  together.
- `list` now spans every repo in the shared store (`~/.local/share/herdr-handoff/store`), not just the current one.
- A script that reads `.herdr/handoffs/outbox/<id>.json` must pass `--root` or read the shared store instead.
- A record is copied once on import; a record updated in either place afterwards is not synced back.

### Added
- `create --fallback` records `provenance.fallback: true` (optional boolean in the schema and validator; shown in
  the document as "Written by the fallback writer").
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

### Fixed
- A harness is read as exited only if the pane's shell also has no child process, so a harness suspended with
  Ctrl-Z or sent to the background is a live session. `source_exit_failed` evidence lists the child names.
- `herdr agent get` answering `pane_not_found` also means the source pane is gone.
