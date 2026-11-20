# Confirm Closes a Late-Confirmed Source Pane — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `herdr-handoff confirm` closes the source pane when takeover gave up waiting, under the unchanged close gate.

**Architecture:** Takeover records its close options in the takeover block (`close_options`). The post-confirmation close sequence in `_takeover` is extracted into one function, `_close_source`, which both takeover and `confirm_takeover` call. Confirm runs it only when it can take the takeover lock without waiting and the record says a close is wanted.

**Tech Stack:** Python 3.12, pytest, a real headless herdr server (`tests/conftest.py` fixtures), real git repos (`pushed_repo`).

**Spec:** `docs/superpowers/specs/2026-11-19-confirm-closes-late-source-design.md`

## Global Constraints
- `decide_takeover_close` (`src/herdr_handoff.py:862`) is not changed.
- No mocks, stubs or fakes: integration tests use `herdr_server` / `live_herdr_server` and real git repos. Editing the outbox JSON on disk to build a legacy or other-tab record is allowed (it is real state, not a fake).
- Integration tests carry `@pytest.mark.integration` (CI skips them; run them locally where `herdr` is installed).
- `confirm` exits 0 whenever the confirmation is recorded, whether or not the close happened; the close outcome is in the JSON.
- Release version `0.5.1`. Commit trailer: `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Lint: `ruff check src tests` must stay clean; the full gate is `bash scripts/ci/unit-gates.sh`.

## Review Focus
1. A takeover rerun **without** `--keep-source` after one **with** it must overwrite `close_options.keep_source` to false (the existing test `test_takeover_finishes_a_record_the_launcher_left_confirmed_but_unclosed` must keep passing, and a confirm after that rerun's unconfirmed end must close). Task 2.
2. Confirm while a takeover holds the lock must not close and must not block more than a moment. Task 3.
3. A confirm from a pane other than the recorded child still exits with the existing error and closes nothing. Task 3.
4. A source pane that is already gone when confirm runs reports `already_closed`, not an error. Task 3.
5. A record written by takeover 0.5.0 (no `close_options`) is never closed by confirm. Task 3.

---

### Task 1: Record close options in the takeover block

**Files:**
- Modify: `src/herdr_handoff.py:666-700` (`_TAKEOVER_KEYS`, `validate_takeover`)
- Modify: `schemas/handoff-v2.schema.json` (takeover block, if it restricts keys)
- Test: `tests/test_takeover.py` (unit section)

**Interfaces:**
- Produces: takeover block key `close_options`: `{"keep_source": bool, "allow_dirty": bool, "source_cwd": str | None, "source_exit_text": str | None, "source_exit_keys": list[str], "source_exit_timeout": float}`. `validate_takeover` accepts it and raises `ValueError` for a non-dict, an unknown sub-key, or a wrong type.

- [ ] **Step 1: Write failing tests** `test_the_takeover_block_accepts_close_options` (a full valid `close_options` round-trips through `validate_takeover`) and `test_the_takeover_block_rejects_bad_close_options` (each of: `close_options="x"`, unknown sub-key `{"force": True}`, `keep_source="yes"`, `source_exit_keys="Enter"` raises `ValueError`). Also, if `tests/test_envelope_v2.py` validates against the schema, add a case that a package whose takeover block holds `close_options` validates.
- [ ] **Step 2: Run** `pytest tests/test_takeover.py -k close_options -v` → FAIL (unsupported key).
- [ ] **Step 3: Implement** add `"close_options"` to `_TAKEOVER_KEYS` and its validation in `validate_takeover`; mirror it in the schema if the schema lists takeover properties.
- [ ] **Step 4: Run** `pytest tests -q -m "not integration"` → PASS.
- [ ] **Step 5: Commit** `feat: takeover block records its close options`

### Task 2: Extract `_close_source` and have takeover record `close_options`

**Files:**
- Modify: `src/herdr_handoff_cli.py:549-746` (`_takeover`)
- Test: `tests/test_takeover.py` (integration section)

**Interfaces:**
- Consumes: `close_options` (Task 1).
- Produces: `_close_source(store, handoff_id, *, block: dict, source_pane: str | None, child_pane: str, tab_id: str | None, placement: str, options: dict) -> dict` returning `{"status": "source_closed" | "source_dirty" | "launch_unconfirmed" | "source_exit_failed" | "source_close_failed", "source_closed": bool, "reason": str, "block": dict, "worktree": dict | None}`. It is exactly the code after `if keep_source:` in `_takeover` (source tab lookup → worktree guard → `decide_takeover_close` → optional `exit_source` → `close_pane` → record phase), moved, with `options` supplying `source_cwd`, `allow_dirty` and the source-exit settings. Records it writes are unchanged.

- [ ] **Step 1: Write failing integration test** `test_takeover_records_its_close_options` (`live_herdr_server`, run `takeover_args(...)` plus `--keep-source --source-exit-text /exit --source-exit-key Enter`; assert `source_state(...)["takeover"]["close_options"] == {"keep_source": True, "allow_dirty": False, "source_cwd": None, "source_exit_text": "/exit", "source_exit_keys": ["Enter"], "source_exit_timeout": 30.0}`), and `test_a_takeover_rerun_without_keep_source_overwrites_the_recorded_option` (first run `--keep-source`, second run without; assert `close_options["keep_source"] is False` after the second).
- [ ] **Step 2: Run** `pytest tests/test_takeover.py -k "close_options or overwrites" -v` → FAIL.
- [ ] **Step 3: Implement** in `_takeover`, write `close_options` into the record on every run once the pane is known (the `launched` record for a new pane; a separate `record_takeover` for a rerun with an existing child), before the confirmation wait. Move the post-confirm block into `_close_source` and call it from `_takeover`, mapping its result onto `report(...)` as today.
- [ ] **Step 4: Run** `pytest tests -q` (integration included, locally) → all PASS, existing takeover tests unchanged.
- [ ] **Step 5: Commit** `refactor: takeover closes the source through _close_source and records its options`

### Task 3: `confirm` closes a late-confirmed source

**Files:**
- Modify: `src/herdr_handoff_cli.py:749-768` (`confirm_takeover`)
- Test: create `tests/test_confirm_close.py`

**Interfaces:**
- Consumes: `_close_source`, `close_options`, `HandoffStore.takeover_lock(handoff_id, wait=0)`, `TakeoverInProgress`.
- Produces: confirm JSON gains `"close": "source_closed" | "already_closed" | "refused" | "not_attempted"`, `"source_closed": bool`, `"source_closed_at": str | None`, `"reason": str` (`""` when closed). Existing keys stay.

Close rules, in order, after recording the confirmation (and only when not already closed — the existing idempotent branch also gains `close`/`source_closed` keys):
1. Take `store.takeover_lock(handoff_id, wait=0)`; on `TakeoverInProgress` → `not_attempted`, reason `"a takeover of this handoff is running; it closes the source"`.
2. Re-read the record under the lock; if `source_closed_at` is now set → return it (`source_closed`/`already_closed` per phase).
3. No `close_options` → `not_attempted`, reason `"recorded by a takeover that predates confirm-side close"`.
4. `close_options.keep_source` → `not_attempted`, reason `"takeover was run with --keep-source"`.
5. No `source_pane` → `not_attempted`, reason `"no source pane is recorded"`.
6. Otherwise call `_close_source(..., placement=recorded["placement"], tab_id=recorded["tab_id"], options=close_options)`; status `source_closed` → `close` = `already_closed` when the record's phase is `already_closed` else `source_closed`; any other status → `refused` with its reason.

Test scaffold for every integration test: `herdr_server` fixture (recorder harness exits at once, so takeover with `--confirm-timeout 3` ends `launch_unconfirmed`), source pane from `herdr workspace create --cwd <dir>`, then `run_cli(["--root", root, "confirm", id], env={**env, "HERDR_PANE_ID": child})`. Reuse `create`, `panes`, `run_cli`, `source_state` from `test_takeover`.

- [ ] **Step 1: Write failing tests** in `tests/test_confirm_close.py`:
  - `test_a_late_confirm_closes_the_source_pane` — cwd `tmp_path` (not a repo); after confirm: exit 0, `close == "source_closed"`, `source_closed is True`, `source_closed_at` set, source not in `panes(...)`, record phase `source_closed`.
  - `test_a_second_confirm_after_the_close_is_idempotent` — confirm twice; second has `idempotent is True`, same `source_closed_at`, `close == "source_closed"`.
  - `test_a_late_confirm_refuses_a_source_with_unpushed_commits` — cwd `pushed_repo(tmp_path)` plus a local-only commit (copy `local_only_commit` from `test_source_worktree_guard`); `close == "refused"`, `reason` mentions unpushed, source still in panes, phase `confirmed`.
  - `test_a_late_confirm_closes_a_dirty_source_when_takeover_allowed_it` — same repo, takeover with `--allow-dirty-source`; `close == "source_closed"`.
  - `test_a_late_confirm_respects_keep_source` — takeover `--keep-source` on `herdr_server`; `close == "not_attempted"`, source in panes.
  - `test_a_late_confirm_leaves_a_legacy_record_alone` — delete `close_options` from the outbox JSON before confirm; `not_attempted`, source in panes.
  - `test_a_late_confirm_refuses_when_the_record_names_another_tab` — set record `tab_id` to a tab id that is not the source's tab; `refused`, reason `not_same_tab`, source in panes.
  - `test_a_late_confirm_when_the_source_pane_is_gone_reports_already_closed` — `herdr pane close <source>` before confirm; `close == "already_closed"`, phase `already_closed`.
  - `test_confirm_does_not_close_while_a_takeover_holds_the_lock` — hold `HandoffStore(root).takeover_lock(id)` in the test process while running confirm; `not_attempted`, finishes in < 5 s, source in panes, phase `confirmed`.
  - `test_only_the_successor_pane_can_trigger_the_close` — confirm with `HERDR_PANE_ID=<source>`; nonzero exit with the existing "only the successor pane" message, source in panes.
- [ ] **Step 2: Run** `pytest tests/test_confirm_close.py -v` → FAIL (no `close` key / source still open).
- [ ] **Step 3: Implement** the rules above in `confirm_takeover`.
- [ ] **Step 4: Run** `pytest tests -q` (integration included) → all PASS, including `test_confirm_stamps_the_successor_pane_from_its_own_environment`.
- [ ] **Step 5: Commit** `fix: confirm closes the source pane a takeover gave up waiting on`

### Task 4: Release 0.5.1 docs

**Files:**
- Modify: `pyproject.toml` (version `0.5.1`), `CHANGELOG.md` (new `## 0.5.1 - 2026-11-19` / `### Fixed` entry), `skill/herdr-handoff/SKILL.md:34,78,112` (confirm is no longer "optional record": it closes the source pane takeover could not, under the same checks; it tells the successor what `close`/`reason` mean), `src/herdr_handoff_cli.py` confirm subparser `help=`/`description=`.
- Test: `tests/test_takeover_documentation.py`

