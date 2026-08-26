#!/usr/bin/env bash
# Run on the local machine (the Podman host, NOT inside a devbox) to start
# the Switchyard LLM autorouting proxy container that devbox instances can
# connect to (see ../devbox and ../opencode.json).
#
# Uses Switchyard (https://github.com/NVIDIA-NeMo/Switchyard):
#   - Translates between OpenAI Chat, Anthropic Messages, and OpenAI Responses.
#   - Routes requests dynamically across LLM providers based on capability classification,
#     stage progress signals, or A/B random splits.
#
# The container publishes its port to 127.0.0.1 only (not the LAN). Devbox
# containers reach it there too: rootless Podman's default (slirp4netns)
# networking always makes the host's own loopback interface reachable from
# inside a container at the fixed address 10.0.2.2, as long as the
# container opts in with `--network slirp4netns:allow_host_loopback=true`.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

IMAGE="${SWITCHYARD_IMAGE:-switchyard-proxy:latest}"
CONTAINER_NAME="${SWITCHYARD_CONTAINER_NAME:-opencode-switchyard-proxy}"
PORT="${SWITCHYARD_PORT:-4000}"
CONFIG_FILE="${SWITCHYARD_CONFIG:-$HERE/routes.toml}"

if ! podman image exists "$IMAGE" 2>/dev/null && ! podman image exists "localhost/$IMAGE" 2>/dev/null; then
  echo "==> Building Switchyard proxy container image '$IMAGE'"
  podman build \
    --file "$HERE/Dockerfile" \
    --tag "$IMAGE" \
    "$HERE"
fi

echo "==> Removing any previous '$CONTAINER_NAME' container"
podman rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true

ENV_ARGS=()
if [ -n "${GEMINI_API_KEY:-}" ]; then
  ENV_ARGS+=(--env "GEMINI_API_KEY=${GEMINI_API_KEY}")
fi
if [ -n "${GOOGLE_GENERATIVE_AI_API_KEY:-}" ]; then
  ENV_ARGS+=(--env "GOOGLE_GENERATIVE_AI_API_KEY=${GOOGLE_GENERATIVE_AI_API_KEY}")
fi
if [ -n "${LITEMAAS_API_KEY:-}" ]; then
  ENV_ARGS+=(--env "LITEMAAS_API_KEY=${LITEMAAS_API_KEY}")
fi
if [ -n "${OPENAI_API_KEY:-}" ]; then
  ENV_ARGS+=(--env "OPENAI_API_KEY=${OPENAI_API_KEY}")
fi
if [ -n "${ANTHROPIC_API_KEY:-}" ]; then
  ENV_ARGS+=(--env "ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}")
fi
if [ -n "${OPENROUTER_API_KEY:-}" ]; then
  ENV_ARGS+=(--env "OPENROUTER_API_KEY=${OPENROUTER_API_KEY}")
fi

echo "==> Starting Switchyard proxy container '$CONTAINER_NAME'"
podman run -d \
  --name "$CONTAINER_NAME" \
  --restart unless-stopped \
  --publish "127.0.0.1:${PORT}:4000" \
  --volume "${CONFIG_FILE}:/app/routes.toml:ro" \
  --env SWITCHYARD_CONFIG=/app/routes.toml \
  --env "SWITCHYARD_PORT=4000" \
  "${ENV_ARGS[@]}" \
  "$IMAGE" >/dev/null

echo "==> Waiting for the proxy server to become ready"
for _ in $(seq 1 30); do
  curl -sf "http://localhost:${PORT}/health" >/dev/null 2>&1 && break
  sleep 1
done

cat <<EOF

==> Switchyard proxy is up.
    - OpenAI / Anthropic compatible API: http://localhost:${PORT}/v1  (from this machine)
    - From devbox containers created via '$HERE/../devbox':
      http://10.0.2.2:${PORT}/v1
    - Health: http://localhost:${PORT}/health
    - Models: http://localhost:${PORT}/v1/models
    - Stats:  http://localhost:${PORT}/v1/stats

Run '$HERE/stop-switchyard-proxy.sh' to stop it, or re-run this script to apply
changed settings.
EOF
