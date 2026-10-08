# my-sandbox

A secure, VM-native development environment for AI-assisted coding and modern
software projects. `devbox` enters one shared Fedora Lima VM at the current
project directory; OpenCode and the pinned development toolchain run inside that
VM. Docker CE and Podman are available as separate rootless runtimes in the
guest. The Docker CLI uses its own Engine by default; Podman remains available
for explicitly selected workflows. Kind uses Docker CE by default, with a
separate Podman provider.

The VM template, host requirements, mounts, provisioning, and lifecycle details
are documented in [`lima/README.md`](lima/README.md).

## Start and manage the VM

After completing the one-time host setup in the Lima guide, run the launcher
from a project under `~/src`:

```shell
cd ~/src/my-project
devbox                 # open a shell at this project path in the VM
devbox opencode        # start OpenCode in this project
devbox --debug opencode # enable debug logs for all managed-service sessions
devbox --no-debug opencode # disable managed-service debug logs
devbox --stop          # gracefully stop the shared VM
devbox --recreate      # create a fresh VM, then open its interactive shell
devbox --recreate -- true # non-interactive recreation
devbox --recreate -- git status   # run a command in the fresh VM
devbox -r -- opencode            # short form; run a command after recreation
devbox --stop && devbox -- git status # reapply manifest/tool updates, then run a command
devbox --delete        # remove the VM without starting or recreating it
```

The first invocation creates the VM from this checkout's Lima template. The
launcher starts it on demand, maps the current project path into the guest, and
rejects paths outside configured mounts. `--recreate` validates that mapping
before deleting the configured VM, then creates and starts a fresh VM from this
checkout's Lima template. An optional command runs only after readiness checks
pass, in the mapped current directory, with the normal filtered environment;
its argv and exit status are preserved. Bare `devbox --recreate` opens the
normal interactive shell in the fresh VM; use `devbox --recreate -- true` for
non-interactive automation. Use `--` before a command that starts with an
option.

`devbox --recreate` loses VM-local state but preserves host-mounted projects,
configuration, and OpenCode L1 state. If the VM does not exist, recreation just
creates it. `devbox --delete` removes the configured Lima instance and its
guest-local state without starting or recreating it; this explicitly removes
Lima's protection before deletion. Host-mounted files remain intact. See the
Lima guide for details and VM recreation requirements.

## Worktrees and Python environments

Use the normal Git worktree workflow, with commands run inside the VM. Give
each worktree its own VM-local Python environment; the checkout itself is on a
host-shared mount, so a single `.venv` must not be used by both host and guest.

```shell
cd ~/src/my-project
git worktree add .worktrees/my-change origin/main
cd .worktrees/my-change
export UV_PROJECT_ENVIRONMENT="$HOME/.venvs/my-project-my-change"
uv sync --extra test
```

Keep the worktree in `~/src`, use it from either the host or the VM (not both),
and do not store virtual environments or tool caches on the shared project
mount. Repository-specific agent instructions are in [`AGENTS.md`](AGENTS.md).

## VM toolchain and agent integrations

The full toolchain is provisioned from
[`lima/tool-versions.json`](lima/tool-versions.json) and includes OpenCode, Go,
Rust, Node.js, Python/uv, Playwright, ast-grep,
Semble, Repomix, cloud and productivity CLIs, linters, and release inspection
utilities. The VM also includes GNU make, kind, Minikube v1.39.0, kubectl, Helm,
Pipenv, and a VM-local setup-envtest asset-store location. Use
`devbox-toolchain-check` to compare installed tool versions with the manifest
and `devbox-go --doctor` to see a Go project's selected toolchain.

The VM-owned agent capability catalog is staged at
`~/.agents/skills/devbox-tools/SKILL.md` inside the guest. The VM generates an
in-memory OpenCode overlay for baseline permissions and optional integrations
immediately before launch; it does not generate provider or model settings.
EnMaaS provider/model settings are user-managed in
`~/.config/opencode/opencode.jsonc`. Credential values are never serialized
into the runtime overlay.

