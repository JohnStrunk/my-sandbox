# my-sandbox

A secure, rootless containerized development environment tailored for
AI-assisted coding and modern software development workflows.

This repository provides:

- **`create-devbox`**: A single-command launcher that starts an interactive,
  fully rootless development container with nested container support
  (Podman-in-Podman) and automatic credential passthrough.
- **`devbox/`**: A container image definition bundling modern language
  toolchains, cloud CLIs, code linters, and AI coding assistants like
  [OpenCode](https://opencode.ai).
- **`local-llm/`**: A self-contained local LLM inference server based on
  [Ollama](https://ollama.com), enabling offline or local AI model execution
  with zero cloud API dependencies.

---

## Key Features

- **Fully Rootless & Secure**: Runs via Podman using `--userns=keep-id` without
  requiring `--privileged` mode or added Linux capabilities. Files created
  inside the container remain owned by the host user.
- **Nested Podman-in-Podman**: Build and run containers inside the devbox
  without host root permissions. Uses `fuse-overlayfs` and dynamic subordinate
  UID/GID delegation (`/etc/subuid` and `/etc/subgid`).
- **Comprehensive Toolchain**:
  - **Languages & Runtimes**: Go, Python packaging via `uv` and `uvx`, Node.js,
    and Playwright with full browser dependencies.
  - **Cloud & Productivity CLIs**: GitHub CLI (`gh`), GitLab CLI (`glab`),
    Google Cloud SDK (`gcloud`), Google Workspace CLI (`gws`), Atlassian CLI
    (`acli`), Google Antigravity (`agy`), and OpenCode (`opencode`).
  - **Linters & Utilities**: `ripgrep`, `jq`, `shellcheck`, `hadolint`,
    `markdownlint-cli2`, and `ffmpeg`.
- **Automatic Host Credential & Config Passthrough**: `create-devbox` detects
  and bind-mounts existing host configurations (GitHub tokens, Google Cloud ADC,
  Atlassian CLI, Google Workspace, LiteMaaS API keys, and OpenCode state).
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

To launch a devbox mounted to the current directory:

```shell
./create-devbox
```

To bind-mount a specific project workspace into the devbox:

```shell
./create-devbox /path/to/project
```

The script builds the `devbox:latest` container image (if not already built),
configures user namespace delegations, passes relevant host configuration and
environment variables, and opens an interactive bash shell in `/sandbox`.

### Working Inside the Devbox

Inside the container, you can run development commands, build projects, execute
nested containers, or invoke AI assistants:

```shell
# Nested container execution
podman run --rm alpine uname -a

# Run OpenCode with configured models
opencode
```

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
├── devbox/
│   ├── Dockerfile             # Multi-stage container definition
│   └── devbox-entry.sh        # Devbox container entrypoint
├── local-llm/
│   ├── README.md              # Local LLM documentation
│   ├── start-llm-server.sh    # Script to start local Ollama server
│   ├── status-llm-server.sh   # Script to check Ollama status
│   └── stop-llm-server.sh     # Script to stop Ollama server
├── create-devbox              # Main launcher script
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
