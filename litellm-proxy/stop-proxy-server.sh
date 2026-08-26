#!/usr/bin/env bash
# Stop and remove the LiteLLM proxy container started by start-proxy-server.sh.
set -euo pipefail

CONTAINER_NAME="${PROXY_CONTAINER_NAME:-opencode-litellm-proxy}"

echo "==> Stopping and removing container '$CONTAINER_NAME'"
podman rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