| Integration | Enablement |
| --- | --- |
| GitHub | Authenticated `gh` CLI; it is the canonical GitHub interface. |
| Semble | Local CLI/MCP and a VM-local model/index cache. |
| Context7 | `CONTEXT7_API_KEY`. |
| Tavily search | `TAVILY_API_KEY`, through OpenCode's built-in web search. |
| The Source | `IGLOO_MCP_*` credentials; VM trusts the internal CA roots. |
| EnMaaS | A complete `ENMAAS_URL`/`ENMAAS_API_KEY` pair is forwarded; full URL validation runs on the `devbox opencode` launch path. Provider and model settings are user-managed in OpenCode config. |
| PriceTag utilities | `PRICETAG_HOSTED_URL` or `PRICETAG_OPENAI_URL` plus `PRICETAG_API_KEY`; used by `list-models.sh` and the opt-in gateway inference test. |

Devbox forwards EnMaaS only as a non-empty pair. The host rejects non-HTTPS
schemes, and the guest validates the full URL on the `devbox opencode` launch
path. With a complete pair, direct OpenAI/Anthropic keys and the direct
Anthropic base URL are removed from the guest environment. Devbox does not
generate provider or model settings; manage those in your user OpenCode config
(see the [Lima guide](lima/README.md) for the expected provider settings). A
provider configured with `env: []` and EnMaaS references does not fall back to
ordinary provider credentials when the pair is absent. OpenCode project-level
settings can override the global provider endpoint, so only forward EnMaaS
credentials to trusted projects.

