---
name: herdr
description: "Route concise /herdr:: commands for artifacts, handoffs, reviews, verification, fabric, publication, observation, and session context across Herdr harnesses."
---

# HerdR command namespace

Use the namespaced commands below as the stable user-facing interface:

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
/herdr::publish
/herdr::observe
/herdr::session
```

The command registry is in `commands.json`. Harness adapters must load that
registry and must not register top-level aliases such as `/artifact` or
`/review`. The namespace is intentionally collision-free.

Routing rules:

- `toClaude`, `toPi`, `toCodex`, and `toOpenCode` create durable handoffs with
  the selected target harness. A trailing model is a hint only.
- `artifact` delegates to the installed `herdr-artifact` skill and defaults to
  the current authorized session context.
- `handoff` delegates to `herdr-handoff`.
- `review`, `verify`, `fabric`, `publish`, `observe`, and `session` delegate to
  the corresponding Herdr control-plane operation when available.
- Public publication always requires explicit approval.
- Never overwrite a pre-existing harness command or skill. If an adapter
  cannot register the namespace, retain the portable CLI/API path and report
  the collision.
