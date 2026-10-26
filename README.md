# herdr-handoff

Portable private-by-default handoffs between Claude, Pi, Codex, and OpenCode.

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