The launcher passes a strict allowlist of supported provider credentials. The
VM mounts only selected project, knowledge-base, and configuration paths; see
the [mount table](lima/README.md#shared-vs-vm-local-state) for details.

## Security posture

- The Lima VM is the only devbox runtime. Its template mounts only explicit
  project, knowledge-base, and selected configuration directories; it never
  mounts all of `$HOME`.
- Require Lima **2.1.3 or newer** for the guest-agent security fix. Keep the
  host kernel patched and reboot promptly when updates require it.
- Nested virtualization is enabled for L2 test VMs and related project tools.
  This is an accepted residual risk; mitigate it with host kernel patch
  discipline and restrict `/dev/kvm` to the `root:kvm` group (mode `0660`). Add
  the host user to `kvm`; do not make the device world-writable.
- Host configuration and credentials are mounted behind a protected parent,
  and provider environment variables cross into the VM only through the
  launcher's explicit allowlist.
- When EnMaaS credentials are forwarded, project OpenCode config can override
  `providers.*.settings.baseURL` while `{env:ENMAAS_API_KEY}` remains available.
  Only run OpenCode with forwarded EnMaaS credentials in trusted projects.

## Testing

Install test dependencies in the VM-local environment for the active worktree:

```shell
export UV_PROJECT_ENVIRONMENT="$HOME/.venvs/my-sandbox-my-change"
uv sync --extra test
```

Run tests through the sanitized wrapper. It creates a temporary `HOME` and XDG
tree, drops host credentials/config overrides, and forwards only the small
non-secret environment required by the selected tier.

In local `--guest-vm` mode, same-UID test processes can still read host-mounted
files under `~/.host-config` (including CLI credentials). Use this mode only
with trusted source. CI runs tests in a fresh guest with empty config and
credential mounts.

```shell
# Fast lint and unit-test iteration
./scripts/sanitized-test.sh --guest-vm --vm-lock -- \
  env UV_PROJECT_ENVIRONMENT="$UV_PROJECT_ENVIRONMENT" \
  PRE_COMMIT_HOME="$HOME/.cache/pre-commit" ./scripts/fast-check.sh

# Tests against this provisioned VM, including the kind smoke test
./scripts/sanitized-test.sh --guest-vm --vm-lock --require-vm -- \
  env UV_PROJECT_ENVIRONMENT="$UV_PROJECT_ENVIRONMENT" \
  uv run --extra test pytest -m "vm or e2e_kind"

# Lima-in-Lima coverage (requires host nested KVM)
./scripts/sanitized-test.sh --guest-vm --vm-lock --require-recursive-vm -- \
  env UV_PROJECT_ENVIRONMENT="$UV_PROJECT_ENVIRONMENT" \
  uv run --extra test pytest -m recursive
```

Minikube v1.39.0 is pinned for #318. Its Docker CE,
rootless-Podman, and KVM2 modes require explicit driver selection; the Podman
and KVM2 test profiles each use 2 CPUs/4 GiB and run sequentially within the
8-CPU/16-GiB Lima L1. Read the [Minikube driver and resource
notes](lima/README.md#minikube-backends-issues-318-and-319). The #319
end-to-end test covers the driver matrix; no result is claimed here.

The per-user lock serializes full test commands across worktrees. Full
`fast-check` runs include signal/process-group regression tests that timed out
when run concurrently, so use the lock shown above; targeted parallel-safe
tests can still run concurrently. See
[`docs/test-runtime.md`](docs/test-runtime.md) for measured baselines,
resource-collection commands, and concurrency guidance.
The lock waits up to 3600 seconds by default (configurable up to 86400 seconds).

Pytest markers are `unit`, `unit_serial`, `vm`, `recursive`, `e2e_kind`,
`cold_bootstrap`, and `e2e_inference`. `unit_serial` keeps process/signal
regression tests out of the parallel-safe worker pool. The inference tests call
real provider APIs and are excluded
from routine validation; run them only when intentionally using provider
credentials. A missing VM capability is reported as an infrastructure limit,
not as a product-test failure. CI runs unit/pre-commit checks and the
provisioned VM tier; recursive tests run on relevant changes and on the nightly
schedule.

## Tool-version maintenance

`lima/tool-versions.json` is the canonical pin manifest. Lima provisioning,
CI, pre-commit, and Renovate consume or validate its entries. When changing a
pin, run:

```shell
python3 scripts/validate_tool_versions.py
python3 scripts/verify_provenance.py
```

Every tool and downloaded agent skill in the manifest declares an explicit
`integrity` policy: `sha256` or `version-only`. The checksum-managed release
binaries—Hadolint, uv, Antigravity CLI, Lima, kind, Minikube, ast-grep, and
Atlassian CLI (`acli`)—have separate amd64/arm64 artifact records with exact
upstream versions and SHA-256 digests. Renovate's regex manager models GitHub
release assets as `github-release-attachments` dependencies. For acli, separate
custom datasources extract each Linux URL's version and its following SHA-256
line from Atlassian's official Homebrew formula. Each checksum group requires
three updates, so Renovate waits for the version alias and both architectures
before opening a PR. CI validates the aliases and checks the declared release
assets; the verifier runs in check-only mode. The ast-grep agent-skill archive
remains SHA-256-verified at its pinned commit.

The deliberate version-only v1 exceptions for direct downloads are Node, Go,
rustup, Helm, and kubectl. They remain exact HTTPS version/commit pins, but are
**not SHA-256-verified**. Go retains Renovate's timestamped `golang-version`
datasource and the global 10-day release-age gate. Although go.dev's JSON feed
publishes Linux hashes, it lacks timestamps to join to the version datasource,
so Go is version-only in v1. Other package-manager tool pins are likewise
marked `version-only` because the manifest does not carry package artifact
digests. acli uses Atlassian's official Homebrew formula for its version and
both architecture digests. Its feeds have no release
timestamps, so a scoped `timestamp-optional` rule lets the acli group update
without the global 10-day wait. All other dependencies keep that gate unchanged.

Version-only direct downloads rely on HTTPS and the VM trust store, which
includes the repository's internal Red Hat CA roots as well as public roots;
those internal CAs are part of the TLS integrity boundary for these pins.

Offline tests exercise per-architecture regex replacement, GitHub release
attachment matching, the configured acli JSONata transforms against a formula
fixture, and provenance verification. A real Mend-hosted Renovate run is still
required to prove that the hosted bot creates both architecture updates in one
PR.

Review any change to `provenance.url_templates` against the base branch: those
URLs select the assets checked by CI, and Renovate should only update the
artifact version/digest records, not their provenance templates.
Manifest-only updates are applied when the VM next starts (for example, run
`devbox --stop` and then `devbox`). For scripted manifest refresh followed by a
command, use `devbox --stop && devbox -- <command>` (for example,
`devbox --stop && devbox -- git status`); edits to embedded Lima provisioning
or template files require `devbox --recreate`.

## Repository map

```text
.
├── .github/          # CI, Renovate, issue forms, and pull-request template
├── lima/             # VM template, provisioning, pinned tools, and VM skills
├── scripts/          # Test wrappers and manifest/provenance validation
├── tests/unit/       # Fast isolated tests
├── tests/vm/         # Provisioned-VM, recursive, and kind tests
└── devbox            # VM-only launcher
```

See [`AGENTS.md`](AGENTS.md) for worktree conventions, issue triage, and the
authoritative pull-request CI/merge wait procedure.
