# my-sandbox

A secure, VM-native development environment for AI-assisted coding and modern
software projects. `devbox` enters one shared Fedora Lima VM at the current
project directory; OpenCode and the pinned development toolchain run inside that
VM. Rootless Podman is available in the guest as a project tool for workflows
such as kind and nested builds.

The VM template, host requirements, mounts, provisioning, and lifecycle details
are documented in [`lima/README.md`](lima/README.md).

## Start and manage the VM

After completing the one-time host setup in the Lima guide, run the launcher
from a project under `~/src`:

```shell
cd ~/src/my-project
devbox                 # open a shell at this project path in the VM
devbox opencode        # start OpenCode in this project
devbox --stop          # gracefully stop the shared VM
devbox --reprovision   # restart and apply current tool/provisioning changes
devbox --reset         # factory-reset VM-local state and reprovision
```

The first invocation creates the VM from this checkout's Lima template. The
launcher starts it on demand, maps the current project path into the guest, and
rejects paths outside configured mounts. `devbox --reset` removes VM-local
state, but leaves host-mounted projects and configuration intact. See the Lima
guide for optional login autostart and VM recreation requirements.

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
utilities. The VM also includes GNU make, kind, kubectl, Helm, Pipenv, and a
VM-local setup-envtest asset-store location. Use `devbox-toolchain-check` to
compare installed tool versions with the manifest and `devbox-go --doctor` to
see a Go project's selected toolchain.

The VM-owned agent capability catalog is staged at
`~/.agents/skills/devbox-tools/SKILL.md` inside the guest. OpenCode runtime
configuration is generated in the VM immediately before launch; credentials are
referenced by environment name and are not written into the host's shared
configuration.

| Integration | Enablement |
| --- | --- |
| GitHub | Authenticated `gh` CLI; it is the canonical GitHub interface. |
| Semble | Local CLI/MCP and a VM-local model/index cache. |
| Context7 | `CONTEXT7_API_KEY`. |
| Tavily search | `TAVILY_API_KEY`, through OpenCode's built-in web search. |
| The Source | `IGLOO_MCP_*` credentials; VM trusts the internal CA roots. |
| Anthropic | `ANTHROPIC_API_KEY`; optionally `ANTHROPIC_BASE_URL`. |
| PriceTag | The relevant endpoint plus `PRICETAG_API_KEY`. |
| OCTO Open | Both `OCTO_OPEN_URL` and `OCTO_OPEN_KEY`. |

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
./scripts/sanitized-test.sh --guest-vm -- \
  env UV_PROJECT_ENVIRONMENT="$UV_PROJECT_ENVIRONMENT" \
  PRE_COMMIT_HOME="$HOME/.cache/pre-commit" ./scripts/fast-check.sh

# Tests against this provisioned VM, including the kind smoke test
./scripts/sanitized-test.sh --guest-vm --require-vm -- \
  env UV_PROJECT_ENVIRONMENT="$UV_PROJECT_ENVIRONMENT" \
  uv run --extra test pytest -m "vm or e2e_kind"

# Lima-in-Lima coverage (requires host nested KVM)
./scripts/sanitized-test.sh --guest-vm --require-recursive-vm -- \
  env UV_PROJECT_ENVIRONMENT="$UV_PROJECT_ENVIRONMENT" \
  uv run --extra test pytest -m recursive
```

Pytest markers are `unit`, `vm`, `recursive`, `e2e_kind`, `cold_bootstrap`, and
`e2e_inference`. The inference tests call real provider APIs and are excluded
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

For the manifest, Renovate updates only each tool's `version` field; it cannot
derive architecture-specific checksums or agent-skill hashes. A version-only
update to a checksum-backed tool is incomplete until provenance is refreshed
with `python3 scripts/verify_provenance.py --update`. CI runs the verifier
without `--update` and blocks changes with stale hashes. The first command
checks manifest/consumer consistency; the second verifies release checksums and
agent-skill pins. Before refreshing a PR you did not author, review its
`provenance.url_templates` against the base branch; they define the assets
whose hashes are trusted and should not change for a version-only update.
Manifest-only updates are applied by `devbox --reprovision`; edits to embedded
Lima provisioning or template files require recreating the VM.

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
