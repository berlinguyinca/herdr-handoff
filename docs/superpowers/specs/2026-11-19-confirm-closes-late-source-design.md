# Confirm closes a late-confirmed source pane — design

Approved 2026-11-19.

## Intent
`takeover` waits `--confirm-timeout` (25 s) for the successor to prove it is live, then closes the
source pane. A Claude successor (startup, SessionStart hooks, MCP connect, reading the handoff
document) always confirms after that window, so takeover returns `launch_unconfirmed` and the source
pane stays open forever: `confirm_takeover()` only records `phase=confirmed`. The source pane should
close once the successor confirms, whenever that happens.

## Goals / non-goals / anti-goals
- Goal: `herdr-handoff confirm` closes the source pane when takeover could not, under the same gate.
- Non-goal: changing the default `--confirm-timeout`, or the in-window takeover path.
- Anti-goal: ever closing a source pane takeover would have refused (unconfirmed, other tab, same
  pane, dirty/unpushed worktree without `--allow-dirty-source`, `--keep-source`). The gate
  `decide_takeover_close` stays unchanged.

## Approaches
1. **Confirm-side close (picked).** The successor provably runs when it confirms; deterministic,
   testable, no extra process.
2. Detached waiter left by takeover — an orphan process with no owner, hard to test.
3. Longer default timeout for Claude — no correct value, still a race.

## Design
- Takeover records the options a later close needs in the takeover block: `keep_source`,
  `allow_dirty`, `source_cwd` (the explicit `--source-cwd`, if any), and the source-exit settings
  (`source_exit_text`, `source_exit_keys`, `source_exit_timeout`). Old records without them read as
  the defaults (`keep_source` absent ⇒ treated as **not** recorded ⇒ confirm does not close; see below).
  `_TAKEOVER_KEYS` / `validate_takeover` accept the new keys.
- `confirm_takeover` after recording the confirmation:
  - Tries the takeover lock without waiting. If held (takeover still running), returns the
    confirmation only; takeover's own wait sees the confirm and closes.
  - Closes only when the record shows a takeover that ended unconfirmed without closing:
    `source_pane` set, no `source_closed_at`, the new option keys present (records written by an
    older takeover are left alone), and `keep_source` false.
  - Runs the same sequence takeover runs after confirmation: source tab lookup (gone ⇒
    `already_closed`), `source_worktree_state(source_cwd or pane cwd)`, `decide_takeover_close(...,
    confirmed=True, allow_dirty=recorded)`, optional `exit_source`, `close_pane`. Ideally by
    extracting that block from `_takeover` into one shared function both call.
  - Records `phase=source_closed|already_closed` + `source_closed_at` on success, or the refusal
    reason (phase stays `confirmed`) and returns it: `source_closed: true|false`, `reason`.
  - Idempotent: a second confirm after a close returns the recorded close.
- Tests (TDD, real herdr panes and real git worktrees, no mocks): late confirm closes; refusals for
  dirty worktree, unpushed commits, other tab, keep_source, legacy record; wrong pane cannot confirm;
  repeat confirm idempotent; takeover lock held ⇒ no close.

## Delivery
herdr-handoff `fix/confirm-closes-late-source` → PR → release 0.5.1 (CHANGELOG, skill text).
herdr-engineering then on its own branch: bump pin in `herdr_engineering/handoff_dependency.py`,
update `skill_data/handoff/SKILL.md`, `docs/operations/handoff.md`, `docs/automatic-handoffs.md`;
not pushed. Refresh the host install (currently 0.4.0).

## Left to the builder
Exact helper names; how tests drive a real herdr server (follow existing `tests/e2e` and
`test_takeover.py` patterns).
