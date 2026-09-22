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

### ast-grep

- Runtime command: `ast-grep` (with the `sg` alias).
- Agent integration: the official `ast-grep` and `ast-grep-outline` skills are
  staged into the active `.agents/skills` directory.
- Use it for syntax-aware code search, lint rules, and AST-accurate rewrites;
  start with `ast-grep --lang python -p '...' -r '...' path` and verify a
  pattern or rule on a fixture before applying `-U` rewrites.
- Use `rg` for plain-text searches where syntax is not relevant.

### Repomix

- Runtime command: `repomix`.
- Use it for one-shot, portable repository snapshots or review artifacts when a
  live, incremental view is not required.
- Safe invocation: `repomix --token-budget 12000 --compress`; use `--no-files`
  for a cheap directory and metadata map. Always pass `--token-budget`; the
  command exits non-zero when the packed output exceeds the limit.
- Repomix's default Secretlint scan remains enabled. Do not pass
  `--no-security-check` in agent workflows.
- Use Semble for natural-language searches over a live repository, or `rg` for
  targeted text searches when a portable snapshot is not needed.
- The container test verifies both this active guidance and the budget gate in
  `tests/container/test_opencode_config.py` and
  `tests/container/test_image_binaries.py`.

### Semble

- Runtime command: `semble` (including `semble search` and its local MCP
  server).
- Agent integration: OpenCode receives the always-on local `semble` MCP server.
- Use it for vague natural-language code searches, for example:
  `semble search "where are failed requests retried" . --json`.
- Use ast-grep for syntax-aware structural queries, or `rg` for exact literal
  matches.
- The embedding model is baked into the image and incremental indexes live in
  the shared `/sandbox/.cache/semble` volume, so queries need no network or API
  key after the image is built.
- The runtime search and fresh OpenCode MCP discovery are covered by container
  tests.

### GitHub search

- Agent integration: OpenCode receives the pinned local `github-mcp-server`
  through a stdio proxy when GitHub credentials are available.
- For `search_issues`, `search_pull_requests`, `search_code`, and related search
  tools, the proxy requires only the `fields` needed for the current step and
  clamps `perPage` to 20 or less. Use `page` for follow-up results instead of
  asking for a large unbounded response.
- Do not request `body`, comments, labels, or full repository objects during a
  discovery search unless they are required. Use a targeted read tool after
  identifying the relevant issue, pull request, or repository.
- If the MCP server is unavailable, use `gh api` with an explicit `--jq`
  projection and `--paginate` as the fallback; do not print full API objects.
- Search tool results are capped at 64 KiB; an oversized result returns an
  actionable error asking for fewer fields or a smaller page.
- The local server's pinned version and binary availability are validated by
  the image and tool-manifest checks.

### Project-aware Go toolchains

- Runtime command: `devbox-go`.
- Use it from a Go project when the project's `go.work` or `go.mod` declares a
  different toolchain than the image default. `devbox-go --doctor` reports the
  selected version, `devbox-go version` runs Go with it, and
  `devbox-go install <tool-module>@<version>` builds a Go tool with it.
- `devbox-go run COMMAND ...` exports `GOTOOLCHAIN` to child commands that
  invoke Go. It cannot change the Go runtime embedded in an already-compiled
  binary. Installed tools use the persistent Go cache's `bin` directory, which
  is on `PATH`; use a precompiled tool's own version-selection mechanism when
  needed.
- Go's downloaded toolchains and module cache live under the persistent
  `/sandbox/.cache/go` volume. `devbox --recreate` keeps that cache.
- If `devbox-go` is unavailable, use the reported `GOTOOLCHAIN=<version>+auto`
  value explicitly with the Go command or tool. Without a discoverable
  `go.work` or `go.mod`, the command fails rather than silently selecting the
  image default.
- Unit coverage is in `tests/unit/test_devbox_go.py`; container availability is
  covered by `tests/container/test_image_binaries.py`.

### kind Kubernetes clusters

- Runtime commands: `kind`, `kubectl`, and `devbox-kind`.
- Use `devbox-kind` when a task needs a real local Kubernetes cluster. It
  selects rootless Podman and a kind-specific cgroup, logging, PID, and bridge
  configuration; run `devbox-kind preflight` before `devbox-kind create ...`.
- Launch the outer container with `devbox --kind` from a host scope that has
  cgroup v2 delegation, for example:
  `systemd-run --scope --user -p Delegate=yes devbox --recreate --kind`.
- A host-capability preflight failure returns infrastructure status `125`; an
  image/tool/configuration failure returns status `2`. Both report the exact
  problem, and neither should be replaced with `--privileged`; use a supported
  delegated runner for host limitations instead.
- For a minimal cluster check, use `devbox --kind devbox-kind create cluster
  --name devbox --wait 5m`, `devbox --kind kubectl get nodes --context
  kind-devbox`, and `devbox --kind devbox-kind delete cluster --name devbox`.
- If kind cannot be supported by the current host, use the preflight report and
  the unit/container tests; do not silently turn the integration test into a
  product pass.

### Token-hygiene utilities

The Fedora package names are `tokei`, `just`, `difftastic`, `hyperfine`, and
`fd-find`. Their command names are `tokei`, `just`, `difft`, `hyperfine`, and
`fd`, respectively.

### tokei

- Use `tokei .` for repository size and per-language file/SLOC counts; use
  `tokei -o json` when the result will be parsed by another command.
- Fall back to `rg --files` and targeted reads when a repository is too large
  for the output budget or only a file list is needed.

### just

- Use `just --list` to inspect the available recipes when the repository has a
  `justfile`; this lists commands without running a recipe.
- A Makefile-only repository gains nothing from `just`, so use normal tools for
  Makefile archaeology instead.

### difft

- Use `difft old/path new/path` for syntax-aware comparisons when moved or
  refactored code would be easy to misread in a line diff.
- Fall back to `git diff` when the syntax-aware output is longer than the
  available context or the file type is unsupported.

### hyperfine

- Use `hyperfine --warmup 3 'command'` for repeatable command timing instead
  of writing an ad-hoc timing loop; quote the command as one argument.
- Fall back to `time` for a one-off measurement or when a command must not be
  repeated.

### fd

- Use `fd pattern path` for fast, gitignore-aware file discovery; add
  `--hidden --exclude .git` when hidden files are part of the search.
- Fall back to `rg --files` when its file-listing behavior is sufficient or
  when `fd` is unavailable.

## Adding Entries

Keep each entry short and operational. Include:

- The exact command or OpenCode integration name.
- The task signal that should trigger its use.
- One safe example or invocation shape.
- The normal fallback.
- The test that proves the agent can discover or invoke it.

Do not list a capability here before its runtime artifact and integration are
actually present in the image.
