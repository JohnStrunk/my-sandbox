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
- **Git-Safe Worktree Mounts**: When launched from a linked Git worktree,
  `devbox` also bind-mounts the repository's git directory at the same host
  path inside the container, so the worktree's `.git` pointer file resolves
  and Git commands keep working from the mounted worktree. Containers
  created earlier pick this up on the next `devbox --recreate`.
- **Nested Podman-in-Podman**: Build and run containers inside the devbox
  without host root permissions. Uses `fuse-overlayfs` and dynamic subordinate
  UID/GID delegation (`/etc/subuid` and `/etc/subgid`).
- **Persistent Shared Data**: The knowledge base plus `uv`, pre-commit, and
  nested Podman/Buildah image storage survive `devbox --recreate`, so
  recreating a container doesn't lose the knowledge base or repeat downloads
  and image builds whose inputs haven't changed.
- **Nested Docker-Compatible API**: Every devbox starts a rootless Podman
  API service reachable at a stable `DOCKER_HOST`, so Docker API clients like
  [Testcontainers](https://testcontainers.com) or Dockerode work out of the
  box, including published container ports.
- **Comprehensive Toolchain**:
  - **Languages & Runtimes**: Go, Rust, Python packaging via `uv` and `uvx`,
    Node.js, and Playwright CLI with a bundled Chromium browser.
  - **Cloud & Productivity CLIs**: GitHub CLI (`gh`), GitLab CLI (`glab`),
    Google Cloud SDK (`gcloud`), Google Workspace CLI (`gws`), Atlassian CLI
    (`acli`), Google Antigravity (`agy`), OpenCode (`opencode`), Repomix
    (`repomix`), ast-grep (`ast-grep`, `sg`), and Semble
    (`semble`).
  - **Linters & Utilities**: `pre-commit`, `ripgrep`, `jq`, `shellcheck`,
    `hadolint`, `markdownlint-cli2`, `ffmpeg`, `file` for release artifacts,
    and process diagnostics (`ps`, `pgrep`) via `procps-ng`.
  - **Agent token-hygiene utilities**: Fedora 44 packages `tokei` 14.0.0,
    `just` 1.57.0, `difftastic` 0.69.0 (command `difft`), `hyperfine` 1.20.0,
    and `fd-find` 10.4.2 (command `fd`).
- **Automatic Host Credential & Config Passthrough**: `devbox` detects and
  bind-mounts existing host configurations (GitHub tokens, Google Cloud ADC,
  Atlassian CLI, Google Workspace, LiteMaaS API keys, and OpenCode
  configuration, state, and session data), and passes supported API credentials
  and endpoints such as Anthropic's directly into the container. When a GitHub
  token is available, it also enables the OpenCode GitHub MCP server without
  modifying any mounted OpenCode configuration file.
- **Image-Owned Agent Capability Catalog**: The `devbox-tools` skill is staged
  into every container's active `.agents/skills` directory after host skills
  are mounted. It is the image-wide place for instructions that cannot live in
  one project's `AGENTS.md` or README.

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
each project directory gets its own persistent container. The first run builds
or reuses an image tagged from the launcher and container-context fingerprint.
When `devbox` is used from another project, that image is also assigned to the
shared `devbox:latest` tag, so normal consumers keep sharing one image. When
developing `my-sandbox` from a checkout or worktree, the launcher uses the
context-specific tag directly and leaves `devbox:latest` untouched. A shared
per-user Podman runtime lock serializes launcher lifecycle operations across
worktrees. The launcher then configures user
namespace delegations, passes relevant host configuration and environment
variables, and opens an interactive bash shell in the bind-mounted directory.
Subsequent runs from the same directory just exec a new shell into the
existing container (starting it first if it's stopped).

The launcher records a fingerprint of the image build context and of the
launcher script itself on each new container and checks it, along with the
image ID, on subsequent runs. If the Dockerfile or another file in
`container/` changed, the image was rebuilt, or the launcher script changed
(since bind mounts, injected environment, and exec contracts also live in
that script), the launcher warns that the existing container is stale and
prints the `devbox --recreate` command needed to refresh it. Recreating
removes only the container; the host-backed project directory remains intact.

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

### Python Environments (`.venv`)

A `.venv` created on the host records host-only interpreter paths and script
shebangs, so reusing one inside a container fails (typically `Failed to spawn:
pytest`) whenever `uv run` -- or anything else that discovers `.venv` -- picks
it up from a bind-mounted project. To keep the documented
`uv run --extra test pytest` workflow working, `devbox` shadows a project
`.venv` **directory** with a per-project Podman named volume
(`devbox-venv-<dirname>`) whenever one exists at container-creation time:

- Inside the container, `.venv` starts empty and `uv run` creates a
  container-native environment in it, so host shebangs are never executed.
- The host `.venv` is only masked, never read or written, and is not created
  for projects that don't already have one.
- The volume survives `devbox --recreate`; `uv run` re-syncs the environment
  against the lockfile on every invocation. Clear it with
  `podman volume rm devbox-venv-<dirname>`.
- A `.venv` that is a _symlink_ (or any other non-directory) can't be shadowed
  safely; `devbox` warns and leaves it alone. Recreate it inside devbox instead
  (`rm .venv && uv sync`).
- Only the project root's `.venv` is shadowed; virtualenvs in nested
  subprojects are not.
- The shadow is established at container creation. If entering an older
  container (created before the project had a `.venv`, or before this feature
  existed) whose host project now has one, `devbox` warns that it isn't
  shadowed; run `devbox --recreate` to pick up the mount.

### Go Project Toolchains

The image includes a manifest-pinned Go toolchain, but a project can require an
older version for its build or analysis tools. From a Go project, use
`devbox-go` to select the version declared by the active `go.work` or `go.mod`:

```shell
devbox-go --doctor
devbox-go version
devbox-go test ./...
devbox-go install <tool-module>@<version>
devbox-go run make test
```

The workspace file takes precedence over a module file. `devbox-go --doctor`
reports the selected version and the `GOTOOLCHAIN` value it applies. A missing
project file is an error so an unrelated image default is never selected by
accident. If a project file has no explicit version, the doctor reports that
Go's default selection is being used. Go can download the selected toolchain
through its normal proxy and checksum flow; downloaded toolchains and the module
cache are stored in the persistent `devbox-go-cache` volume. Use
`devbox --recreate` after image changes, but do not need to redownload a
toolchain already present in that volume.

`devbox-go run` exports `GOTOOLCHAIN` to a child command that invokes `go`; it
cannot change the Go runtime embedded in an already-compiled binary. Install
source-built Go tools with `devbox-go install` so the selected toolchain builds
them; installed binaries are placed in the persistent Go cache's `bin` path,
which the image adds to `PATH`. Use a precompiled tool's own version-selection
mechanism otherwise.

The image includes `gcc` and `glibc-devel` for Go projects that use cgo,
including the race detector with `CGO_ENABLED=1 go test -race ./...`.
Pure-Go static builds with `CGO_ENABLED=0` remain supported.

### Git Identity & GitHub Authentication

When a container is created, `devbox` configures a Git identity inside it: any
`user.name`/`user.email` already set inside that (persistent) container is
left untouched, and anything still missing is filled in from the invoking
host user's own `git config --global user.name`/`user.email` -- the host's
Git config is only ever read, never modified. If neither source has an
identity, `devbox` prints a warning explaining how to set one with
`git config --global user.name/user.email` inside the container.

The devbox has no usable SSH access to GitHub, so GitHub SSH remotes are routed
to HTTPS inside the container. Operations that require authentication,
including pushes and private-repository access, use the `gh` CLI. When a
mounted repository has a GitHub SSH remote such as
`git@github.com:OWNER/REPO.git` or
`ssh://git@github.com/OWNER/REPO.git`, `devbox` rewrites it to HTTPS through a
container-local Git configuration rule. This leaves the host Git config and
the mounted repository's `.git/config` unchanged, and does not affect SSH
remotes for other hosts. GitHub host SSH keys are not mounted, and SSH host-key
verification is not disabled.

Whenever a GitHub token is available (see the GitHub integration below),
`devbox` runs `gh auth setup-git --hostname github.com --force` inside the
container automatically. The explicit hostname and `--force` also register
Git's credential helper when the token is supplied only through
`GH_TOKEN`/`GITHUB_TOKEN` and no stored `gh` host configuration exists. The
helper supplies credentials for authenticated clone/fetch/push operations;
public repository reads can still be anonymous. Without a token, the launcher
prints a warning with the manual `gh auth login` and `gh auth setup-git` steps.
Run `devbox --recreate` to apply the URL rewrite to an existing persistent
container.

### Persistent Storage

`devbox` backs a few directories with storage that survives
`devbox --recreate` (and container removal in general). This keeps the
knowledge base available to every devbox instance and avoids repeating
downloads or nested image builds whose inputs haven't changed:

| Path | Backing | Notes |
| --- | --- | --- |
| `/sandbox/kb` | Podman named volume `devbox-kb` | Shared knowledge-base checkout. |
| `/sandbox/.cache/go` | Podman named volume `devbox-go-cache` | Go module and downloaded toolchain cache. |
| `/sandbox/.uv_cache` | Podman named volume `devbox-uv-cache` | `uv`/`uvx` package downloads. |
| `/sandbox/.cache/pre-commit` | Podman named volume `devbox-precommit-cache` | Pre-commit hook environments. |
| `/sandbox/.cache/semble` | Podman named volume `devbox-semble-cache` | Semble's mtime-incremental code indexes. |
| `/sandbox/.local/share/containers/storage` | Host directory `${XDG_CACHE_HOME:-~/.cache}/devbox/containers-storage` | Nested Podman/Buildah's own image and layer storage. |
| `/sandbox/<project>/.venv` | Podman named volume `devbox-venv-<dirname>` | Container-local shadow of a host-created `.venv` (per project; see [Python Environments](#python-environments-venv)). |

The knowledge base, `uv`, and pre-commit caches use Podman-managed named
volumes because their exact host-side location doesn't matter. Nested
Podman/Buildah's storage instead uses a plain host directory so its size can be
inspected and pruned with ordinary tools (`du -sh`, `rm -rf`) without needing
`podman volume` commands.

The shared cache entries above are shared across _every_ devbox instance, not
just one project's container, and the per-project `.venv` shadow volume
likewise survives its container, so all of them persist even across
`devbox --remove`; only deleting the volume/directory itself clears them:

```shell
# Shared knowledge base
podman volume rm devbox-kb

# Go, uv, pre-commit, and Semble caches
podman volume rm devbox-go-cache devbox-uv-cache devbox-precommit-cache devbox-semble-cache

# Nested Podman/Buildah image and layer storage
rm -rf "${XDG_CACHE_HOME:-$HOME/.cache}/devbox/containers-storage"
```

This is local, runtime cache persistence between devbox sessions on one
machine. It's a different scope from the GitHub Actions layer caching that
speeds up building the `devbox:latest` image itself in CI (see
`.github/workflows/`); the two don't share storage.

### Nested Docker-Compatible API

Every devbox starts a rootless Podman API service on the socket `DOCKER_HOST`
already points at, so Docker API clients work with no extra setup:

```shell
printf '%s\n' "$DOCKER_HOST"                                    # unix:///sandbox/.docker/run/docker.sock
curl --unix-socket "${DOCKER_HOST#unix://}" http://localhost/_ping  # OK
devbox-docker-api-check                                         # human-readable diagnostic
```

Containers created through this API (including by clients that don't request
a network explicitly, such as Testcontainers' `GenericContainer`) use
[`pasta`](https://passt.top/) for networking when the host cannot configure
the nested bridge prerequisites. Pasta supports published ports without
needing any Linux capabilities beyond what the devbox already has. When the
host accepts the launch-time sysctls and the nested bridge preflight passes,
`devbox` switches the nested default to the `netavark` bridge backend so Docker
API clients can also attach containers to user-defined networks.

`pasta` itself does not support **user-defined bridge networks** (`podman
network create`, or Testcontainers' `Network` class with container aliases).
Those need nested Podman's `netavark` bridge backend, which in turn needs a
handful of namespaced IPv4/IPv6 sysctls that the _host_ running `devbox` must
be able to preconfigure on the outer container. `devbox` attempts this and an
actual temporary network/published-port probe automatically, then falls back
to a pasta-only devbox (still fully functional for everything else) if either
check fails. Run `devbox-docker-api-check
--require-user-networks` inside the devbox to check which case applies.

If the API service itself fails to start (rare -- e.g. a host that also
blocks nested user namespaces), `devbox` prints a warning but still starts
the container normally; regular devbox usage, including plain nested
`podman run`, is unaffected.

### Automatic OpenCode Integrations

The integrations below are enabled when their requirements are present while a
devbox container is created:

| Name | Description | Requirements |
| --- | --- | --- |
| GitHub | GitHub repository, issue, pull request, and code search capabilities through the pinned local MCP server. | At least one of `GH_TOKEN`, `GITHUB_TOKEN`, or an authenticated host `gh` CLI. |
| The Source | Search and fetch capabilities for The Source, Red Hat's intranet. | All of `IGLOO_MCP_COMMUNITY`, `IGLOO_MCP_COMMUNITY_KEY`, `IGLOO_MCP_APP_PASS`, `IGLOO_MCP_APP_ID`, `IGLOO_MCP_USERNAME`, and `IGLOO_MCP_PASSWORD`. |
| Context7 | Up-to-date documentation and code examples for software libraries. | `CONTEXT7_API_KEY`. |
| Tavily | Web search, extraction, crawling, and mapping through Tavily's remote MCP server. | `TAVILY_API_KEY`. |
| Semble | Natural-language semantic code search through the local OpenCode MCP server. | Always enabled; included in the devbox image. |
| Anthropic | Direct Anthropic models, including Anthropic-compatible endpoints. | `ANTHROPIC_API_KEY` enables the built-in provider; optional `ANTHROPIC_BASE_URL` selects a custom endpoint. |
| PriceTag (OpenAI gateway) | Routes the built-in `openai` provider and its model catalog through the PriceTag OpenAI-compatible gateway. | Both `PRICETAG_OPENAI_URL` and `PRICETAG_API_KEY`. |
| PriceTag (Anthropic gateway) | Routes the built-in `anthropic` provider and its model catalog through the PriceTag Anthropic-compatible gateway. | Both `PRICETAG_ANTHROPIC_URL` and `PRICETAG_API_KEY`. |
| PriceTag (Hosted) | Static hosted Qwen 3.8 Flash Next and GLM 5.3 model definitions (262,144 context tokens, 128,000 output tokens; Qwen offers low/medium/xhigh effort, GLM offers low/high/max effort; GLM is text-only). | Both `PRICETAG_HOSTED_URL` and `PRICETAG_API_KEY`. |
| OCTO Open Models | OpenAI-compatible Qwen 3.8 Frontier, Core, and Bulk models. | Both `OCTO_OPEN_URL` (gateway `/v1` URL) and `OCTO_OPEN_KEY`. |

Runtime integrations can contribute any top-level OpenCode config property, with
multiple MCP integrations combined under one `mcp` object in
`OPENCODE_CONFIG_CONTENT`. The user's global `~/.config/opencode`
configuration remains unchanged. The generated config also sets baseline
`external_directory` permissions and native OpenCode v2 `provider.use` deny
policies for the `github-copilot` and `gitlab` providers. Since the container
is persistent, use `devbox --recreate` after adding or changing host
credentials or integration triggers. On creation, the launcher also runs
`opencode models` inside the container to start OpenCode v2's background service
and load the model catalog before the first interactive client starts. If the
command fails, devbox reports a warning and leaves any existing cache in place.

The GitHub MCP server runs locally from the pinned image binary through a
response-bounding proxy, which keeps search behavior versioned and testable.
Search tools require explicit field selection, clamp `perPage` to 20 or less,
and cap serialized results at 64 KiB. Use page-based pagination and fetch full
issue or pull-request bodies with a targeted read after identifying the relevant
result.

When both Octo variables are set, select the models with these OpenCode IDs:
`octo-open/qwen38-27b-frontier`, `octo-open/qwen38-flash-next`, and
`octo-open/qwen38-27b-fast`. The portal and key setup are available at
<https://octo-app-octo-models.apps.emerg.pcbk.p1.openshiftapps.com/>; use its
gateway `/v1` URL as `OCTO_OPEN_URL`.

When both PriceTag Hosted variables are set, select
`pricetag-hosted/Inferact/Qwen3.8-Flash-Next-NVFP4` or
`pricetag-hosted/rits/zai-org/glm-5-3`. The model catalog is configured
statically; the launcher does not probe PriceTag during startup.

When a PriceTag OpenAI or Anthropic gateway URL is set, the corresponding
built-in provider is redirected to that gateway with `PRICETAG_API_KEY`, and
models keep their built-in catalog IDs (`openai/<model-id>`,
`anthropic/<model-id>`). OpenCode v2 resolves a built-in provider's credential
from its environment connection (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`) in
preference to a provider override's `settings.apiKey`, so a direct-provider key
passed through from the host would be sent to the gateway and rejected with
HTTP 401. The generated overrides therefore clear the provider's environment
credential list (`"env": []`), which makes the gateway key authoritative while
leaving the direct-provider variable itself available to other tools in the
container.

OpenCode automatically discovers its built-in Anthropic provider from
`ANTHROPIC_API_KEY` and the Anthropic SDK uses `ANTHROPIC_BASE_URL` for a custom
compatible endpoint, so no generated provider configuration is required. Select
an Anthropic model with `anthropic/<model-id>`.

### Agent Capability Registration

Installing a binary proves runtime availability only. Every agent-facing image
capability must also be visible to the agent through the image-owned
`devbox-tools` skill, explicit MCP/plugin/wrapper configuration, or both.
Capabilities must document their trigger, exact command or integration name,
safe invocation, fallback, and a test proving that an agent can discover or
invoke them.

The skill is copied into `/sandbox/.agents/skills` during container startup,
after the launcher mounts any host `.agents` directory. This keeps the
capability catalog available for arbitrary project repositories without
modifying their `AGENTS.md`, README, or mounted OpenCode configuration.

When adding or changing an image capability, recreate persistent containers:

```shell
devbox --recreate
```

For syntax-aware code searches and structural rewrites, use the staged
`ast-grep` skill and command instead of a text-only edit loop:

```shell
ast-grep --lang python -p 'print($ARG)' -r 'logger.info($ARG)' -U path/to/file.py
```

Test a pattern or rule against a fixture first, then use `rg` for plain-text
searches where syntax is not relevant.

For one-shot repository snapshots, use the image-installed `repomix` command.
Always pass an explicit token budget so an oversized artifact fails rather than
silently exceeding an agent's context window:

```shell
repomix --token-budget 12000 --compress
```

Use `--no-files` for a cheap directory and metadata map. Repomix's default
Secretlint scan remains enabled, so do not pass `--no-security-check` in agent
workflows. Use Semble for natural-language searches over a live repository, or
`rg` for targeted text searches when a portable snapshot is not needed.

For vague natural-language code searches, use Semble before broad text searches:

```shell
semble search "where are failed requests retried" . --json
```

Semble combines lexical and local static-embedding search. Its embedding model
is included in the image, and its incremental index is stored in the shared
`devbox-semble-cache` volume, so queries do not need an API key or network after
the image is built. Use ast-grep for syntax-aware structural queries, or `rg`
for exact literal matches.

---

## Repository Structure

```text
.
├── .github/
│   ├── workflows/             # GitHub Actions CI workflows
│   ├── ISSUE_TEMPLATE/        # Issue forms with triage label defaults
│   ├── lint-all.sh            # Script to run pre-commit across all files
│   ├── markdownlint-cli2.yaml # Markdown lint configuration
│   ├── mergify.yml            # Mergify PR automation rules
│   └── renovate.json5         # Renovate dependency updates
├── container/
│   ├── agent-skills/          # Image-owned agent capability guidance
│   ├── Dockerfile             # Container definition
│   ├── devbox-entry.sh        # Devbox container entrypoint
│   └── tool-versions.json     # Canonical image and CI tool versions
├── devbox                     # Main launcher script
├── scripts/
│   ├── fast-check.sh          # Fast lint + unit test validation
│   ├── validate_tool_versions.py # Version consumer consistency check
│   └── verify_provenance.py   # Recompute/verify release checksums
└── .pre-commit-config.yaml    # Pre-commit hook definitions
```

---

## Issue Triage

Every open work issue carries labels that let an agent or human pick the
next issue from a single list query, without opening bodies: exactly one
status label (`ready` or `blocked`), one value label (`value:high`,
`value:medium`, `value:low`), and one confidence label
(`confidence:high`, `confidence:medium`, `confidence:low`); `trial` marks
timeboxed experiments. Real dependencies use GitHub's native "Blocked by"
relationship. The issue forms in
[`.github/ISSUE_TEMPLATE/`](.github/ISSUE_TEMPLATE) preselect the
defaults; [`AGENTS.md`](AGENTS.md) documents the full vocabulary and the
selection order.

---

## Testing

Tests live under `tests/` and run with [pytest](https://pytest.org). Install
the test extras and invoke pytest through `uv`; this is the underlying command
used by the sanitized wrapper locally and in CI:

```shell
uv run --extra test pytest -m "not e2e_inference and not cold_bootstrap"
```

Tests are organized with markers:

- `unit` - Fast, fully isolated tests of `devbox`'s argument parsing and
  helper logic (no real Podman required).
- `container` - Static and smoke checks against a built `devbox` image.
- `integration` - Container lifecycle and nested Podman-in-Podman checks.
- `cold_bootstrap` - Cold-cache `pre-commit` bootstrap check inside the
  devbox image. Deselected by default because it initializes every hook
  environment from an empty cache; CI runs it as its own path-gated step in
  the `Automated Tests` job instead of on every pull request's full suite.
- `e2e_inference` - End-to-end checks against real LLM provider APIs.

CI runs the sanitized wrapper around the underlying pytest command. The
`container` and `integration` markers still run in CI (they require Podman,
which is available there). The `cold_bootstrap` marker runs as a dedicated
step that a `changes` job path-gates on pre-commit/container/CI-relevant
edits, and a daily schedule always includes it. `e2e_inference` is opt-in
since it needs real provider credentials.

### Isolated by default

Every test except `e2e_inference` runs inside a credential-isolated
environment (see `tests/conftest.py`): an autouse fixture scrubs known
provider/integration credential environment variables
(`CREDENTIAL_ENV_VARS`) and host config override variables
(`HOST_CONFIG_ENV_VARS`) before each test, and the `isolated_env`/
`isolated_home` fixtures give `devbox`-launching tests a fresh, empty `$HOME`
and XDG directories. Real launcher integration tests use an explicit local
Podman shim that restores only the non-secret runtime settings needed by the
outer Podman process; those settings are not passed into the test containers.
When starting tests directly from the host, use the reusable wrapper below so
the test process receives the same isolation boundary. This means:

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

### Sanitized host test wrapper

Use `scripts/sanitized-test.sh` for a one-shot sanitized command-line run. It
starts the command with a temporary `HOME` and XDG tree and passes only a
small non-secret allowlist (`PATH`, temporary directories, locale, terminal,
and CI markers). Provider credentials, authenticated CLI state, host config
override variables, and arbitrary host variables are not passed to the test
command.

For unit tests or lint checks, which do not require Podman, run:

```shell
./scripts/sanitized-test.sh -- ./scripts/fast-check.sh
```

For container and integration tests, add `--require-podman`:

```shell
./scripts/sanitized-test.sh --require-podman -- \
  uv run --extra test pytest -m "not e2e_inference and not cold_bootstrap"
```

`--require-podman` uses `docker.io/library/alpine:3.22` for its bounded real
container probe. Pre-pull a locally available probe image and select it with
`--podman-probe-image IMAGE` when registry access requires custom proxy or CA
setup; command arguments after `--` run only after this preflight. It takes the
same per-user Podman runtime lock as the launcher and holds it across the probe
and test command, so separate worktrees do not concurrently mutate or probe the
shared Podman store. The probe has a unique name and the wrapper removes it on
failure or interruption; launcher subprocesses inherit the lock-held marker to
avoid deadlocking against their parent test run.

The wrapper gives the outer `podman` process an isolated `HOME`, a temporary
Podman config root containing only the allowlisted non-secret container config
files, explicit host graph/run roots for the image store and runtime, the
host `PATH`, an empty `REGISTRY_AUTH_FILE`, and an empty `DOCKER_CONFIG`
directory. It does not pass whole host XDG trees to Podman. It explicitly
removes provider credentials and other CLI credential/config override
variables from that process too. The `--require-podman` preflight verifies
that rootless Podman is usable before starting the test command; a missing or
unusable runtime returns status `125` with an infrastructure or registry
diagnostic, so it is not confused with a product test failure. Proxy and
provider variables are intentionally not inherited. Do not use this wrapper for
`e2e_inference`, whose purpose is to read real provider credentials.

For parallel tests in a resource-constrained devbox, add
`--resource-preflight`:

```shell
./scripts/sanitized-test.sh --resource-preflight -- \
  go test ./...
```

The resource preflight reports the cgroup PID limit/current usage, CPU quota,
and memory limit/current usage before the command starts. It checks both cgroup
v2 and v1 layouts. A conservative PID, CPU, or memory budget is classified as
an infrastructure limitation and returns status `125` without starting the
test command. If the cgroup root, a mounted limit, or current usage cannot be
read, the preflight reports it as unavailable and applies the same
infrastructure classification rather than assuming the resource is unlimited.
The diagnostic recommends a bounded `GOMAXPROCS`, `go test -p`, and Ginkgo
`--nodes` setting; for example, rerun a constrained Go suite with:

```shell
./scripts/sanitized-test.sh -- \
  env GOMAXPROCS=2 go test -p 1 ./...
```

The resource check is opt-in, and it runs under the same `env -i` allowlist as
the test command, so credentials and unrelated host variables are not exposed.

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
A dedicated test (marked `cold_bootstrap`) also runs every hook inside the
freshly built devbox image with an empty `PRE_COMMIT_HOME`. This verifies the
image's actual toolchain instead of allowing the host CI job's restored cache
to hide a bootstrap failure. Running it from an empty cache costs a minute or
more and depends on hook-repository availability, so CI keeps it out of the
default suite: a `changes` job gates the cold-bootstrap step so it runs on a
pull request only when the change touches pre-commit configuration,
`container/`, tool manifests, CI setup, or the check itself, and a daily
schedule on `main` always exercises the cold path. The current hook set has
no Ruby-language hook; adding one
requires declaring and provisioning its runtime in both the devbox image and
CI rather than relying on the cache.

| Command | Checks | Approximate cost |
| --- | --- | --- |
| `./scripts/fast-check.sh` | Pre-commit lint hooks and unit tests (`tests/unit`) | Seconds after the first run; no container image build |
| `uv run --extra test pytest -m "not e2e_inference and not cold_bootstrap"` | Everything above plus container image build/smoke tests and container lifecycle/nested-Podman integration tests | Several minutes; builds the devbox container image |
| `uv run --extra test pytest -m cold_bootstrap` | Cold-cache `pre-commit` bootstrap inside the freshly built devbox image | Roughly two extra minutes beyond the row above; downloads every hook environment |

Use `./scripts/fast-check.sh` while iterating on launcher scripts,
configuration, or documentation, then run the full command before opening a
pull request. CI always runs the full command (see
[`.github/workflows/ci-workflow.yaml`](.github/workflows/ci-workflow.yaml)) as
the final validation, along with `pre-commit run -a` for the same lint hooks
`fast-check.sh` runs and the path-gated cold-bootstrap step described above.

Tests are grouped with pytest markers (`unit`, `container`, `integration`,
`cold_bootstrap`, `e2e_inference`; see [`pyproject.toml`](pyproject.toml)),
so any subset can also be run directly, e.g. `uv run --extra test pytest -m
unit`. The `e2e_inference` and `cold_bootstrap` markers are excluded from the
documented full command above: `e2e_inference` because it calls real LLM
provider APIs and requires provider credentials, and `cold_bootstrap`
because CI runs it in its own path-gated and scheduled job.

### Managing Tool Versions

Pinned versions for tools installed in the devbox image or CI are maintained in
[`container/tool-versions.json`](container/tool-versions.json). The Dockerfile
and workflow read that manifest directly. Pre-commit requires literal `rev`
values, so its revisions are checked against the manifest by the validation
hook.

For local inspection of a downloaded Linux release artifact, prefer
`file /path/to/artifact` to identify its format and architecture before
execution. This checks the artifact's contents; it does not establish its
publisher or integrity. The checksum workflow below validates expected digests
for release assets pinned in `container/tool-versions.json`; it does not verify
arbitrary downloads or authenticate a publisher. For other artifacts, verify
the checksum and publisher signature or attestation using the project's trusted
release metadata.

Run the consistency check directly when changing a tool version:

```shell
python3 scripts/validate_tool_versions.py
```

The Renovate configuration updates the manifest and groups related pre-commit
consumer updates so a version change remains synchronized.

Some entries also pin release-provenance metadata that Renovate cannot
recompute: `ast_grep` carries per-platform release checksums and the official
agent-skill archive hash, while `github_mcp_server` carries per-platform
release checksums. Every such entry declares a
`provenance.url_templates` block mapping each checksummed field to the asset
that must hash to it, and the Dockerfile must read exactly those fields.

Verify the pinned digests against upstream (this runs in CI, so a version-only
bump fails fast rather than at image-build time), or refresh the whole unit in
place after bumping a version:

```shell
python3 scripts/verify_provenance.py           # check (non-zero if stale)
python3 scripts/verify_provenance.py --update  # recompute and rewrite digests
```

The detect-secrets hook uses `scripts/detect_secrets_filters.py` to ignore only
the intentional SHA-256 values inside the manifest's `checksums` objects. The
manifest is still scanned for every other detector and every other field, so a
real secret must not be added to the checksum metadata. After changing a
version or checksum, run the supported refresh and validation workflow without
editing `.secrets.baseline`:

```shell
python3 scripts/verify_provenance.py --update
pre-commit run detect-secrets --all-files
```

The check retries transient network errors and fails closed: an asset that
cannot be fetched (or has not yet been published for a bumped version) is
reported distinctly from a checksum mismatch, and the command exits non-zero
either way so CI fails rather than silently passing.
