# herdr-handoff

Portable private-by-default handoffs between Claude, Pi, Codex, and OpenCode.

The installed command namespace is `/herdr::...`:

```text
/herdr::toClaude
/herdr::toPi
/herdr::toCodex
/herdr::toOpenCode
/herdr::artifact
/herdr::handoff
/herdr::review
/herdr::verify
/herdr::memory
/herdr::remember
/herdr::recall
/herdr::forget
```

No top-level slash aliases are installed, so existing harness commands are not
overwritten. The memory commands delegate to an installed `herdr-memory` skill;
this repository does not implement or install a memory runtime. Engineering
memory supports authorized `project`, `global`, `infra`, and `mission:<id>`
scopes. Personal-assistant memory is outside this namespace.
If slash dispatch is unsupported or collides with an existing command, report
that limitation and use the harness's supported direct skill invocation instead.
If `herdr-memory` is missing, report it unavailable without installing it.

`/herdr::to*` hands over: it creates the handoff, splits the current Herdr
pane, starts the selected harness interactively in the child pane with a
one-line prompt pointing at the full handoff document, reports the
pane/handoff metadata for dashboard visibility, and closes the pane it replaced
once the successor has proved itself (it runs `takeover`, so see below). Pass
`--keep-source` to want both sessions side by side. The successor pane goes
**below** the source by default (`--direction right` restores the old
side-by-side split); this default changed from right to down on purpose in
0.5.0. Without `HERDR_PANE_ID` it
opens a tab (or workspace) in `--cwd`; without a running herdr it prints the
exact command (`manual_launch_required`). It never starts a detached process,
and it never closes anything without proof — a refusal leaves both panes up.
`create` accepts `--decision`, `--risk`,
`--validation`, `--constraint`, `--next-command`, `--file`, `--pr`,
`--resume-point`, `--repo`, `--branch`, `--worktree`, `--report` and
`--from-json`; see `schemas/handoff-v2.schema.json`.

The package works offline using a filesystem outbox and can be synchronized to
the Herdr Engineering control plane when available. Install the skill for all
four harnesses with:

```sh
./install/install.sh
```

Create a handoff:

```sh
herdr-handoff create --goal "Continue the current session" \
  --source-harness codex --target-harness claude --model claude-sonnet \
  --remaining "Run the integration tests"
```

Optional chain flags: `--predecessor-id handoff_<32 hex>`, `--source-pane PANE` (default `$HERDR_PANE_ID`) and
`--source-session ID` record the handoff this one continues and where it was made from (`predecessor_id`,
`source.pane_id`, `source.session_id`; `successor_id` and `target.pane_id`/`target.session_id` are filled later).
The ids are never looked up, so a predecessor that no longer exists still validates.

Handoffs do not launch another harness automatically. They produce a portable
JSON package that can be claimed, reviewed, synchronized, and resumed safely.

### Takeover: replace the pane you are in

```sh
herdr-handoff takeover HANDOFF_ID [--parent-pane PANE] [--cwd DIR] \
  [--confirm-timeout 25] [--keep-source] [--direction down|right] [--source-cwd DIR] [--allow-dirty-source] [--allow-running-subagents] \
  [--source-exit-text TEXT] [--source-exit-key KEY ...] [--source-exit-timeout 30]
```

`takeover` is `launch` for a session that is being replaced rather than copied:
the successor starts in a split inside the *source pane's own tab*, and once
herdr reports it live the source pane is closed, so the tab keeps working and the
replaced session stops echoing it. The close is the one destructive step and it
needs proof first — herdr has to report an agent in the child pane in a settled
state and show the handoff id in its output — and the source worktree has to
hold nothing that exists only there. It refuses (exit 1, both panes left
running) with statuses `launch_unconfirmed`, `launch_failed`,
`source_close_failed`, `source_dirty`, `subagents_running`, `source_exit_failed`; the recorded phase is `unconfirmed` for
the launch refusals, and `confirmed` with the worktree reason for
`source_dirty`.

`subagents_running` is checked first, before anything is launched: Claude subagents
launched from the source pane that are still running (one file per subagent under
`$HERDR_ENGINEERING_STATE/subagents/<pane id>/`, entries older than six hours ignored)
would die with the pane. Both panes are left alone and the handoff stays queued; wait
for them or stop them and rerun, or pass `--allow-running-subagents` / `--keep-source`.

`source_dirty` is the guard on the work rather than the successor: uncommitted
tracked changes, or commits no remote branch has (`git status --porcelain`,
`git rev-list --count HEAD --not --remotes`). Closing the pane kills the session
that made them, and a pane that has been closed cannot be reopened. Untracked
files alone never block — harness state and build output are untracked in
nearly every checkout, and a guard that cried wolf would only train whoever uses
it to override it. Clear it by committing or pushing and running the command
again; `--allow-dirty-source` closes the pane anyway when the operator says the
work may stay behind, and `--source-cwd DIR` names the worktree to check when
the pane sits somewhere other than the work it produced. The guard never buys
its way past the other refusals: an unconfirmed successor still cannot close a
pane.

