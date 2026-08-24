#!/usr/bin/env bash
# Run once on the local machine (the Podman host, NOT inside a devbox) to
# start a local LLM inference server that every devbox instance created
# from this repo can connect to (see ../create-devbox and ../opencode.json).
#
# Uses Ollama (https://ollama.com) as the inference server:
#   - Single container serves multiple models via an OpenAI-compatible API.
#   - Loads/unloads models on demand (OLLAMA_KEEP_ALIVE below), so the two
#     ~14-18GB quantized models below don't both have to stay resident in
#     RAM at once.
#   - CPU inference works out of the box; no GPU is required or used here.
#
# The container publishes its port to 127.0.0.1 only (not the LAN). Devbox
# containers reach it there too: rootless Podman's default (slirp4netns)
# networking always makes the host's own loopback interface reachable from
# inside a container at the fixed address 10.0.2.2, as long as the
# container opts in with `--network slirp4netns:allow_host_loopback=true`
# (see ../create-devbox, which adds this automatically once this script has
# been run). This avoids relying on a shared Podman bridge network, which
# needs kernel network sysctls that aren't writable in every environment
# (e.g. some hardened/nested containers).
#
# Re-running this script is safe: it recreates the container (picking up
# any changed settings) without re-downloading model weights, which are
# kept in a named volume.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# renovate: datasource=docker depName=docker.io/ollama/ollama
IMAGE="${LLM_IMAGE:-docker.io/ollama/ollama:0.32.15@sha256:57d60e686821ea81a7748a3ec8141308c8b8f95b27105713954abf7a6529e700}"
CONTAINER_NAME="${LLM_CONTAINER_NAME:-opencode-local-llm}"
VOLUME_NAME="${LLM_VOLUME_NAME:-opencode-local-llm-data}"
PORT="${LLM_PORT:-11434}"

# Context length applied to every model served. Both models below support
# large native context windows (128K / 256K); this is deliberately generous
# for coding use cases (large repos/diffs in context) while remaining
# affordable in RAM on a 64GB CPU-only machine when combined with the
# flash-attention + quantized KV cache settings below. Lower this (e.g. to
# 32768) if you hit out-of-memory errors.
CONTEXT_LENGTH="${LLM_CONTEXT_LENGTH:-131072}"

# How long an idle model stays loaded in RAM before being evicted. Keeps a
# recently-used model warm across a handful of requests without permanently
# reserving RAM for both models simultaneously.
KEEP_ALIVE="${LLM_KEEP_ALIVE:-30m}"

# Models to make available. gpt-oss:20b ships natively pre-quantized
# (MXFP4); qwen3.8:27b's default tag is a q4_K_M GGUF quantization with
# multi-token prediction (MTP) enabled for faster CPU decoding. Both fit
# comfortably in 64GB RAM one at a time alongside the large context above.
MODELS=(
  "gpt-oss:20b"
  "qwen3.8:27b"
)

echo "==> Creating volume '$VOLUME_NAME' for model storage (if missing)"
podman volume exists "$VOLUME_NAME" || podman volume create "$VOLUME_NAME" >/dev/null

echo "==> Removing any previous '$CONTAINER_NAME' container"
podman rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true

echo "==> Starting inference server container '$CONTAINER_NAME'"
podman run -d \
  --name "$CONTAINER_NAME" \
  --restart unless-stopped \
  --publish "127.0.0.1:${PORT}:11434" \
  --volume "${VOLUME_NAME}:/root/.ollama" \
  --env OLLAMA_HOST=0.0.0.0:11434 \
  --env OLLAMA_CONTEXT_LENGTH="${CONTEXT_LENGTH}" \
  --env OLLAMA_FLASH_ATTENTION=1 \
  --env OLLAMA_KV_CACHE_TYPE=q8_0 \
  --env OLLAMA_KEEP_ALIVE="${KEEP_ALIVE}" \
  --env OLLAMA_MAX_LOADED_MODELS=1 \
  "$IMAGE" >/dev/null

echo "==> Waiting for the server to become ready"
for _ in $(seq 1 60); do
  podman exec "$CONTAINER_NAME" ollama list >/dev/null 2>&1 && break
  sleep 1
done

for model in "${MODELS[@]}"; do
  echo "==> Pulling model '$model' (this can take a while the first time)"
  podman exec "$CONTAINER_NAME" ollama pull "$model"
done

cat <<EOF

==> Local LLM server is up.
    - OpenAI-compatible API: http://localhost:${PORT}/v1  (from this machine)
    - From devbox containers created via '$HERE/../create-devbox':
      http://10.0.2.2:${PORT}/v1
    - Models available: ${MODELS[*]}

Run '$HERE/stop-llm-server.sh' to stop it, or re-run this script to apply
changed settings.
EOF