- [ ] **Step 1: Write failing test** `test_the_skill_says_confirm_closes_a_late_confirmed_source` asserting the SKILL.md text contains `confirm` together with `closes the source pane` and no longer contains `the close does not wait for it`; and `test_confirm_help_names_the_close` asserting `herdr-handoff confirm --help` output contains `close`.
- [ ] **Step 2: Run** `pytest tests/test_takeover_documentation.py -v` → FAIL.
- [ ] **Step 3: Update** the files listed.
- [ ] **Step 4: Run** `bash scripts/ci/unit-gates.sh` → green; and `pytest -m integration -q` locally → green.
- [ ] **Step 5: Commit** `herdr-handoff 0.5.1: confirm closes a late-confirmed source pane`

### Task 5 (after the herdr-handoff PR merges): herdr-engineering pin and docs

Done by the controller, not a subagent, in a herdr-engineering worktree under `~/.local/share/worktrees/` on branch `chore/pin-herdr-handoff-0.5.1`; **not pushed**.
- Bump the pin in `herdr_engineering/handoff_dependency.py` to the 0.5.1 merge commit (follow #893's shape and its tests).
- Update `herdr_engineering/skill_data/handoff/SKILL.md`, `docs/operations/handoff.md` (line ~108), `docs/automatic-handoffs.md`: the source pane closes when the successor confirms, even after the 25 s wait.
- Run that repo's affected tests; refresh the host install (`scripts/install.sh`) and check `herdr-handoff --version`/behaviour.
