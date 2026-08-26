#!/usr/bin/env bash
# Run once on the local machine (the Podman host, NOT inside a devbox) to
# start a LiteLLM autorouting proxy server that every devbox instance created
# from this repo can connect to (see ../devbox and ../opencode.json).
#
# Uses LiteLLM (https://docs.litellm.ai/docs/proxy/auto_routing) as the proxy:
#   - Routes incoming LLM requests automatically based on prompt complexity
#     across configured model tiers (SIMPLE, MEDIUM, COMPLEX, REASONING).
#   - Exposes an OpenAI-compatible API on port 4000 (default) with
#     smart-router and upstream model endpoints.
#   - Passes through credentials from host environment (GEMINI_API_KEY,
#     LITEMAAS_API_KEY, OPENAI_API_KEY, ANTHROPIC_API_KEY, etc.) to upstream
#     providers.
#
# The container publishes its port to 127.0.0.1 only (not the LAN). Devbox
# containers reach it there too: rootless Podman's default (slirp4netns)
# networking makes the host's loopback interface reachable at 10.0.2.2 when
# started with `--network slirp4netns:allow_host_loopback=true` (which
# ../devbox configures automatically).
#
# Re-running this script is safe: it recreates the container with updated
# configuration/environment.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# renovate: datasource=docker depName=ghcr.io/berriai/litellm
IMAGE="${PROXY_IMAGE:-ghcr.io/berriai/litellm:main-latest@sha256:0f4dce575a6c33d737886fe0796a6b3022358b45c2068a5e1312293e28b35f0f}"
CONTAINER_NAME="${PROXY_CONTAINER_NAME:-opencode-litellm-proxy}"
CONFIG_FILE="${PROXY_CONFIG_FILE:-$HERE/config.yaml}"
PORT="${PROXY_PORT:-4000}"

if [ ! -f "$CONFIG_FILE" ]; then
  echo "Error: Config file '$CONFIG_FILE' does not exist." >&2
  exit 1
fi

echo "==> Removing any previous '$CONTAINER_NAME' container"
podman rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true

ENV_ARGS=()
for var in \
  GEMINI_API_KEY \
  GOOGLE_GENERATIVE_AI_API_KEY \
  GOOGLE_CLOUD_PROJECT \
  VERTEX_LOCATION \
  LITEMAAS_API_KEY \
  OPENAI_API_KEY \
  ANTHROPIC_API_KEY \
  LITELLM_MASTER_KEY \
  LITELLM_SALT_KEY
do
  if [ -n "${!var:-}" ]; then
    ENV_ARGS+=(--env "$var=${!var}")
  fi
done

echo "==> Starting LiteLLM proxy container '$CONTAINER_NAME'"
for _ in $(seq 1 10); do
  podman rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
  if podman run -d \
    --name "$CONTAINER_NAME" \
    --restart unless-stopped \
    --publish "127.0.0.1:${PORT}:4000" \
    --network slirp4netns:allow_host_loopback=true \
    --volume "${CONFIG_FILE}:/app/config.yaml:ro,z" \
    "${ENV_ARGS[@]}" \
    "$IMAGE" \
    --config /app/config.yaml \
    --port 4000 >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

echo "==> Waiting for the proxy server to become ready"
READY=0
for _ in $(seq 1 60); do
  if curl -sf "http://127.0.0.1:${PORT}/health/readiness" >/dev/null 2>&1; then
    READY=1
    break
  fi
  sleep 1
done

if [ "$READY" -ne 1 ]; then
  echo "Error: Proxy server failed to become ready." >&2
  podman logs "$CONTAINER_NAME" 2>&1 | tail -n 20 >&2 || true
  exit 1
fi

cat <<EOF

==> LiteLLM autorouting proxy is up.
    - OpenAI-compatible API: http://localhost:${PORT}/v1  (from this machine)
    - From devbox containers created via '$HERE/../devbox':
      http://10.0.2.2:${PORT}/v1
    - Autorouting model: smart-router

Run '$HERE/stop-proxy-server.sh' to stop it, or re-run this script to apply
changed settings.
EOF
