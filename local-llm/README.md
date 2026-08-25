# Local LLM inference server

Runs a local, CPU-based LLM inference server (via [Ollama](https://ollama.com))
in a Podman container on your local machine, so that every devbox created
from this repo (via [`../devbox`](../devbox)) can use it as an
OpenCode model provider without needing internet access or API keys.

This is meant to be started **once, directly on your local machine** (the
Podman host) — not inside a devbox. It keeps running in the background;
devboxes created afterwards connect to it automatically.

## Models

| Model | Size (downloaded) | Context | Notes |
| --- | --- | --- | --- |
| `gpt-oss:20b` | ~14GB | 128K | OpenAI's open-weight model, natively MXFP4-quantized. |
| `qwen3.8:27b` | ~18GB | 256K | Alibaba's coding-focused model; q4_K_M quantization + multi-token prediction (MTP) for faster CPU decoding. |

Both are quantized specifically to run well on CPU. Only one model is kept
resident in RAM at a time (Ollama loads/unloads on demand — see
`OLLAMA_MAX_LOADED_MODELS`/`OLLAMA_KEEP_ALIVE` in the script), so peak RAM
usage stays well under 64GB even with the large (131072-token, ~128K)
context window configured for both.

## Requirements

- Podman
- ~35GB free disk space (for both models' weights)
- 64GB RAM recommended for CPU inference at the configured context length

## Usage

Run once on your local machine:

```shell
./start-llm-server.sh
```

This creates a named volume, starts the `opencode-local-llm` container with
its API published to `127.0.0.1` only, and pulls both models (subsequent
runs reuse the already-downloaded weights). The container is started with
`--restart unless-stopped`, so Podman will restart it automatically if it
stops or the machine reboots (as long as Podman itself is running).

Check on it any time:

```shell
./status-llm-server.sh
```

Stop it:

```shell
./stop-llm-server.sh          # keeps downloaded model weights
./stop-llm-server.sh --purge  # also deletes the models/volume
```

### Connecting from a devbox

`../devbox` automatically detects the running `opencode-local-llm`
container and, if found, starts new devboxes with
`--network slirp4netns:allow_host_loopback=true`. This is rootless Podman's
standard way to let a container reach services bound to the _host's own_
loopback interface: `10.0.2.2` is the fixed gateway address slirp4netns
gives every container for that purpose (no shared Docker/Podman network
needed). No extra steps are needed once the server has been started; this
matches the `local-llm` provider's `baseURL` already configured in
[`../opencode.json`](../opencode.json):

```jsonc
"local-llm": {
  "npm": "@ai-sdk/openai-compatible",
  "name": "Local LLM (Ollama)",
  "options": {
    "baseURL": "http://10.0.2.2:11434/v1"
  },
  "models": {
    "gpt-oss:20b": { "name": "gpt-oss-20b (local)" },
    "qwen3.8:27b": { "name": "Qwen3.8-27B (local)" }
  }
}
```

Inside a devbox, select one of the models with `opencode models` /
`/models`, or run a one-shot prompt directly:

```shell
opencode run -m local-llm/gpt-oss:20b "Say hi in 3 words"
```

### Configuration

All settings can be overridden with environment variables when running the
scripts, e.g.:

```shell
LLM_CONTEXT_LENGTH=65536 ./start-llm-server.sh
```

| Variable | Default | Purpose |
| --- | --- | --- |
| `LLM_IMAGE` | pinned `ollama/ollama` | Ollama server image |
| `LLM_CONTAINER_NAME` | `opencode-local-llm` | Container name; also what `devbox` looks for |
| `LLM_VOLUME_NAME` | `opencode-local-llm-data` | Named volume holding downloaded model weights |
| `LLM_PORT` | `11434` | Host port (127.0.0.1) the API is published on |
| `LLM_CONTEXT_LENGTH` | `131072` | Context window (tokens) applied to served models |
| `LLM_KEEP_ALIVE` | `30m` | How long an idle model stays loaded before eviction |

If you change `LLM_PORT` or `LLM_CONTAINER_NAME`, update
[`../opencode.json`](../opencode.json)'s `local-llm.options.baseURL` (and
`devbox`'s `LLM_CONTAINER_NAME` default) to match.

Lower `LLM_CONTEXT_LENGTH` (e.g. to `32768`) if you hit out-of-memory
errors; both models also support smaller contexts fine.

## Performance expectations

This is CPU inference of 20-27B-class models, so it's not fast, and the
_first_ message of an OpenCode session is the slowest: OpenCode's default
agent sends a large tool-definition system prompt (several thousand
tokens) that the server must process before generating anything. Measured
on a 22-core / 64GB CPU-only host:

- Direct, short prompts (no agent/tool overhead), model already loaded:
  a few seconds to ~30s.
- A model that isn't currently loaded: add ~10-30s to read its weights
  from disk before it can start.
- A full OpenCode agent turn (first message of a session, with the full
  tool-definition prompt): tens of minutes for `qwen3.8:27b` (measured
  ~36 minutes for a trivial one-word request, dominated by prompt
  processing at ~3.5-4 tokens/sec for its ~7400-token prompt).
  Follow-up messages in the _same_ session are faster since the server
  caches the processed prompt prefix.
- `gpt-oss:20b` processes prompts faster (~14 tokens/sec) but its hybrid
  attention design forces a full prompt re-processing on some turns; still
  much slower overall than typical hosted APIs.

In short: expect the first response in a new OpenCode session to take a
while (get a coffee), especially with `qwen3.8:27b`. This is a hardware/CPU
inference limitation, not a misconfiguration.

## Why Ollama

Ollama was chosen over alternatives like llama.cpp's `llama-server` or vLLM
because:

- It serves multiple models from a single container/port and manages
  loading/unloading them from RAM automatically, avoiding one server
  process per model (important for keeping two ~15-20GB models within a
  64GB RAM budget).
- It provides an OpenAI-compatible API out of the box
  (`/v1/chat/completions`), which is exactly what OpenCode's
  `@ai-sdk/openai-compatible` provider expects.
- It has first-class, pre-quantized support for both `gpt-oss` and
  `qwen3.8` directly from its model library, with no manual GGUF selection
  needed.
- vLLM is primarily optimized for GPU serving; CPU inference support is
  limited and slower for this use case.
