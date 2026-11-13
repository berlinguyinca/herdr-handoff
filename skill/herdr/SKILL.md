---
name: herdr
description: "Route concise /herdr:: commands for artifacts, handoffs, reviews, verification, fabric, publication, observation, session context, and engineering memory across Herdr harnesses."
---

# HerdR command namespace

Use these namespaced commands where the harness supports native slash registration;
otherwise use the installed skill or CLI fallback below:

```text
/herdr::toClaude [model]
/herdr::toPi [model]
/herdr::toCodex [model]
/herdr::toOpenCode [model]
/herdr::artifact
/herdr::handoff
/herdr::review
/herdr::verify
/herdr::fabric
/herdr::join [--no-workspace]
/herdr::publish
/herdr::observe
/herdr::session
/herdr::memory
/herdr::remember
/herdr::recall
/herdr::forget
```

The command registry is in `commands.json`. Harness adapters must load that
registry only where native slash registration is supported and must not register
top-level aliases such as `/artifact` or `/review`. Existing registrations take
precedence even within this namespace.

Routing rules:

- `toClaude`, `toPi`, `toCodex`, and `toOpenCode` create durable handoffs and
  launch the selected target harness in a new pane split from the current
  Herdr pane. A trailing model is a hint only.
- `artifact` delegates to the installed `herdr-artifact` skill and defaults to
  the current authorized session context.
- `handoff` delegates to `herdr-handoff`.
- `takeover` delegates to `herdr-handoff takeover`: the successor starts in a
  split inside the current pane's own tab, and the current pane is closed only
  after herdr reports the successor live and the successor has run
  `herdr-handoff confirm HANDOFF_ID`. Without that proof the current pane keeps
  running and the refusal is reported.
- `review`, `verify`, `fabric`, `publish`, `observe`, and `session` delegate to
  the corresponding Herdr control-plane operation when available.
- `memory`, `remember`, `recall`, and `forget` delegate to the installed
  `herdr-memory` skill: memory inspection, explicit save, search, and deletion,
  respectively. Load that skill before executing the operation and forward the
  user's intent and arguments. It owns scope resolution, authorization, storage,
  revision handling, and execution; do not duplicate its runtime here.
- Follow the canonical herdr-memory SDK 0.1.6 engineering scope contract:
  `project`, `global`, `infra`, and `mission:<id>`. Resolve authorized engineering
  scopes through `herdr-memory`; do not broaden requests to all scopes by default.
  The engineering namespace cannot access the personal hub. Never read or write
  personal memory here, or recommend `--project personal` as a workaround.
  `remember` is an explicit save request; `forget` must identify the memory and
  its authorized engineering scope before deletion.
- Use the existing backend/MCP and live controller through `herdr-memory`.
  This registry adds routing only, not a memory runtime or installer.
- `join` (action `fabric-join`) delegates to the installed `herdr-join` skill (installed by
  `herdr-eng summary install`), which runs `herdr-eng rooms join-fabric NAME --json`. It is
  explicit opt-in: never run it from a session-start hook. `coordinate` and `grant_agent` stay owner-only.
- Public publication always requires explicit approval.
- Never overwrite a pre-existing harness command or skill. If an adapter
  cannot register the namespace, retain the portable CLI/API path and report
  the collision.

## Unsupported slash commands and unavailable skills

The registry describes routing intent; it does not prove a harness has installed
slash-command dispatch. Register native commands only where the harness actually
supports them. If `/herdr::...` is unsupported, report that limitation and invoke
its installed `herdr-memory` skill using the harness's supported skill mechanism.
Follow that skill's available MCP tools or documented `herdr-memory` CLI for the
same operation and authorized engineering scope. `memory` means inspection/status;
it does not imply a CLI subcommand named `memory`.
Do not claim slash registration or execution without evidence, invent CLI/API
commands, or substitute a different memory store. If the skill cannot be loaded,
use the installed CLI only when its supported contract is available; otherwise
report memory routing unavailable. Do not install anything as a routing side effect.

Preserve pre-existing commands and skills, including top-level `/memory`,
`/remember`, `/recall`, and `/forget` and any colliding `/herdr::...` registration.
Report the collision and use a supported direct skill invocation when available;
never overwrite, shadow, or silently replace the existing handler. This registry
does not install aliases or memory runtime implementations.
