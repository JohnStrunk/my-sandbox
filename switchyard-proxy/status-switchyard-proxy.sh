#!/usr/bin/env bash
# Show the status of the Switchyard LLM autorouting proxy server and its
# available models and statistics.
set -euo pipefail

CONTAINER_NAME="${SWITCHYARD_CONTAINER_NAME:-opencode-switchyard-proxy}"
PORT="${SWITCHYARD_PORT:-4000}"

if ! podman container exists "$CONTAINER_NAME" 2>/dev/null; then
  echo "Container '$CONTAINER_NAME' does not exist. Run ./start-switchyard-proxy.sh first." >&2
  exit 1
fi

podman ps --filter "name=^${CONTAINER_NAME}\$"
echo
echo "==> Health (http://localhost:${PORT}/health):"
curl -sf "http://localhost:${PORT}/health" || echo "  (not reachable)"
echo
echo "==> Models (http://localhost:${PORT}/v1/models):"
curl -sf "http://localhost:${PORT}/v1/models" || echo "  (not reachable)"
echo
echo "==> Stats (http://localhost:${PORT}/v1/stats):"
curl -sf "http://localhost:${PORT}/v1/stats" || echo "  (not reachable)"
echo
