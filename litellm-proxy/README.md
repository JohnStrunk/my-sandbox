# LiteLLM autorouting proxy

Runs an LLM autorouting proxy server (via
[LiteLLM](https://docs.litellm.ai/docs/proxy/auto_routing)) in a Podman
container on your local machine, allowing devboxes created from this repo (via
[`../devbox`](../devbox)) to route requests dynamically across multiple model
tiers (e.g. Gemini, LiteMaaS, local Ollama, OpenAI, Anthropic) based on prompt
complexity.

Like [`../local-llm`](../local-llm), this is meant to be started **once,
directly on your local machine** (the Podman host) — not inside a devbox.
It runs in the background; devboxes created afterwards connect to it
automatically.

## Model tiers & auto routing

LiteLLM's complexity auto-router classifies incoming requests and directs
them to the appropriate tier:

| Tier | Default Model | Purpose |
| --- | --- | --- |
| `SIMPLE` | `gemini-flash` | Lightweight queries, greetings |
| `MEDIUM` | `litemaas-qwen` | Standard coding tasks, function edits |
| `COMPLEX` | `litemaas-qwen` | Architecture analysis, refactoring |
| `REASONING` | `litemaas-qwen` | Step-by-step reasoning, logic proofs |

The router alias is exposed as `smart-router`. Individual models configured
in `config.yaml` can also be called directly.

## Requirements

- Podman
- Container image `ghcr.io/berriai/litellm` (pulled automatically)
- API credentials in environment (e.g. `GEMINI_API_KEY`, `LITEMAAS_API_KEY`,
  `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`), or a running local Ollama instance
  from [`../local-llm`](../local-llm)

## Usage

Run once on your local machine:

```shell
./start-proxy-server.sh
```

This starts the `opencode-litellm-proxy` container with its API published to
`127.0.0.1:4000` and passes through supported API keys from your environment.
The container is started with `--restart unless-stopped`.

Check on it any time:

```shell
./status-proxy-server.sh
```

Stop it:

```shell
./stop-proxy-server.sh
```

### Connecting from a devbox

`../devbox` automatically detects the running `opencode-litellm-proxy`
container and starts new devboxes with
`--network slirp4netns:allow_host_loopback=true`. This allows the devbox
container to reach the proxy running on the host's loopback interface via
`10.0.2.2:4000`.

This matches the `litellm-proxy` provider configured in
[`../opencode.json`](../opencode.json):

```jsonc
"litellm-proxy": {
  "npm": "@ai-sdk/openai-compatible",
  "name": "LiteLLM Proxy (Auto Routing)",
  "options": {
    "baseURL": "http://10.0.2.2:4000/v1"
  },
  "models": {
    "smart-router": {
      "name": "Smart Router (Auto-routed)"
    }
  }
}
```

Inside a devbox, run a prompt directly using the auto-router:

```shell
opencode run -m litellm-proxy/smart-router "Explain how binary search works"
```

### Configuration

All settings can be overridden with environment variables when running the
scripts:

```shell
PROXY_PORT=5000 ./start-proxy-server.sh
```

| Variable | Default | Purpose |
| --- | --- | --- |
| `PROXY_IMAGE` | pinned `ghcr.io/berriai/litellm` | LiteLLM container image |
| `PROXY_CONTAINER_NAME` | `opencode-litellm-proxy` | Container name |
| `PROXY_CONFIG_FILE` | `$HERE/config.yaml` | Proxy configuration YAML |
| `PROXY_PORT` | `4000` | Host port (127.0.0.1) |
| `LITELLM_MASTER_KEY` | (empty) | Optional master key for auth |

If you change `PROXY_PORT` or `PROXY_CONTAINER_NAME`, update
[`../opencode.json`](../opencode.json)'s `litellm-proxy.options.baseURL` to
match.

## Why LiteLLM auto routing

- Single gateway endpoint provides automatic cost and latency optimization
  by matching query complexity to the most appropriate model tier.
- Compatible with OpenAI SDK and standard `@ai-sdk/openai-compatible`
  providers.
- Supports failover, load balancing, and local + cloud hybrid setups.