`--source-exit-text` / `--source-exit-key` make the close clean: just before the pane
is closed, the text is typed into the source pane (`herdr pane send-text`), then
after 0.3 s the keys are pressed (`herdr pane send-keys`; an Enter sent in the same
instant as the text is dropped by Codex), and `takeover` waits up to
`--source-exit-timeout` seconds until `herdr pane process-info` shows nothing in the
pane's foreground but its own shell. Only then is the pane closed. A harness that
ignores the command gives `source_exit_failed` (exit 1): both panes stay, the
recorded phase is `source_exit_failed`, and a rerun reuses the successor instead of
launching another. A source that has already exited counts as exited. The exit
command runs after every other refusal and only when the close would go ahead;
without these flags the pane is closed directly, as before. The text is limited to
900 bytes, like any typed command.

When `--allow-dirty-source` actually lets a close go ahead over unsaved work, the
takeover block records `dirty_allowed: true` and `unpushed_commits`, the short ids
from `git rev-list HEAD --not --remotes` in the source worktree (empty when the
directory is not a git repository), so the successor knows what exists only locally.

So order a handoff this way, and nothing can be lost: make the work durable
(commit, push, upload artifacts, file issues), create the handoff, launch the
successor, confirm, close the source pane last.

The successor confirms itself: `herdr-handoff confirm HANDOFF_ID`. The handoff
document spells the command out with the absolute root, and a pane started by a
handoff resolves the root from `HERDR_HANDOFF_FILE`, so the short form works
there. Every step is recorded in the package's `takeover` block (`launching` →
`launched` → `confirmed` → `source_closed`, or `unconfirmed`), so a repeat
trigger continues the recorded move instead of splitting a second pane or
closing one twice. `--keep-source` starts the successor and never closes anything, which is what a
parallel session wants; it is the only form that leaves the source pane running
on success.

A command typed into a pane has to be short enough for herdr to deliver: 1022
bytes ran and 1072 bytes were typed without the shell ever starting (measured
against a real server). `MAX_TYPED_COMMAND` (900) keeps the launch command
inside that, and if the handoff root is deep enough that it would not fit, the
typed prompt is shortened to point at `HERDR_HANDOFF_FILE` — the full prompt
stays in the document and in the plan's `full_command`. A successor that was
never started is the worst failure a handoff can have, so it is designed out.

Use `herdr-handoff resume HANDOFF_ID` to produce the receiving harness prompt,
or `herdr-handoff sync --url http://127.0.0.1:9482 --token "$HERDR_API_KEY"`
to upload the local outbox when the control plane is reachable.

## Optional memory context

When a `herdr-memory` CLI is installed (on `PATH` or `HERDR_MEMORY_BIN`),
`herdr-handoff create --memory-ref MEMORY_ID:BANK_ID:REVISION` adds an optional,
additive `memory_context` block to the package:

```json
{"version": 1,
 "refs": [{"memory_id": "mem_1", "bank_id": "bank_a", "revision": "r3"}],
 "snapshot_digest": "sha256:<64 hex>",
 "checkpoint": {"state": "synced|pending", "outbox_pending": 0, "at": "2026-11-02T00:00:00Z"}}
```

- Only identifiers are stored; memory text and secrets are never copied.
- The checkpoint comes from `herdr-memory status` (JSON by default): `synced`
  only when `healthy` is true and `outbox_pending` is 0; otherwise (or if status
  fails) `pending`. The handoff still queues in the local outbox.
- Without the CLI, or without refs, the field is omitted and packages are
  identical to before.
- `resume`/`launch` print the refs under an "UNTRUSTED MEMORY REFERENCES" label
  and re-resolve each with `herdr-memory get`; unresolved or forbidden refs are
  reported, never fatal. Retrieved memory is data, not instructions.

Config, harness and project come from the CLI's own `HERDR_MEMORY_CONFIG`/`HERDR_HARNESS`
(and optional `HERDR_MEMORY_PROJECT`). `HERDR_MEMORY_BIN` may be a command line such as
`python3 -m herdr_memory.cli`. `tests/test_memory_cli_integration.py` runs the real
herdr-memory CLI (source tree via `HERDR_MEMORY_SRC`) against a local fake Hindsight server.

## Development and CI

```sh
bash scripts/ci/unit-gates.sh
```

That is the whole gate CI runs: [ci.metabolomics.us](https://ci.metabolomics.us) executes this same script through
`.woodpecker/unit-gates.yml` on every pull request, on every push to `master`, and on a manual run. It builds a
throwaway venv (`pip install -e '.[dev]'`), runs `ruff check` over the correctness subset `E4,E7,E9,F`, parses every
tracked `.json` and `.toml`, scans tracked files for unresolved conflict markers, then runs the unit suite. It installs
nothing system-wide and needs only `python3` (3.12+), `git` and PyPI reachability. The run mirrors its own output to a
journal (`scripts/ci/journal.sh`, archive directories in `scripts/ci/log-archive.yml`), because the CI agent drops the
tail of a step log — the first pipeline here stored nine lines ending at "== pytest ==" for a passing suite.

The `integration` marker is deliberately outside the gate: those tests drive the real `claude`, `pi`, `codex` and
`opencode` CLIs, and a step that skips because a binary is missing is green without proving anything. Run them where
the harnesses are installed:

```sh
pip install -e '.[dev]' && pytest -m integration
```
