---
name: "devbox-tools"
description: >
  Use this skill when choosing, adding, or integrating a command or
  agent-facing capability provided by the devbox image.
---

# Devbox Capability Contract

The devbox image has two separate contracts for every agent-facing capability:

1. Runtime availability: the command, plugin, or service is installed and
   works in the image.
2. Agent visibility: the agent has instructions or configuration that tells it
   when and how to use the capability.

Installing a binary proves runtime availability only. Do not assume that an
agent knows about a command because it is on `PATH`.

## Registration Requirements

When adding an image capability, complete all applicable parts together:

- Pin and install the runtime artifact, and document the exact command name.
- Add an entry here describing when to use it, its safe invocation, and its
  fallback when it is unavailable or unsuitable.
- Add explicit OpenCode MCP, plugin, or wrapper configuration when the
  capability is not a plain command.
- Add tests for runtime availability and agent visibility or invocation.
- If the devbox container is persistent, document that `devbox --recreate` is
  required after changing image contents or integration triggers.

The skill is image-owned and is copied into the active `.agents/skills`
directory after any host `.agents` mount is applied. That makes this catalog
available for arbitrary project repositories without requiring a shared
repository `AGENTS.md` or README.

## Current Capability

### Ripwire

- Runtime command: `ripwire`
- Agent integration: the launcher registers the local `ripwire --mcp` server
  and the image stages Ripwire skills.
- Use it for repository mapping, ranked code navigation, call-graph queries,
  impact analysis, and change-safety checks.
- Prefer the relevant Ripwire skill and MCP operation over broad repository
  dumps or repeated grep/read loops.

## Adding Entries

Keep each entry short and operational. Include:

- The exact command or OpenCode integration name.
- The task signal that should trigger its use.
- One safe example or invocation shape.
- The normal fallback.
- The test that proves the agent can discover or invoke it.

Do not list a capability here before its runtime artifact and integration are
actually present in the image.
