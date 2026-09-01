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
- **`local-llm/`**: A self-contained local LLM inference server based on
  [Ollama](https://ollama.com), enabling offline or local AI model execution
  with zero cloud API dependencies.
- **`litellm-proxy/`**: An LLM autorouting proxy server based on
  [LiteLLM](https://docs.litellm.ai/docs/proxy/auto_routing), enabling dynamic
  complexity-based model routing across multiple model tiers.
- **`switchyard-proxy/`**: An LLM autorouting proxy server based on
  [Switchyard](https://github.com/NVIDIA-NeMo/Switchyard), providing protocol
  translation and dynamic model routing across providers.

---

## Key Features

- **Fully Rootless & Secure**: Runs via Podman using `--userns=keep-id` without
  requiring `--privileged` mode or added Linux capabilities. Files created
  inside the container remain owned by the host user.
- **Nested Podman-in-Podman**: Build and run containers inside the devbox
  without host root permissions. Uses `fuse-overlayfs` and dynamic subordinate
  UID/GID delegation (`/etc/subuid` and `/etc/subgid`).
- **Comprehensive Toolchain**:
  - **Languages & Runtimes**: Go, Rust, Python packaging via `uv` and `uvx`,
    Node.js, and Playwright with full browser dependencies.
  - **Cloud & Productivity CLIs**: GitHub CLI (`gh`), GitLab CLI (`glab`),
    Google Cloud SDK (`gcloud`), Google Workspace CLI (`gws`), Atlassian CLI
    (`acli`), Google Antigravity (`agy`), and OpenCode (`opencode`).
  - **Linters & Utilities**: `pre-commit`, `ripgrep`, `jq`, `shellcheck`,
    `hadolint`, `markdownlint-cli2`, and `ffmpeg`.
- **Automatic Host Credential & Config Passthrough**: `devbox` detects and
  bind-mounts existing host configurations (GitHub tokens, Google Cloud ADC,
  Atlassian CLI, Google Workspace, LiteMaaS API keys, and OpenCode
  configuration, state, and session data). When a GitHub token is available,
  it also enables the OpenCode GitHub MCP server without modifying any mounted
  OpenCode configuration file.
- **Local LLM Integration**: Devboxes automatically detect running local
  inference containers and configure slirp4netns loopback routing to connect to
  local Ollama models via `10.0.2.2:11434`.

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

### Automatic OpenCode Integrations

The integrations below are enabled when their requirements are present while a
devbox container is created:

| Name | Description | Requirements |
| --- | --- | --- |
| GitHub | GitHub repository, issue, pull request, and code search capabilities. | At least one of `GH_TOKEN`, `GITHUB_TOKEN`, or an authenticated host `gh` CLI. |
| The Source | Search and fetch capabilities for The Source, Red Hat's intranet. | All of `IGLOO_MCP_COMMUNITY`, `IGLOO_MCP_COMMUNITY_KEY`, `IGLOO_MCP_APP_PASS`, `IGLOO_MCP_APP_ID`, `IGLOO_MCP_USERNAME`, and `IGLOO_MCP_PASSWORD`. |
| Context7 | Up-to-date documentation and code examples for software libraries. | `CONTEXT7_API_KEY`. |

Runtime integrations can contribute any top-level OpenCode config property, with
multiple MCP integrations combined under one `mcp` object in
`OPENCODE_CONFIG_CONTENT`. The user's global `~/.config/opencode`
configuration and the project's `opencode.json` remain unchanged. Since the
container is persistent, use `devbox --recreate` after adding or changing host
credentials or integration triggers.

---

## Local LLM Inference Server

The `local-llm/` directory provides scripts to run CPU-quantized large language
models locally via Ollama. This allows devbox instances to perform AI-assisted
development completely offline.

### Quick Start (Host Machine)

Start the local LLM inference server once on your host machine before launching
your devbox:

```shell
# Start the Ollama container and download default models
./local-llm/start-llm-server.sh

# Check the server status and loaded models
./local-llm/status-llm-server.sh

# Stop the server when done
./local-llm/stop-llm-server.sh
```

### Supported Models

- `gpt-oss:20b`: OpenAI open-weight model with MXFP4 quantization.
- `qwen3.8:27b`: Alibaba coding model with q4_K_M quantization and
  multi-token prediction.

For detailed hardware requirements, configuration options, and performance
guidelines, see the [Local LLM README](local-llm/README.md).

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
│   └── devbox-entry.sh        # Devbox container entrypoint
├── local-llm/
│   ├── README.md              # Local LLM documentation
│   ├── start-llm-server.sh    # Script to start local Ollama server
│   ├── status-llm-server.sh   # Script to check Ollama status
│   └── stop-llm-server.sh     # Script to stop Ollama server
├── litellm-proxy/
│   ├── config.yaml            # LiteLLM routing and model configuration
│   ├── README.md              # LiteLLM proxy documentation
│   ├── start-proxy-server.sh  # Script to start LiteLLM autorouting proxy
│   ├── status-proxy-server.sh # Script to check proxy status
│   └── stop-proxy-server.sh   # Script to stop proxy server
├── switchyard-proxy/
│   ├── README.md              # Switchyard proxy documentation
│   ├── Dockerfile             # Switchyard proxy container definition
│   ├── routes.toml            # Switchyard routing configuration
│   ├── server.py              # Switchyard server runner script
│   ├── start-switchyard-proxy.sh # Script to start Switchyard proxy container
│   ├── status-switchyard-proxy.sh # Script to check Switchyard proxy status
│   └── stop-switchyard-proxy.sh # Script to stop Switchyard proxy container
├── devbox                     # Main launcher script
├── opencode.json              # OpenCode model and provider configuration
└── .pre-commit-config.yaml    # Pre-commit hook definitions
```

---

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
