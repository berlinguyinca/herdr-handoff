---
name: herdr-handoff
description: Create, package, transfer, and resume private Herdr work across Pi, Claude, Codex, and OpenCode.
---

# HerdR Handoff Skill

Use `herdr-handoff` when work needs to move between harnesses, hosts, or
sessions. A handoff is a durable request, not an automatic process launch.

## Safety contract

- Handoffs are private by default.
- Capture structured context: goal, completed work, remaining work, decisions,
  risks, validation, repository state, session/mission IDs, and artifact IDs.
- Never include credentials, tokens, hidden reasoning, private prompts, or raw
  unrelated transcripts.
- Reference HerdR artifacts by immutable artifact/version ID; do not copy RustFS
  credentials or binary payloads into messages.
- An optional model is a target hint. The recipient records the resolved model;
  do not claim a model was used before it reports that fact.
- Use the local outbox when Herdr is unavailable and synchronize later.

## Workflow

1. Create a package with `herdr-handoff create`.
2. Inspect the JSON and confirm the target harness and remaining work.
3. Queue or synchronize it through Herdr when credentials and connectivity are
   available.
4. The recipient claims and accepts the handoff before starting work.
5. Materialize referenced artifacts through `herdr-artifact` and continue from
   the pinned versions.
6. Complete, reject, cancel, or retry the handoff so the dashboard lineage is
   accurate.

When the user asks to hand work to Claude, Pi, Codex, or OpenCode, choose the
matching `--target-harness` value. Use `--model` only when a model/profile hint
was explicitly requested. Launching another CLI is opt-in and must never be
assumed from creating a handoff.
