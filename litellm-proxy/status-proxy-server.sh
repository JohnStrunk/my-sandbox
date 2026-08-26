#!/usr/bin/env bash
# Show the status of the LiteLLM proxy server and its health.
set -euo pipefail

CONTAINER_NAME="${PROXY_CONTAINER_NAME:-opencode-litellm-proxy}"
PORT="${PROXY_PORT:-4000}"

if ! podman container exists "$CONTAINER_NAME"; then
  echo "Container '$CONTAINER_NAME' does not exist. Run ./start-proxy-server.sh first." >&2
  exit 1
fi

podman ps --filter "name=^${CONTAINER_NAME}\$"
echo
echo "==> API readiness (http://localhost:${PORT}/health/readiness):"
curl -sf "http://localhost:${PORT}/health/readiness" || echo "  (not reachable)"
echo
echo "==> Available models (http://localhost:${PORT}/v1/models):"
AUTH_HEADER=()
if [ -n "${LITELLM_MASTER_KEY:-}" ]; then
  AUTH_HEADER=(-H "Authorization: Bearer ${LITELLM_MASTER_KEY}")
fi
curl -sf "${AUTH_HEADER[@]}" "http://localhost:${PORT}/v1/models" || echo "  (not reachable or unauthorized)"
echo
