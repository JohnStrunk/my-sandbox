# my-sandbox

A secure, rootless containerized development environment tailored for
AI-assisted coding and modern software development workflows.

This repository provides:

- **`devbox`**: A single-command launcher that starts an interactive, fully
  rootless development container with nested container support
  (Podman-in-Podman) and automatic credential passthrough.
- **`container/`**: A container image definition bundling modern language
  toolchains, cloud CLIs, code linters, and AI coding assistants like
  [OpenCode](https://opencode.ai).

---

## Key Features

- **Fully Rootless & Secure**: Runs via Podman using `--userns=keep-id` without
  requiring `--privileged` mode or added Linux capabilities. Files created
  inside the container remain owned by the host user.
- **Nested Podman-in-Podman**: Build and run containers inside the devbox
  without host root permissions. Uses `fuse-overlayfs` and dynamic subordinate
  UID/GID delegation (`/etc/subuid` and `/etc/subgid`).
- **Persistent Caches**: `uv`, pre-commit, and nested Podman/Buildah image
  storage survive `devbox --recreate`, so recreating a container doesn't
  repeat downloads or image builds whose inputs haven't changed.
- **Comprehensive Toolchain**:
  - **Languages & Runtimes**: Go, Rust, Python packaging via `uv` and `uvx`,
    Node.js, and Playwright with full browser dependencies.
  - **Cloud & Productivity CLIs**: GitHub CLI (`gh`), GitLab CLI (`glab`),
    Google Cloud SDK (`gcloud`), Google Workspace CLI (`gws`), Atlassian CLI
    (`acli`), Google Antigravity (`agy`), and OpenCode (`opencode`), with
    [tokenjuice](https://github.com/vincentkoc/tokenjuice)'s OpenCode plugin
    pre-installed to compact noisy terminal output before it reaches context.
  - **Linters & Utilities**: `pre-commit`, `ripgrep`, `jq`, `shellcheck`,
    `hadolint`, `markdownlint-cli2`, and `ffmpeg`.
- **Automatic Host Credential & Config Passthrough**: `devbox` detects and
  bind-mounts existing host configurations (GitHub tokens, Google Cloud ADC,
  Atlassian CLI, Google Workspace, LiteMaaS API keys, and OpenCode
  configuration, state, and session data), and passes supported API credentials
  and endpoints such as Anthropic's directly into the container. When a GitHub
  token is available, it also enables the OpenCode GitHub MCP server without
  modifying any mounted OpenCode configuration file.

---

## Prerequisites

- [Podman](https://podman.io) installed on the host machine.
- Linux host operating system recommended for rootless user namespaces.
- Optional: Host credentials for cloud services (e.g., `gh auth login`,
  `gcloud auth application-default login`, or OpenCode configuration).

---

## Usage

### Launching a Devbox

`devbox` always bind-mounts the _current working directory_, so `cd` into
whatever project you want to work on and run it from there:

```shell
cd /path/to/project
/path/to/this/repo/devbox
```

The container is named after the current directory (`devbox-<dirname>`), so
each project directory gets its own persistent container. The first run
builds the `devbox:latest` image (if not already built), configures user
namespace delegations, passes relevant host configuration and environment
variables, and opens an interactive bash shell in the bind-mounted directory.
Subsequent runs from the same directory just exec a new shell into the
existing container (starting it first if it's stopped).

The launcher records a fingerprint of the image build context on each new
container and checks it, along with the image ID, on subsequent runs. If the
Dockerfile or another file in `container/` changed, or the image was rebuilt,
the launcher warns that the existing container is stale and prints the
`devbox --recreate` command needed to refresh it. Recreating removes only the
container; the host-backed project directory remains intact.

For convenience, symlink the script onto your `PATH` so it can be run as
just `devbox` from any project directory:

```shell
ln -s /path/to/this/repo/devbox ~/bin/devbox
```

You can also pass a command directly to execute it inside the container instead
of opening an interactive shell:

```shell
devbox opencode
devbox ls -al
```

Additional flags let you manage the container's lifecycle:

```shell
devbox --remove    # or -r: stop and remove this directory's container
devbox --recreate  # or --new: remove then re-create the container
```

### Working Inside the Devbox

Inside the container, you can run development commands, build projects, execute
nested containers, or invoke AI assistants:

```shell
# Nested container execution
podman run --rm alpine uname -a

# Run OpenCode with configured models
opencode
```

### Git Identity & GitHub Authentication

When a container is created, `devbox` configures a Git identity inside it: any
`user.name`/`user.email` already set inside that (persistent) container is
left untouched, and anything still missing is filled in from the invoking
host user's own `git config --global user.name`/`user.email` -- the host's
Git config is only ever read, never modified. If neither source has an
identity, `devbox` prints a warning explaining how to set one with
`git config --global user.name/user.email` inside the container.

The devbox has no usable SSH access to GitHub, so all Git operations against
`github.com` must go over HTTPS, authenticated through the `gh` CLI. Whenever
a GitHub token is available (see the GitHub integration below), `devbox` runs
`gh auth setup-git` inside the container automatically, registering `gh` as
Git's credential helper so `git clone`/`fetch`/`push` against
`https://github.com/...` URLs work without SSH keys or an agent. Otherwise, it
prints a warning explaining how to run `gh auth login && gh auth setup-git`
manually.

### Persistent Caches

`devbox` backs a few directories that are otherwise disposable-but-expensive
with storage that survives `devbox --recreate` (and container removal in
general), so recreating a container doesn't repeat downloads or nested image
builds whose inputs haven't changed:

| Path | Backing | Notes |
| --- | --- | --- |
| `/sandbox/.uv_cache` | Podman named volume `devbox-uv-cache` | `uv`/`uvx` package downloads. |
| `/sandbox/.cache/pre-commit` | Podman named volume `devbox-precommit-cache` | Pre-commit hook environments. |
| `/sandbox/.local/share/containers/storage` | Host directory `${XDG_CACHE_HOME:-~/.cache}/devbox/containers-storage` | Nested Podman/Buildah's own image and layer storage. |

The `uv` and pre-commit caches use Podman-managed named volumes because their
exact host-side location doesn't matter. Nested Podman/Buildah's storage
instead uses a plain host directory so its size can be inspected and pruned
with ordinary tools (`du -sh`, `rm -rf`) without needing `podman volume`
commands.

All three are shared across _every_ devbox instance, not just one project's
container, so they persist even across `devbox --remove`; only deleting the
volume/directory itself clears them:

```shell
# uv and pre-commit caches
podman volume rm devbox-uv-cache devbox-precommit-cache

# Nested Podman/Buildah image and layer storage
rm -rf "${XDG_CACHE_HOME:-$HOME/.cache}/devbox/containers-storage"
```

This is local, runtime cache persistence between devbox sessions on one
machine. It's a different scope from the GitHub Actions layer caching that
speeds up building the `devbox:latest` image itself in CI (see
`.github/workflows/`); the two don't share storage.

### Automatic OpenCode Integrations

The integrations below are enabled when their requirements are present while a
devbox container is created:

| Name | Description | Requirements |
| --- | --- | --- |
| GitHub | GitHub repository, issue, pull request, and code search capabilities. | At least one of `GH_TOKEN`, `GITHUB_TOKEN`, or an authenticated host `gh` CLI. |
| The Source | Search and fetch capabilities for The Source, Red Hat's intranet. | All of `IGLOO_MCP_COMMUNITY`, `IGLOO_MCP_COMMUNITY_KEY`, `IGLOO_MCP_APP_PASS`, `IGLOO_MCP_APP_ID`, `IGLOO_MCP_USERNAME`, and `IGLOO_MCP_PASSWORD`. |
| Context7 | Up-to-date documentation and code examples for software libraries. | `CONTEXT7_API_KEY`. |
| Anthropic | Direct Anthropic models, including Anthropic-compatible endpoints. | `ANTHROPIC_API_KEY` enables the built-in provider; optional `ANTHROPIC_BASE_URL` selects a custom endpoint. |

Runtime integrations can contribute any top-level OpenCode config property, with
multiple MCP integrations combined under one `mcp` object in
`OPENCODE_CONFIG_CONTENT`. The user's global `~/.config/opencode`
configuration and the project's `opencode.json` remain unchanged. Since the
container is persistent, use `devbox --recreate` after adding or changing host
credentials or integration triggers.

OpenCode automatically discovers its built-in Anthropic provider from
`ANTHROPIC_API_KEY` and the Anthropic SDK uses `ANTHROPIC_BASE_URL` for a custom
compatible endpoint, so no generated provider configuration is required. Select
an Anthropic model with `anthropic/<model-id>`.

---

## Repository Structure

```text
.
├── .github/
│   ├── workflows/             # GitHub Actions CI workflows
│   ├── lint-all.sh            # Script to run pre-commit across all files
│   ├── markdownlint-cli2.yaml # Markdown lint configuration
│   ├── mergify.yml            # Mergify PR automation rules
│   └── renovate.json5         # Renovate dependency updates
├── container/
│   ├── Dockerfile             # Container definition
│   ├── devbox-entry.sh        # Devbox container entrypoint
│   └── tool-versions.json     # Canonical image and CI tool versions
├── devbox                     # Main launcher script
├── opencode.json              # OpenCode model and provider configuration
├── scripts/
│   ├── fast-check.sh          # Fast lint + unit test validation
│   └── validate_tool_versions.py # Version consumer consistency check
└── .pre-commit-config.yaml    # Pre-commit hook definitions
```

---

## Testing

Tests live under `tests/` and run with [pytest](https://pytest.org). Install
the test extras and invoke pytest through `uv` -- this is the one documented
way to run the suite, locally and in CI:

```shell
uv run --extra test pytest
```

Tests are organized with markers:

- `unit` - Fast, fully isolated tests of `devbox`'s argument parsing and
  helper logic (no real Podman required).
- `container` - Static and smoke checks against a built `devbox` image.
- `integration` - Container lifecycle and nested Podman-in-Podman checks.
- `e2e_inference` - End-to-end checks against real LLM provider APIs.

CI runs `uv run --extra test pytest -m "not e2e_inference"`; the `container`
and `integration` markers still run in CI (they require Podman, which is
available there) but `e2e_inference` is opt-in since it needs real provider
credentials.

### Isolated by default

Every test except `e2e_inference` runs inside a credential-isolated
environment (see `tests/conftest.py`): an autouse fixture scrubs known
provider/integration credential environment variables
(`CREDENTIAL_ENV_VARS`) before each test, and the `isolated_env`/
`isolated_home` fixtures give `devbox`-launching tests a fresh, empty `$HOME`
and XDG directories. This means:

- Unit and container/integration tests produce the same result whether or
  not the machine running them has `GEMINI_API_KEY`, a `gh auth login`
  session, gcloud application-default credentials, OpenCode state, etc.
- Host CLI configuration and credential files under `$HOME` cannot
  influence a test unless the test explicitly creates them under its own
  `isolated_home`.
- Mock command logs and test diagnostics never contain host credential
  values by default.

Tests that need to exercise credential passthrough behavior opt in
explicitly, for example:

```python
def test_devbox_gemini_env(devbox_path, mock_podman_env, tmp_path):
    env, log_file = mock_podman_env
    env["GEMINI_API_KEY"] = "mock-gemini-token"  # pragma: allowlist secret
    ...
```

`tests/e2e_inference` is the intentional boundary: those tests are real
end-to-end checks and are exempt from the scrub, reading actual provider
credentials (e.g. `GEMINI_API_KEY`, `LITEMAAS_API_KEY`) from the environment
and skipping themselves when a credential isn't set. Run them explicitly
with the relevant credentials exported:

```shell
GEMINI_API_KEY=... uv run --extra test pytest -m e2e_inference
```

## Code Quality & Pre-Commit

This repository uses [pre-commit](https://pre-commit.com) to validate code
quality, container definitions, YAML, and Markdown files.

### Running Checks Locally

```shell
# Run pre-commit across all files
./.github/lint-all.sh

# Or run directly via pre-commit
pre-commit run --all-files
```

Container tests build and reuse an image tag derived from the contents of the
`container/` build context, so changes to the Dockerfile or copied files use a
fresh test image instead of an unrelated `devbox:latest` image.

### Validation Levels

Two documented validation commands cover different stages of iterating on a
change. Both run unmodified inside a fresh devbox, since the `pre-commit` and
`uv` executables are preinstalled in the devbox image; no manual tool
installation is required. Pre-commit still downloads and caches each hook's
own environment on its first invocation in a new container, so only that
very first run pays a one-time, network-dependent setup cost. Because devbox
containers persist across sessions, later runs reuse the cache and stay fast.

| Command | Checks | Approximate cost |
| --- | --- | --- |
| `./scripts/fast-check.sh` | Pre-commit lint hooks and unit tests (`tests/unit`) | Seconds after the first run; no container image build |
| `uv run --extra test pytest -m "not e2e_inference"` | Everything above plus container image build/smoke tests and container lifecycle/nested-Podman integration tests | Several minutes; builds the devbox container image |

Use `./scripts/fast-check.sh` while iterating on launcher scripts,
configuration, or documentation, then run the full command before opening a
pull request. CI always runs the full command (see
[`.github/workflows/ci-workflow.yaml`](.github/workflows/ci-workflow.yaml)) as
the final validation, along with `pre-commit run -a` for the same lint hooks
`fast-check.sh` runs.

Tests are grouped with pytest markers (`unit`, `container`, `integration`,
`e2e_inference`; see [`pyproject.toml`](pyproject.toml)), so any subset can
also be run directly, e.g. `uv run --extra test pytest -m unit`. The
`e2e_inference` marker is excluded from both documented commands above
because it calls real LLM provider APIs and requires provider credentials.

### Managing Tool Versions

Pinned versions for tools installed in the devbox image or CI are maintained in
[`container/tool-versions.json`](container/tool-versions.json). The Dockerfile
and workflow read that manifest directly. Pre-commit requires literal `rev`
values, so its revisions are checked against the manifest by the validation
hook.

Run the consistency check directly when changing a tool version:

```shell
python3 scripts/validate_tool_versions.py
```

The Renovate configuration updates the manifest and groups related pre-commit
consumer updates so a version change remains synchronized.
