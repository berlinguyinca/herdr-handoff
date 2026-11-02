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
```

No top-level slash aliases are installed, so existing harness commands are not
overwritten.

`/herdr::to*` splits the current Herdr pane, starts the selected harness
interactively in the child pane, sends the bounded resume prompt, and reports
the pane/handoff metadata for dashboard visibility. It requires
`HERDR_PANE_ID`; it never starts a detached process.

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

Handoffs do not launch another harness automatically. They produce a portable
JSON package that can be claimed, reviewed, synchronized, and resumed safely.

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
- The checkpoint comes from `herdr-memory status --json`. If that is
  unreachable or reports pending outbox items the checkpoint is `pending`; the
  handoff still queues in the local outbox.
- Without the CLI, or without refs, the field is omitted and packages are
  identical to before.
- `resume`/`launch` print the refs under an "UNTRUSTED MEMORY REFERENCES" label
  and re-resolve each with `herdr-memory get`; unresolved or forbidden refs are
  reported, never fatal. Retrieved memory is data, not instructions.

The CLI contract assumed here (`status --json`, `get ID --bank B --revision R --json`)
is mocked in tests; no real herdr-memory was available.
