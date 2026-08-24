#!/usr/bin/env bash
# Stop (and optionally remove) the local LLM inference server started by
# start-llm-server.sh. Downloaded model weights are kept in their named
# volume unless --purge is given.
set -euo pipefail

CONTAINER_NAME="${LLM_CONTAINER_NAME:-opencode-local-llm}"
VOLUME_NAME="${LLM_VOLUME_NAME:-opencode-local-llm-data}"

echo "==> Stopping and removing container '$CONTAINER_NAME'"
podman rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true

if [ "${1:-}" = "--purge" ]; then
  echo "==> Removing volume '$VOLUME_NAME' (deletes downloaded model weights)"
  podman volume rm "$VOLUME_NAME" >/dev/null 2>&1 || true
else
  echo "    (model weights kept in volume '$VOLUME_NAME'; pass --purge to delete them too)"
fi
