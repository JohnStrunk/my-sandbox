# Switchyard LLM autorouting proxy

Runs a local LLM proxy and autorouting server (via
[Switchyard](https://github.com/NVIDIA-NeMo/Switchyard)) in a Podman container
on your local machine, so that every devbox created from this repo (via
[`../devbox`](../devbox)) can route traffic across models and providers while
preserving OpenAI and Anthropic API compatibility.

This is meant to be started **once, directly on your local machine** (the
Podman host) — not inside a devbox. It keeps running in the background;
devboxes created afterwards connect to it automatically.

## Routing Strategies and Models

| Model / Route | Strategy | Description |
| --- | --- | --- |
| `switchyard-auto` | LLM Classifier | Evaluates prompt complexity with a judge model and dynamically routes to weak or strong tiers. |
| `switchyard-stage` | Stage Router | Inspects conversation progress signals and tool results to select efficient or capable tiers per turn. |
| `switchyard-random` | Random A/B Split | Spreads traffic across configured models according to configured weights. |
| `switchyard-gemini` | Passthrough | Direct passthrough route to Gemini 2.5 Flash. |
| `switchyard-qwen` | Passthrough | Direct passthrough route to LiteMaaS Qwen 3.6 35B. |

## Requirements

- Podman
- API credentials for upstream providers (e.g. `GEMINI_API_KEY`,
  `LITEMAAS_API_KEY`, or `OPENAI_API_KEY`)

## Usage

Run once on your local machine:

```shell
./start-switchyard-proxy.sh
```

This builds the `switchyard-proxy:latest` container image (if missing),
mounts `routes.toml`, passes configured environment variables, and starts the
`opencode-switchyard-proxy` container with its API published to
`127.0.0.1:4000`. The container is started with `--restart unless-stopped`, so
Podman will restart it automatically if it stops or the machine reboots.

Check on it any time:

```shell
./status-switchyard-proxy.sh
```

Stop it:

```shell
./stop-switchyard-proxy.sh
```

### Connecting from a devbox

`../devbox` automatically detects the running `opencode-switchyard-proxy`
container and, if found, starts new devboxes with
`--network slirp4netns:allow_host_loopback=true`. This is rootless Podman's
standard way to let a container reach services bound to the _host's own_
loopback interface: `10.0.2.2` is the fixed gateway address slirp4netns
gives every container for that purpose. No extra steps are needed once the
proxy server has been started; this matches the `switchyard-proxy` provider's
`baseURL` configured in [`../opencode.json`](../opencode.json):

```jsonc
"switchyard-proxy": {
  "npm": "@ai-sdk/openai-compatible",
  "name": "Switchyard Proxy (autorouting)",
  "options": {
    "baseURL": "http://10.0.2.2:4000/v1"
  },
  "models": {
    "switchyard-auto": {
      "name": "Switchyard Auto-router (Capability Classifier)",
      "limit": {
        "context": 131072,
        "output": 32768
      }
    },
    "switchyard-stage": {
      "name": "Switchyard Stage-router (Signals & Progress)",
      "limit": {
        "context": 131072,
        "output": 32768
      }
    },
    "switchyard-random": {
      "name": "Switchyard Random-router (A/B Split)",
      "limit": {
        "context": 131072,
        "output": 32768
      }
    }
  }
}
```

Inside a devbox, select one of the models with `opencode models` /
`/models`, or run a prompt directly:

```shell
opencode run -m switchyard-proxy/switchyard-auto "Explain binary search in 3 sentences"
```

### Configuration

All settings can be overridden with environment variables when running the
scripts, e.g.:

```shell
SWITCHYARD_PORT=4000 ./start-switchyard-proxy.sh
```

| Variable | Default | Purpose |
| --- | --- | --- |
| `SWITCHYARD_IMAGE` | `switchyard-proxy:latest` | Container image tag |
| `SWITCHYARD_CONTAINER_NAME` | `opencode-switchyard-proxy` | Container name; also what `devbox` looks for |
| `SWITCHYARD_PORT` | `4000` | Host port (127.0.0.1) the API is published on |
| `SWITCHYARD_CONFIG` | `./routes.toml` | Path to the Switchyard routes configuration file |
| `GEMINI_API_KEY` | (inherited) | API key for Gemini OpenAI-compatible backend |
| `LITEMAAS_API_KEY` | (inherited) | API key for LiteMaaS backend |

If you change `SWITCHYARD_PORT` or `SWITCHYARD_CONTAINER_NAME`, update
[`../opencode.json`](../opencode.json)'s `switchyard-proxy.options.baseURL`
(and `devbox`'s container check) to match.

## Why Switchyard

- **Protocol Translation**: Seamlessly translates between OpenAI Chat
  Completions, Anthropic Messages, and OpenAI Responses formats.
- **Multi-Backend Routing**: Supports intelligent routing via LLM task
  classification, stage-router heuristics, composite pipelines, and random
  splits.
- **Operational Metrics**: Out-of-the-box Prometheus metrics, GenAI semantic
  spans, and request stats at `/v1/stats` and `/metrics`.
