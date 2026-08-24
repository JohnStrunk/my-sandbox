#!/usr/bin/env bash
# Show the status of the local LLM inference server and the models it has
# available.
set -euo pipefail

CONTAINER_NAME="${LLM_CONTAINER_NAME:-opencode-local-llm}"
PORT="${LLM_PORT:-11434}"

if ! podman container exists "$CONTAINER_NAME"; then
  echo "Container '$CONTAINER_NAME' does not exist. Run ./start-llm-server.sh first." >&2
  exit 1
fi

podman ps --filter "name=^${CONTAINER_NAME}\$"
echo
echo "==> Models:"
podman exec "$CONTAINER_NAME" ollama list
echo
echo "==> API health (http://localhost:${PORT}/v1/models):"
curl -sf "http://localhost:${PORT}/v1/models" || echo "  (not reachable)"
echo
